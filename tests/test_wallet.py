from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select

from chat_service.billing.wallet import UsageRecord, WalletError, WalletService
from chat_service.db.schema import chat_bucket_debits, chat_token_packs, chat_usage_events


def usage(inp: int, out: int) -> UsageRecord:
    return UsageRecord(
        billable_input_tokens=inp,
        billable_output_tokens=out,
        model_name="test-model",
        prompt_version="system_v1",
        billing_tokenizer_version="dev-regex-v0",
    )


@pytest.fixture
def wallet(engine, settings, clock) -> WalletService:
    return WalletService(engine, settings, clock)


async def reserve(wallet, org, tokens, user=None):
    return await wallet.reserve(org, user or uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), tokens)


async def add_pack(engine, pack_id="p1m", tokens=1_000_000, validity_days=365):
    async with engine.begin() as conn:
        await conn.execute(
            chat_token_packs.insert().values(
                pack_id=pack_id, tokens=tokens, price_minor=1000, currency="USD", validity_days=validity_days
            )
        )


async def buy(wallet, engine, org, pid="pay_1", pack_id="p1m", tokens=1_000_000):
    await add_pack(engine, pack_id, tokens)
    await wallet.create_pending_payment(org, pack_id, "stripe", pid, None)
    return await wallet.credit_purchase(pid)


# ---------------------------------------------------------------- free trial


async def test_free_trial_granted_once_with_expiry(wallet, settings, clock):
    org = uuid.uuid4()
    assert await wallet.ensure_free_trial(org) is True
    assert await wallet.ensure_free_trial(org) is False
    bal = await wallet.get_balance(org)
    assert bal.tokens_available == settings.free_trial_tokens
    assert len(bal.buckets) == 1
    assert bal.buckets[0].expires_at == clock.now.replace() + __import__("datetime").timedelta(
        days=settings.free_trial_days
    )
    assert bal.only_free_trial
    assert await wallet.check_invariant(org)


async def test_free_trial_concurrent_grants_only_once(wallet, settings):
    org = uuid.uuid4()
    results = await asyncio.gather(*(wallet.ensure_free_trial(org) for _ in range(8)), return_exceptions=True)
    granted = [r for r in results if r is True]
    assert len(granted) == 1
    bal = await wallet.get_balance(org)
    assert bal.tokens_available == settings.free_trial_tokens


# ---------------------------------------------------------------- reserve


async def test_reserve_fails_when_insufficient(wallet):
    org = uuid.uuid4()
    await wallet.ensure_free_trial(org)
    assert await reserve(wallet, org, 200_001) is None
    assert await reserve(wallet, org, 200_000) is not None
    assert await reserve(wallet, org, 1) is None
    bal = await wallet.get_balance(org)
    assert bal.spendable == 0


async def test_reserve_unknown_org_returns_none(wallet):
    assert await reserve(wallet, uuid.uuid4(), 10) is None


async def test_parallel_reservations_never_overspend(wallet, settings):
    org = uuid.uuid4()
    await wallet.ensure_free_trial(org)  # 200,000 tokens
    # 10 parallel requests of 60,000: only 3 can fit.
    results = await asyncio.gather(*(reserve(wallet, org, 60_000) for _ in range(10)))
    assert sum(r is not None for r in results) == 3
    bal = await wallet.get_balance(org)
    assert bal.tokens_reserved == 180_000
    assert bal.spendable == 20_000


# ---------------------------------------------------------------- settle / release


async def test_settle_charges_min_actual_reserved_and_is_idempotent(wallet):
    org = uuid.uuid4()
    await wallet.ensure_free_trial(org)
    r = await reserve(wallet, org, 1000)
    assert await wallet.settle(r.reservation_id, usage(100, 250)) == 350
    assert await wallet.settle(r.reservation_id, usage(100, 250)) == 350  # idempotent
    bal = await wallet.get_balance(org)
    assert bal.tokens_available == 200_000 - 350
    assert bal.tokens_reserved == 0
    assert await wallet.check_invariant(org)


async def test_settle_never_charges_more_than_reserved(wallet):
    org = uuid.uuid4()
    await wallet.ensure_free_trial(org)
    r = await reserve(wallet, org, 500)
    assert await wallet.settle(r.reservation_id, usage(300, 900)) == 500
    bal = await wallet.get_balance(org)
    assert bal.tokens_available == 200_000 - 500


async def test_release_charges_nothing_and_records_not_charged(wallet, engine):
    org = uuid.uuid4()
    await wallet.ensure_free_trial(org)
    r = await reserve(wallet, org, 1000)
    await wallet.release(r.reservation_id, usage(100, 0), "model_error")
    await wallet.release(r.reservation_id, usage(100, 0), "model_error")  # idempotent
    bal = await wallet.get_balance(org)
    assert bal.tokens_available == 200_000 and bal.tokens_reserved == 0
    async with engine.connect() as conn:
        ev = (await conn.execute(select(chat_usage_events))).one()
    assert ev.status == "not_charged" and ev.tokens_charged == 0 and ev.reason == "model_error"
    with pytest.raises(WalletError):
        await wallet.settle(r.reservation_id, usage(1, 1))


async def test_buckets_debited_in_order_free_trial_first(wallet, engine):
    org = uuid.uuid4()
    pack_bucket = await buy(wallet, engine, org)
    await wallet.ensure_free_trial(org)
    r = await reserve(wallet, org, 250_000)
    assert await wallet.settle(r.reservation_id, usage(50_000, 200_000)) == 250_000
    async with engine.connect() as conn:
        debits = (await conn.execute(select(chat_bucket_debits).order_by(chat_bucket_debits.c.id))).all()
    assert [d.tokens for d in debits] == [200_000, 50_000]
    assert debits[1].bucket_id == pack_bucket
    bal = await wallet.get_balance(org)
    assert [b.source for b in bal.buckets] == ["pack"]  # free trial exhausted
    assert await wallet.check_invariant(org)


# ---------------------------------------------------------------- shadow


async def test_shadow_records_usage_without_touching_wallet(wallet, engine):
    org = uuid.uuid4()
    await wallet.ensure_free_trial(org)
    charged = await wallet.record_shadow(org, uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), usage(40, 60), True)
    assert charged == 100
    bal = await wallet.get_balance(org)
    assert bal.tokens_available == 200_000 and bal.tokens_reserved == 0
    async with engine.connect() as conn:
        ev = (await conn.execute(select(chat_usage_events))).one()
    assert ev.status == "shadow" and ev.tokens_charged == 100


# ---------------------------------------------------------------- expiry


async def test_expired_reservations_are_released(wallet, clock, settings):
    org = uuid.uuid4()
    await wallet.ensure_free_trial(org)
    await reserve(wallet, org, 5000)
    clock.advance(seconds=settings.reservation_ttl_seconds + 1)
    assert await wallet.expire_reservations() == 1
    bal = await wallet.get_balance(org)
    assert bal.tokens_reserved == 0


async def test_expired_bucket_removed_but_not_while_reserved(wallet, clock, settings):
    org = uuid.uuid4()
    await wallet.ensure_free_trial(org)
    r = await reserve(wallet, org, 1000)
    clock.advance(days=settings.free_trial_days, seconds=1)
    assert await wallet.expire_buckets() == 0  # in-flight reservation depends on it
    await wallet.settle(r.reservation_id, usage(10, 10))
    assert await wallet.expire_buckets() == 1
    bal = await wallet.get_balance(org)
    assert bal.tokens_available == 0 and bal.buckets == []
    assert await wallet.check_invariant(org)


# ---------------------------------------------------------------- purchases, refunds, goodwill


async def test_credit_purchase_is_idempotent(wallet, engine):
    org = uuid.uuid4()
    b1 = await buy(wallet, engine, org)
    b2 = await wallet.credit_purchase("pay_1")
    assert b1 == b2
    bal = await wallet.get_balance(org)
    assert bal.tokens_available == 1_000_000
    assert await wallet.check_invariant(org)


async def test_unknown_pack_rejected(wallet):
    with pytest.raises(WalletError):
        await wallet.create_pending_payment(uuid.uuid4(), "nope", "stripe", "pay_x", None)


async def test_refund_removes_remaining_without_going_negative(wallet, engine):
    org = uuid.uuid4()
    await buy(wallet, engine, org)
    r = await reserve(wallet, org, 400_000)
    await wallet.settle(r.reservation_id, usage(100_000, 200_000))  # 300k used
    held = await reserve(wallet, org, 650_000)
    result = await wallet.reverse_purchase("pay_1")
    assert result.pending and result.tokens_already_used == 300_000
    await wallet.release(held.reservation_id, usage(0, 0), "client_disconnect")
    assert await wallet.process_pending_reversals() == 1
    bal = await wallet.get_balance(org)
    assert bal.tokens_available == 0 and bal.buckets == []
    assert await wallet.check_invariant(org)


async def test_goodwill_credit(wallet):
    org = uuid.uuid4()
    await wallet.grant_goodwill(org, 5000, uuid.uuid4(), "sorry for the outage")
    bal = await wallet.get_balance(org)
    assert bal.tokens_available == 5000 and bal.buckets[0].source == "goodwill"


async def test_daily_usage_counts_charged_and_shadow(wallet, clock):
    org = uuid.uuid4()
    await wallet.ensure_free_trial(org)
    r = await reserve(wallet, org, 1000)
    await wallet.settle(r.reservation_id, usage(100, 100))
    await wallet.record_shadow(org, uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), usage(50, 50), True)
    start_of_day = clock.now.replace(hour=0, minute=0)
    assert await wallet.org_tokens_since(org, start_of_day) == 300


async def test_jobs_run_once_covers_actions_and_billing(wallet, engine, settings, clock):
    from chat_service.actions.service import ActionService
    from chat_service.billing.invoices import InvoiceService
    from chat_service.billing.subscriptions import SubscriptionService
    from chat_service.engine.store import ConversationStore
    from chat_service.jobs import run_once
    from chat_service.tools.product import MockProductAPI

    subs = SubscriptionService(engine, settings, wallet, InvoiceService(engine, settings, clock=clock), clock)
    await subs.upsert_plan(plan_id="s", name="S", monthly_fee_minor=100, currency="INR", monthly_included_tokens=10)
    org = uuid.uuid4()
    await subs.subscribe(org, "s")
    clock.advance(days=32)
    result = await run_once(
        wallet,
        ConversationStore(engine),
        settings,
        ActionService(engine, settings, MockProductAPI(), clock=clock),
        subs,
    )
    assert result["invoices_created"] == 1 and result["invariant_failures"] == 0


async def test_paid_org_is_not_treated_as_free_trial_after_buckets_run_out(wallet, engine):
    org = uuid.uuid4()
    await buy(wallet, engine, org, tokens=100)
    r = await reserve(wallet, org, 100)
    await wallet.settle(r.reservation_id, usage(50, 50))
    bal = await wallet.get_balance(org)
    assert bal.buckets == [] and bal.only_free_trial is False

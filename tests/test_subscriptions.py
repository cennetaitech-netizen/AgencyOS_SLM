from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, update

from chat_service.billing.caps import CapsService, LogNotifier
from chat_service.billing.invoices import InvoiceError, InvoiceService, LogInvoiceSink
from chat_service.billing.subscriptions import SubscriptionError, SubscriptionService, add_month
from chat_service.billing.wallet import UsageRecord, WalletService
from chat_service.db.schema import (
    chat_billing_profiles,
    chat_invoice_lines,
    chat_token_packs,
    chat_usage_events,
)

PRICE_PER_1M = 100_000  # 1,000.00 per 1M billable tokens (minor units)


def usage(total: int, client_id: str | None = None) -> UsageRecord:
    return UsageRecord(
        billable_input_tokens=total // 2,
        billable_output_tokens=total - total // 2,
        model_name="m",
        prompt_version="system_v1",
        billing_tokenizer_version="dev-regex-v0",
        client_id=client_id,
    )


@pytest.fixture
def svc(engine, settings, clock):
    wallet = WalletService(engine, settings, clock)
    sink = LogInvoiceSink()
    invoices = InvoiceService(engine, settings, sink, clock)
    subs = SubscriptionService(engine, settings, wallet, invoices, clock)
    caps = CapsService(engine, LogNotifier(), clock=clock)
    return wallet, invoices, subs, caps, sink


@pytest.fixture
async def plans(svc):
    _, _, subs, _, _ = svc
    await subs.upsert_plan(
        plan_id="starter",
        name="Starter",
        monthly_fee_minor=499_900,
        currency="INR",
        monthly_included_tokens=2_000_000,
        overage_allowed=False,
    )
    await subs.upsert_plan(
        plan_id="pro",
        name="Pro",
        monthly_fee_minor=1_999_900,
        currency="INR",
        monthly_included_tokens=10_000_000,
        overage_allowed=True,
        price_per_1m_tokens_minor=PRICE_PER_1M,
    )
    await subs.upsert_plan(
        plan_id="tiny",
        name="Tiny",
        monthly_fee_minor=10_000,
        currency="INR",
        monthly_included_tokens=1_000,
        overage_allowed=True,
        price_per_1m_tokens_minor=PRICE_PER_1M,
    )


async def spend(wallet, org, tokens, client_id=None):
    r = await wallet.reserve(org, uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), tokens)
    if r is None:
        return None
    return await wallet.settle(r.reservation_id, usage(tokens, client_id))


def test_add_month_clamps():
    assert add_month(datetime(2026, 1, 31, tzinfo=UTC)) == datetime(2026, 2, 28, tzinfo=UTC)
    assert add_month(datetime(2026, 12, 15, tzinfo=UTC)) == datetime(2027, 1, 15, tzinfo=UTC)


# ---------------------------------------------------------------- subscribe & invoices


async def test_subscribe_grants_allowance_and_invoices_fee_in_advance(svc, plans, engine):
    wallet, invoices, subs, _, sink = svc
    org = uuid.uuid4()
    invoice_id = await subs.subscribe(org, "starter")
    bal = await wallet.get_balance(org)
    assert bal.tokens_available == 2_000_000 and bal.buckets[0].source == "plan_monthly"
    inv = await invoices.get(org, invoice_id)
    assert inv.status == "draft" and inv.kind == "subscription_start" and inv.total_minor == 499_900
    assert await invoices.list(org) == []  # drafts hidden from clients
    sent = await invoices.approve_and_send(invoice_id, uuid.uuid4())
    assert sent.status == "sent" and sent.invoice_number.startswith("AOS-2026-")
    assert len(sink.sent) == 1
    with pytest.raises(SubscriptionError):
        await subs.subscribe(org, "pro")


async def test_tax_applied_from_billing_profile(svc, plans, engine, settings):
    _, invoices, subs, _, _ = svc
    settings.tax_rates = {"IN": 18}
    org = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            chat_billing_profiles.insert().values(
                org_id=org,
                legal_name="Acme Foods Pvt Ltd",
                address={"city": "Mumbai"},
                tax_id="27ABCDE1234F1Z5",
                country="IN",
                billing_email="billing@example.com",
            )
        )
    inv = await invoices.get(org, await subs.subscribe(org, "starter"))
    assert inv.tax_minor == 89_982 and inv.total_minor == 499_900 + 89_982


async def test_tampered_invoice_cannot_be_approved(svc, plans, engine):
    _, invoices, subs, _, _ = svc
    org = uuid.uuid4()
    invoice_id = await subs.subscribe(org, "starter")
    async with engine.begin() as conn:
        await conn.execute(
            update(chat_invoice_lines).where(chat_invoice_lines.c.invoice_id == invoice_id).values(amount_minor=1)
        )
    with pytest.raises(InvoiceError):
        await invoices.approve_and_send(invoice_id, None)
    await invoices.void(invoice_id)
    assert (await invoices.get(org, invoice_id)).status == "void"


# ---------------------------------------------------------------- consumption order & overage


async def test_consumption_order_free_then_monthly_then_pack(svc, plans, engine):
    wallet, _, subs, _, _ = svc
    org = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            chat_token_packs.insert().values(pack_id="p", tokens=500, price_minor=1, currency="INR", validity_days=365)
        )
    await wallet.create_pending_payment(org, "p", "dev", "pay", None)
    await wallet.credit_purchase("pay")
    await subs.subscribe(org, "tiny")  # 1,000 monthly
    await wallet.ensure_free_trial(org)  # 200,000
    await spend(wallet, org, 200_500)
    remaining = {b.source: b.remaining for b in (await wallet.get_balance(org)).buckets}
    assert remaining == {"plan_monthly": 500, "pack": 500}


async def test_overage_stops_exactly_at_spend_limit(svc, plans, engine):
    wallet, _, subs, _, _ = svc
    org = uuid.uuid4()
    await subs.subscribe(org, "tiny", spend_limit_minor=500)  # 500 minor ÷ 100,000 per 1M → 5,000 tokens
    assert await wallet.capacity(org) == 1_000 + 5_000
    assert await spend(wallet, org, 3_000) == 3_000  # 1,000 from allowance + 2,000 overage
    assert await wallet.capacity(org) == 3_000
    assert await spend(wallet, org, 3_001) is None
    assert await spend(wallet, org, 3_000) == 3_000
    assert await wallet.capacity(org) == 0
    async with engine.connect() as conn:
        overage = (await conn.execute(select(chat_usage_events.c.overage_tokens))).scalars().all()
    assert sorted(overage) == [2_000, 3_000]
    assert await wallet.check_invariant(org)


async def test_parallel_overage_never_exceeds_limit(svc, plans):
    wallet, _, subs, _, _ = svc
    org = uuid.uuid4()
    await subs.subscribe(org, "tiny", spend_limit_minor=500)  # 6,000 total capacity
    results = await asyncio.gather(
        *(wallet.reserve(org, uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), 1_500) for _ in range(8))
    )
    assert sum(r is not None for r in results) == 4
    for r in results:
        if r is not None:
            await wallet.release(r.reservation_id, usage(0), "test")
    bal = await wallet.get_balance(org)
    assert bal.tokens_reserved == 0 and await wallet.capacity(org) == 6_000


async def test_no_overage_without_spend_limit_or_on_plans_without_overage(svc, plans):
    wallet, _, subs, _, _ = svc
    a, b = uuid.uuid4(), uuid.uuid4()
    await subs.subscribe(a, "tiny")  # overage plan but no spend limit
    await subs.subscribe(b, "starter", spend_limit_minor=10_000)  # spend limit but plan has no overage
    assert await spend(wallet, a, 1_001) is None
    assert await wallet.capacity(b) == 2_000_000


# ---------------------------------------------------------------- cycle close


async def test_cycle_close_invoices_overage_by_brand_and_renews(svc, plans, clock, engine):
    wallet, invoices, subs, _, _ = svc
    org = uuid.uuid4()
    await subs.subscribe(org, "tiny", spend_limit_minor=10_000)
    await spend(wallet, org, 1_000, "cl_acme")  # uses the allowance
    await spend(wallet, org, 2_500, "cl_acme")
    await spend(wallet, org, 1_500, "cl_nova")
    clock.advance(days=31)
    assert await subs.close_due_cycles() != []
    inv_list = [i for i in await invoices.list(org, include_drafts=True) if i["kind"] == "cycle"]
    inv = await invoices.get(org, uuid.UUID(inv_list[0]["invoice_id"]))
    assert inv.reconciled and inv.overage_tokens == 4_000
    overage = next(ln for ln in inv.lines if ln["kind"] == "overage")
    assert overage["amount_minor"] == 400 and overage["breakdown"]["by_client"] == {"cl_acme": 2_500, "cl_nova": 1_500}
    assert next(ln for ln in inv.lines if ln["kind"] == "plan_fee")["amount_minor"] == 10_000
    sub = await subs.get(org)
    assert sub.current_period_start == datetime(2026, 12, 22, 12, 0, tzinfo=UTC)
    await wallet.expire_buckets()
    buckets = [b for b in (await wallet.get_balance(org)).buckets if b.source == "plan_monthly"]
    assert [(b.remaining, b.expires_at) for b in buckets] == [(1_000, sub.current_period_end)]  # fresh allowance
    csv_text = await invoices.usage_csv(org, inv.invoice_id)
    assert csv_text.count("\n") == 4 and "cl_nova" in csv_text


async def test_catch_up_multiple_missed_cycles(svc, plans, clock):
    _, invoices, subs, _, _ = svc
    org = uuid.uuid4()
    await subs.subscribe(org, "starter")
    clock.advance(days=95)
    created = await subs.close_due_cycles()
    assert len(created) == 3
    assert (await subs.get(org)).current_period_end > clock.now


# ---------------------------------------------------------------- plan changes


async def test_upgrade_is_prorated_and_adds_allowance(svc, plans, clock):
    wallet, invoices, subs, _, _ = svc
    org = uuid.uuid4()
    await subs.subscribe(org, "starter")  # 22 Nov → 22 Dec (30 days)
    clock.advance(days=15)
    result = await subs.change_plan(org, "pro")
    assert result["change"] == "upgraded"
    assert result["prorated_fee_minor"] == (1_999_900 - 499_900) // 2
    assert result["extra_tokens"] == 4_000_000
    assert (await subs.get(org)).plan_id == "pro"
    assert (await wallet.get_balance(org)).tokens_available == 6_000_000


async def test_downgrade_applies_at_renewal(svc, plans, clock):
    wallet, _, subs, _, _ = svc
    org = uuid.uuid4()
    await subs.subscribe(org, "pro")
    assert (await subs.change_plan(org, "starter"))["change"] == "downgrade_scheduled"
    assert (await subs.get(org)).plan_id == "pro"
    clock.advance(days=31)
    await subs.close_due_cycles()
    sub = await subs.get(org)
    assert sub.plan_id == "starter" and sub.pending_plan_id is None


# ---------------------------------------------------------------- dunning


async def test_dunning_past_due_then_suspended_then_recovered(svc, plans, clock):
    wallet, _, subs, _, _ = svc
    org = uuid.uuid4()
    await subs.subscribe(org, "tiny", spend_limit_minor=500)
    await subs.payment_failed(org)
    assert (await subs.get(org)).status == "past_due"
    assert await wallet.capacity(org) == 1_000  # no overage while past due
    clock.advance(days=6)
    assert await subs.run_dunning() == 0
    clock.advance(days=2)
    assert await subs.run_dunning() == 1
    assert (await subs.get(org)).status == "suspended"
    await subs.payment_succeeded(org)
    assert (await subs.get(org)).status == "active"


# ---------------------------------------------------------------- caps & alerts


async def test_user_daily_cap_and_alerts_fire_once(svc, plans, clock):
    wallet, _, subs, caps, _ = svc
    org, user = uuid.uuid4(), uuid.uuid4()
    await wallet.ensure_free_trial(org)
    await caps.set_cap(org, "user", user, "day", 1_000)
    status = await caps.check(org, user, None, "Asia/Kolkata")
    assert status.remaining == 1_000

    r = await wallet.reserve(org, user, uuid.uuid4(), uuid.uuid4(), 850)
    await wallet.settle(r.reservation_id, usage(850))
    alerts = await caps.evaluate_alerts(org, user, None, "Asia/Kolkata")
    assert [a["threshold"] for a in alerts if a["scope"].startswith("cap:user")] == [80]
    assert await caps.evaluate_alerts(org, user, None, "Asia/Kolkata") == []  # fires once
    assert (await caps.check(org, user, None, "Asia/Kolkata")).remaining == 150

    other = uuid.uuid4()
    assert (await caps.check(org, other, None, "Asia/Kolkata")).remaining is None
    clock.advance(days=1)
    assert (await caps.check(org, user, None, "Asia/Kolkata")).remaining == 1_000


async def test_org_cap_must_use_org_id(svc):
    *_, caps, _ = svc
    with pytest.raises(ValueError):
        await caps.set_cap(uuid.uuid4(), "org", uuid.uuid4(), "month", 10)

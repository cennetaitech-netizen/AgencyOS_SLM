"""Token wallet: free trial, packs, reservations, settlement and expiry.

Money-safety rules (docs/IMPLEMENTATION_GUIDE.md §6.2):
* A wallet never goes below zero (DB CHECK constraints plus atomic statements).
* ``reserve`` is a single atomic UPDATE, so parallel chats can't overspend.
* ``settle`` charges ``min(actual, reserved)`` and is idempotent.
* Failed replies are released and never charged.
* Invariant: ``tokens_available == sum(granted - used)`` over active buckets.

Lock order everywhere: reservation → wallet → buckets (avoids deadlocks).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, case, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from chat_service.config import Settings
from chat_service.db.schema import (
    chat_bucket_debits,
    chat_credit_buckets,
    chat_payments,
    chat_plans,
    chat_reservations,
    chat_subscriptions,
    chat_token_packs,
    chat_usage_events,
    chat_wallets,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


# Consumption order: free/promo first, then plan allowance, then packs; earliest expiry first.
_BUCKET_ORDER = (
    case(
        (chat_credit_buckets.c.source == "free_trial", 0),
        (chat_credit_buckets.c.source == "goodwill", 1),
        (chat_credit_buckets.c.source == "plan_monthly", 2),
        else_=3,
    ),
    chat_credit_buckets.c.expires_at.asc().nulls_last(),
    chat_credit_buckets.c.granted_at.asc(),
)


@dataclass(frozen=True)
class BucketView:
    bucket_id: uuid.UUID
    source: str
    remaining: int
    expires_at: datetime | None


@dataclass(frozen=True)
class Balance:
    org_id: uuid.UUID
    tokens_available: int
    tokens_reserved: int
    buckets: list[BucketView] = field(default_factory=list)
    has_paid: bool = False  # ever bought a pack or has a subscription

    @property
    def spendable(self) -> int:
        return self.tokens_available - self.tokens_reserved

    @property
    def only_free_trial(self) -> bool:
        """True for orgs that have never paid: the free-trial daily ceiling applies to them."""
        return not self.has_paid


@dataclass(frozen=True)
class Reservation:
    reservation_id: uuid.UUID
    org_id: uuid.UUID
    user_id: uuid.UUID
    conversation_id: uuid.UUID
    message_id: uuid.UUID
    tokens_reserved: int
    overage_reserved: int = 0


@dataclass
class UsageRecord:
    """What happened in one assistant message, for the usage ledger."""

    billable_input_tokens: int
    billable_output_tokens: int
    model_name: str
    prompt_version: str
    billing_tokenizer_version: str
    host_prompt_tokens: int | None = None
    host_completion_tokens: int | None = None
    host_cached_prompt_tokens: int | None = None
    serving_host: str = "primary"
    steps: int = 0
    workspace_id: uuid.UUID | None = None
    client_id: str | None = None

    @property
    def billable_total(self) -> int:
        return self.billable_input_tokens + self.billable_output_tokens


@dataclass(frozen=True)
class ReverseResult:
    removed_tokens: int
    pending: bool
    tokens_already_used: int


class WalletError(Exception):
    pass


def _ids(res) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    return (res.org_id, res.user_id, res.conversation_id, res.message_id)


class WalletService:
    def __init__(
        self,
        engine: AsyncEngine,
        settings: Settings,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.engine = engine
        self.settings = settings
        self.clock = clock

    # ------------------------------------------------------------------ helpers

    @staticmethod
    async def _ensure_wallet(conn: AsyncConnection, org_id: uuid.UUID) -> None:
        await conn.execute(
            pg_insert(chat_wallets).values(org_id=org_id).on_conflict_do_nothing(index_elements=["org_id"])
        )

    async def _add_bucket(
        self,
        conn: AsyncConnection,
        org_id: uuid.UUID,
        source: str,
        tokens: int,
        expires_at: datetime | None,
        payment_id: uuid.UUID | None = None,
        granted_by: uuid.UUID | None = None,
        note: str | None = None,
    ) -> uuid.UUID:
        if tokens <= 0:
            raise WalletError("tokens must be positive")
        await self._ensure_wallet(conn, org_id)
        bucket_id = uuid.uuid4()
        await conn.execute(
            chat_credit_buckets.insert().values(
                bucket_id=bucket_id,
                org_id=org_id,
                source=source,
                tokens_granted=tokens,
                granted_at=self.clock(),
                expires_at=expires_at,
                payment_id=payment_id,
                granted_by=granted_by,
                note=note,
            )
        )
        await conn.execute(
            update(chat_wallets)
            .where(chat_wallets.c.org_id == org_id)
            .values(tokens_available=chat_wallets.c.tokens_available + tokens, updated_at=self.clock())
        )
        return bucket_id

    # ------------------------------------------------------------------ grants

    async def ensure_free_trial(self, org_id: uuid.UUID) -> bool:
        """Grant the one-time free trial. Returns True if granted now, False if it already existed."""
        now = self.clock()
        async with self.engine.begin() as conn:
            await self._ensure_wallet(conn, org_id)
            bucket_id = uuid.uuid4()
            result = await conn.execute(
                pg_insert(chat_credit_buckets)
                .values(
                    bucket_id=bucket_id,
                    org_id=org_id,
                    source="free_trial",
                    tokens_granted=self.settings.free_trial_tokens,
                    granted_at=now,
                    expires_at=now + timedelta(days=self.settings.free_trial_days),
                )
                .on_conflict_do_nothing(index_elements=["org_id"], index_where=text("source = 'free_trial'"))
                .returning(chat_credit_buckets.c.bucket_id)
            )
            if result.first() is None:
                return False
            await conn.execute(
                update(chat_wallets)
                .where(chat_wallets.c.org_id == org_id)
                .values(
                    tokens_available=chat_wallets.c.tokens_available + self.settings.free_trial_tokens,
                    updated_at=now,
                )
            )
            return True

    async def grant_goodwill(
        self, org_id: uuid.UUID, tokens: int, granted_by: uuid.UUID, note: str, valid_days: int = 90
    ) -> uuid.UUID:
        async with self.engine.begin() as conn:
            return await self._add_bucket(
                conn,
                org_id,
                "goodwill",
                tokens,
                self.clock() + timedelta(days=valid_days),
                granted_by=granted_by,
                note=note,
            )

    async def create_pending_payment(
        self,
        org_id: uuid.UUID,
        pack_id: str,
        provider: str,
        provider_payment_id: str,
        created_by: uuid.UUID | None,
    ) -> uuid.UUID:
        async with self.engine.begin() as conn:
            pack = (
                await conn.execute(
                    select(chat_token_packs).where(
                        chat_token_packs.c.pack_id == pack_id, chat_token_packs.c.active.is_(True)
                    )
                )
            ).first()
            if pack is None:
                raise WalletError(f"unknown or inactive pack {pack_id!r}")
            payment_id = uuid.uuid4()
            await conn.execute(
                chat_payments.insert().values(
                    id=payment_id,
                    org_id=org_id,
                    provider=provider,
                    provider_payment_id=provider_payment_id,
                    pack_id=pack_id,
                    amount_minor=pack.price_minor,
                    currency=pack.currency,
                    status="pending",
                    created_by=created_by,
                )
            )
            return payment_id

    async def credit_purchase(self, provider_payment_id: str) -> uuid.UUID:
        """Mark a payment paid and credit its pack. Idempotent on ``provider_payment_id``."""
        async with self.engine.begin() as conn:
            payment = (
                await conn.execute(
                    select(chat_payments)
                    .where(chat_payments.c.provider_payment_id == provider_payment_id)
                    .with_for_update()
                )
            ).first()
            if payment is None:
                raise WalletError("unknown payment")
            if payment.status == "paid":
                existing = (
                    await conn.execute(
                        select(chat_credit_buckets.c.bucket_id).where(chat_credit_buckets.c.payment_id == payment.id)
                    )
                ).scalar_one()
                return existing
            if payment.status != "pending":
                raise WalletError(f"payment in status {payment.status!r} can't be credited")
            pack = (
                await conn.execute(select(chat_token_packs).where(chat_token_packs.c.pack_id == payment.pack_id))
            ).one()
            bucket_id = await self._add_bucket(
                conn,
                payment.org_id,
                "pack",
                pack.tokens,
                self.clock() + timedelta(days=pack.validity_days),
                payment_id=payment.id,
            )
            await conn.execute(update(chat_payments).where(chat_payments.c.id == payment.id).values(status="paid"))
            return bucket_id

    async def list_payments(
        self, org_id: uuid.UUID | None = None, status: str | None = None, limit: int = 100
    ) -> list[dict]:
        """Pack orders, newest first (all orgs when ``org_id`` is None: staff view)."""
        q = select(
            chat_payments.c.provider_payment_id.label("reference"),
            chat_payments.c.org_id,
            chat_payments.c.provider,
            chat_payments.c.pack_id,
            chat_payments.c.amount_minor,
            chat_payments.c.currency,
            chat_payments.c.status,
            chat_payments.c.created_at,
        ).order_by(chat_payments.c.created_at.desc())
        if org_id is not None:
            q = q.where(chat_payments.c.org_id == org_id)
        if status is not None:
            q = q.where(chat_payments.c.status == status)
        async with self.engine.connect() as conn:
            rows = (await conn.execute(q.limit(limit))).all()
        return [dict(r._mapping) | {"org_id": str(r.org_id)} for r in rows]

    async def payment_provider_of(self, provider_payment_id: str) -> str | None:
        async with self.engine.connect() as conn:
            return (
                await conn.execute(
                    select(chat_payments.c.provider).where(chat_payments.c.provider_payment_id == provider_payment_id)
                )
            ).scalar_one_or_none()

    async def cancel_pending_payment(self, provider_payment_id: str) -> None:
        """An order that won't be paid (manual billing). Only pending orders can be cancelled."""
        async with self.engine.begin() as conn:
            result = await conn.execute(
                update(chat_payments)
                .where(chat_payments.c.provider_payment_id == provider_payment_id, chat_payments.c.status == "pending")
                .values(status="failed")
            )
        if result.rowcount != 1:
            raise WalletError("no pending order with that reference")

    async def reverse_purchase(self, provider_payment_id: str, new_status: str = "refunded") -> ReverseResult:
        """Refund/chargeback: remove the pack's remaining tokens without ever going negative.

        If in-flight reservations block full removal, the result is ``pending``. Call again (the
        expiry job does this via ``process_pending_reversals``) once reservations settle.
        """
        if new_status not in ("refunded", "disputed"):
            raise WalletError("new_status must be 'refunded' or 'disputed'")
        async with self.engine.begin() as conn:
            payment = (
                await conn.execute(
                    select(chat_payments)
                    .where(chat_payments.c.provider_payment_id == provider_payment_id)
                    .with_for_update()
                )
            ).first()
            if payment is None:
                raise WalletError("unknown payment")
            if payment.status not in ("paid", "refunded", "disputed"):
                raise WalletError(f"payment in status {payment.status!r} can't be reversed")
            await conn.execute(update(chat_payments).where(chat_payments.c.id == payment.id).values(status=new_status))
            wallet = (
                await conn.execute(
                    select(chat_wallets).where(chat_wallets.c.org_id == payment.org_id).with_for_update()
                )
            ).one()
            bucket = (
                await conn.execute(
                    select(chat_credit_buckets).where(chat_credit_buckets.c.payment_id == payment.id).with_for_update()
                )
            ).one()
            if bucket.status != "active":
                return ReverseResult(0, False, bucket.tokens_used)
            remaining = bucket.tokens_granted - bucket.tokens_used
            if wallet.tokens_available - remaining < wallet.tokens_reserved:
                return ReverseResult(0, True, bucket.tokens_used)
            await conn.execute(
                update(chat_credit_buckets)
                .where(chat_credit_buckets.c.bucket_id == bucket.bucket_id)
                .values(status="reversed")
            )
            await conn.execute(
                update(chat_wallets)
                .where(chat_wallets.c.org_id == payment.org_id)
                .values(tokens_available=chat_wallets.c.tokens_available - remaining, updated_at=self.clock())
            )
            return ReverseResult(remaining, False, bucket.tokens_used)

    # ------------------------------------------------------------------ balance

    async def get_balance(self, org_id: uuid.UUID) -> Balance:
        async with self.engine.connect() as conn:
            wallet = (await conn.execute(select(chat_wallets).where(chat_wallets.c.org_id == org_id))).first()
            rows = (
                await conn.execute(
                    select(chat_credit_buckets)
                    .where(chat_credit_buckets.c.org_id == org_id, chat_credit_buckets.c.status == "active")
                    .order_by(*_BUCKET_ORDER)
                )
            ).all()
            paid_buckets = (
                await conn.execute(
                    select(func.count()).where(
                        chat_credit_buckets.c.org_id == org_id,
                        chat_credit_buckets.c.source.in_(("pack", "plan_monthly")),
                    )
                )
            ).scalar_one()
            has_sub = (
                await conn.execute(select(func.count()).where(chat_subscriptions.c.org_id == org_id))
            ).scalar_one()
        buckets = [BucketView(r.bucket_id, r.source, r.tokens_granted - r.tokens_used, r.expires_at) for r in rows]
        has_paid = bool(paid_buckets or has_sub)
        if wallet is None:
            return Balance(org_id, 0, 0, buckets, has_paid)
        return Balance(org_id, wallet.tokens_available, wallet.tokens_reserved, buckets, has_paid)

    # ------------------------------------------------------------------ reserve / settle / release

    async def reserve(
        self,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        message_id: uuid.UUID,
        tokens: int,
    ) -> Reservation | None:
        """Atomically hold ``tokens``. Returns None if the org can't cover them."""
        if tokens <= 0:
            raise WalletError("tokens must be positive")
        now = self.clock()
        async with self.engine.begin() as conn:
            held = await conn.execute(
                update(chat_wallets)
                .where(
                    chat_wallets.c.org_id == org_id,
                    chat_wallets.c.tokens_available - chat_wallets.c.tokens_reserved >= tokens,
                )
                .values(tokens_reserved=chat_wallets.c.tokens_reserved + tokens, updated_at=now)
                .returning(chat_wallets.c.org_id)
            )
            if held.first() is not None:
                reservation_id = await self._insert_reservation(
                    conn, org_id, user_id, conversation_id, message_id, tokens, 0, now
                )
                return Reservation(reservation_id, org_id, user_id, conversation_id, message_id, tokens)
        return await self._reserve_with_overage(org_id, user_id, conversation_id, message_id, tokens, now)

    async def _insert_reservation(self, conn, org_id, user_id, conversation_id, message_id, tokens, overage, now):
        reservation_id = uuid.uuid4()
        await conn.execute(
            chat_reservations.insert().values(
                reservation_id=reservation_id,
                org_id=org_id,
                user_id=user_id,
                conversation_id=conversation_id,
                message_id=message_id,
                tokens_reserved=tokens,
                overage_reserved=overage,
                status="held",
                created_at=now,
                expires_at=now + timedelta(seconds=self.settings.reservation_ttl_seconds),
            )
        )
        return reservation_id

    async def _reserve_with_overage(self, org_id, user_id, conversation_id, message_id, tokens, now):
        """Buckets can't cover ``tokens``: use post-paid overage, up to the client's spend limit.

        Serialised per org by locking the subscription row, so parallel requests can't exceed the limit.
        """
        async with self.engine.begin() as conn:
            sub = (
                await conn.execute(
                    select(chat_subscriptions).where(chat_subscriptions.c.org_id == org_id).with_for_update()
                )
            ).first()
            if sub is None:
                return None
            plan = (await conn.execute(select(chat_plans).where(chat_plans.c.plan_id == sub.plan_id))).one()
            if not _overage_open(sub, plan):
                return None
            await self._ensure_wallet(conn, org_id)
            wallet = (
                await conn.execute(select(chat_wallets).where(chat_wallets.c.org_id == org_id).with_for_update())
            ).one()
            bucket_part = max(0, min(wallet.tokens_available - wallet.tokens_reserved, tokens))
            overage_part = tokens - bucket_part
            if overage_part > await self._overage_capacity(conn, org_id, sub, plan):
                return None
            if bucket_part:
                await conn.execute(
                    update(chat_wallets)
                    .where(chat_wallets.c.org_id == org_id)
                    .values(tokens_reserved=chat_wallets.c.tokens_reserved + bucket_part, updated_at=now)
                )
            reservation_id = await self._insert_reservation(
                conn, org_id, user_id, conversation_id, message_id, tokens, overage_part, now
            )
        return Reservation(reservation_id, org_id, user_id, conversation_id, message_id, tokens, overage_part)

    @staticmethod
    async def _overage_capacity(conn: AsyncConnection, org_id: uuid.UUID, sub, plan) -> int:
        """Overage tokens still available this period: spend limit − used − held by in-flight requests."""
        limit_tokens = sub.spend_limit_minor * 1_000_000 // plan.price_per_1m_tokens_minor
        used = (
            await conn.execute(
                select(func.coalesce(func.sum(chat_usage_events.c.overage_tokens), 0)).where(
                    chat_usage_events.c.org_id == org_id,
                    chat_usage_events.c.occurred_at >= sub.current_period_start,
                    chat_usage_events.c.occurred_at < sub.current_period_end,
                )
            )
        ).scalar_one()
        held = (
            await conn.execute(
                select(func.coalesce(func.sum(chat_reservations.c.overage_reserved), 0)).where(
                    chat_reservations.c.org_id == org_id, chat_reservations.c.status == "held"
                )
            )
        ).scalar_one()
        return max(0, int(limit_tokens) - int(used) - int(held))

    async def capacity(self, org_id: uuid.UUID) -> int:
        """Tokens the org can spend right now: bucket balance + remaining overage allowance."""
        bal = await self.get_balance(org_id)
        async with self.engine.connect() as conn:
            sub = (await conn.execute(select(chat_subscriptions).where(chat_subscriptions.c.org_id == org_id))).first()
            if sub is None:
                return bal.spendable
            plan = (await conn.execute(select(chat_plans).where(chat_plans.c.plan_id == sub.plan_id))).one()
            if not _overage_open(sub, plan):
                return bal.spendable
            return bal.spendable + await self._overage_capacity(conn, org_id, sub, plan)

    async def add_bucket_in(
        self, conn: AsyncConnection, org_id: uuid.UUID, source: str, tokens: int, expires_at: datetime | None
    ) -> uuid.UUID:
        """Add a credit bucket inside the caller's transaction (used by the subscription service)."""
        return await self._add_bucket(conn, org_id, source, tokens, expires_at)

    async def _lock_held(self, conn: AsyncConnection, reservation_id: uuid.UUID):
        return (
            await conn.execute(
                select(chat_reservations).where(chat_reservations.c.reservation_id == reservation_id).with_for_update()
            )
        ).one()

    def _usage_values(
        self,
        ids: tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID],
        usage: UsageRecord,
        charged: int,
        status: str,
        reason: str | None,
        overage_tokens: int = 0,
    ) -> dict:
        org_id, user_id, conversation_id, message_id = ids
        return dict(
            occurred_at=self.clock(),
            org_id=org_id,
            user_id=user_id,
            conversation_id=conversation_id,
            message_id=message_id,
            workspace_id=usage.workspace_id,
            client_id=usage.client_id,
            billable_input_tokens=usage.billable_input_tokens,
            billable_output_tokens=usage.billable_output_tokens,
            tokens_charged=charged,
            overage_tokens=overage_tokens,
            billing_tokenizer_version=usage.billing_tokenizer_version,
            host_prompt_tokens=usage.host_prompt_tokens,
            host_completion_tokens=usage.host_completion_tokens,
            host_cached_prompt_tokens=usage.host_cached_prompt_tokens,
            model_name=usage.model_name,
            serving_host=usage.serving_host,
            prompt_version=usage.prompt_version,
            steps=usage.steps,
            status=status,
            reason=reason,
        )

    async def settle(self, reservation_id: uuid.UUID, usage: UsageRecord) -> int:
        """Charge ``min(actual, reserved)``, release the rest, debit buckets in order. Idempotent."""
        async with self.engine.begin() as conn:
            res = await self._lock_held(conn, reservation_id)
            if res.status == "settled":
                existing = (
                    await conn.execute(
                        select(chat_usage_events.c.tokens_charged).where(
                            chat_usage_events.c.message_id == res.message_id
                        )
                    )
                ).scalar_one()
                return existing
            if res.status != "held":
                raise WalletError(f"reservation is {res.status}, can't settle")

            charge = max(0, min(usage.billable_total, res.tokens_reserved))
            bucket_part = res.tokens_reserved - res.overage_reserved
            bucket_charge = min(charge, bucket_part)
            overage_charge = charge - bucket_charge
            await conn.execute(
                update(chat_wallets)
                .where(chat_wallets.c.org_id == res.org_id)
                .values(
                    tokens_available=chat_wallets.c.tokens_available - bucket_charge,
                    tokens_reserved=chat_wallets.c.tokens_reserved - bucket_part,
                    updated_at=self.clock(),
                )
            )
            await self._debit_buckets(conn, res.org_id, res.message_id, bucket_charge)
            await conn.execute(
                chat_usage_events.insert().values(
                    **self._usage_values(_ids(res), usage, charge, "charged", None, overage_charge)
                )
            )
            await conn.execute(
                update(chat_reservations)
                .where(chat_reservations.c.reservation_id == reservation_id)
                .values(status="settled")
            )
            return charge

    async def _debit_buckets(
        self, conn: AsyncConnection, org_id: uuid.UUID, message_id: uuid.UUID, charge: int
    ) -> None:
        if charge == 0:
            return
        buckets = (
            await conn.execute(
                select(chat_credit_buckets)
                .where(chat_credit_buckets.c.org_id == org_id, chat_credit_buckets.c.status == "active")
                .order_by(*_BUCKET_ORDER)
                .with_for_update()
            )
        ).all()
        left = charge
        for b in buckets:
            if left == 0:
                break
            take = min(left, b.tokens_granted - b.tokens_used)
            if take <= 0:
                continue
            new_used = b.tokens_used + take
            await conn.execute(
                update(chat_credit_buckets)
                .where(chat_credit_buckets.c.bucket_id == b.bucket_id)
                .values(
                    tokens_used=new_used,
                    status="exhausted" if new_used == b.tokens_granted else "active",
                )
            )
            await conn.execute(
                chat_bucket_debits.insert().values(message_id=message_id, bucket_id=b.bucket_id, tokens=take)
            )
            left -= take
        if left:
            # Can't happen while the invariant holds; abort the transaction rather than lose money.
            raise WalletError("bucket balances don't cover the charge (invariant broken)")

    async def release(self, reservation_id: uuid.UUID, usage: UsageRecord, reason: str) -> None:
        """Release a reservation without charging. Writes a ``not_charged`` usage event. Idempotent."""
        async with self.engine.begin() as conn:
            res = await self._lock_held(conn, reservation_id)
            if res.status in ("released", "expired"):
                return
            if res.status != "held":
                raise WalletError(f"reservation is {res.status}, can't release")
            await conn.execute(
                update(chat_wallets)
                .where(chat_wallets.c.org_id == res.org_id)
                .values(
                    tokens_reserved=chat_wallets.c.tokens_reserved - (res.tokens_reserved - res.overage_reserved),
                    updated_at=self.clock(),
                )
            )
            await conn.execute(
                pg_insert(chat_usage_events)
                .values(**self._usage_values(_ids(res), usage, 0, "not_charged", reason))
                .on_conflict_do_nothing(index_elements=["message_id"])
            )
            await conn.execute(
                update(chat_reservations)
                .where(chat_reservations.c.reservation_id == reservation_id)
                .values(status="released")
            )

    async def record_shadow(
        self,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        message_id: uuid.UUID,
        usage: UsageRecord,
        charged: bool,
        reason: str | None = None,
    ) -> int:
        """Shadow billing: record what would have been charged; never touch the wallet."""
        would_charge = usage.billable_total if charged else 0
        ids = (org_id, user_id, conversation_id, message_id)
        async with self.engine.begin() as conn:
            await conn.execute(
                pg_insert(chat_usage_events)
                .values(**self._usage_values(ids, usage, would_charge, "shadow", reason))
                .on_conflict_do_nothing(index_elements=["message_id"])
            )
        return would_charge

    # ------------------------------------------------------------------ scheduled jobs

    async def expire_reservations(self) -> int:
        """Release held reservations past their TTL (requests that died). Returns the count."""
        now = self.clock()
        async with self.engine.connect() as conn:
            ids = (
                (
                    await conn.execute(
                        select(chat_reservations.c.reservation_id).where(
                            chat_reservations.c.status == "held", chat_reservations.c.expires_at <= now
                        )
                    )
                )
                .scalars()
                .all()
            )
        count = 0
        for rid in ids:
            async with self.engine.begin() as conn:
                res = await self._lock_held(conn, rid)
                if res.status != "held":
                    continue
                await conn.execute(
                    update(chat_wallets)
                    .where(chat_wallets.c.org_id == res.org_id)
                    .values(
                        tokens_reserved=chat_wallets.c.tokens_reserved - (res.tokens_reserved - res.overage_reserved)
                    )
                )
                await conn.execute(
                    update(chat_reservations).where(chat_reservations.c.reservation_id == rid).values(status="expired")
                )
                count += 1
        return count

    async def expire_buckets(self) -> int:
        """Expire buckets past ``expires_at``; skip any that in-flight reservations still depend on."""
        now = self.clock()
        async with self.engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(chat_credit_buckets.c.bucket_id, chat_credit_buckets.c.org_id).where(
                        chat_credit_buckets.c.status == "active",
                        chat_credit_buckets.c.expires_at.is_not(None),
                        chat_credit_buckets.c.expires_at <= now,
                    )
                )
            ).all()
        count = 0
        for bucket_id, org_id in rows:
            async with self.engine.begin() as conn:
                wallet = (
                    await conn.execute(select(chat_wallets).where(chat_wallets.c.org_id == org_id).with_for_update())
                ).one()
                b = (
                    await conn.execute(
                        select(chat_credit_buckets)
                        .where(chat_credit_buckets.c.bucket_id == bucket_id)
                        .with_for_update()
                    )
                ).one()
                if b.status != "active":
                    continue
                remaining = b.tokens_granted - b.tokens_used
                if wallet.tokens_available - remaining < wallet.tokens_reserved:
                    continue  # retry next run, after in-flight reservations settle
                await conn.execute(
                    update(chat_credit_buckets)
                    .where(chat_credit_buckets.c.bucket_id == bucket_id)
                    .values(status="expired")
                )
                await conn.execute(
                    update(chat_wallets)
                    .where(chat_wallets.c.org_id == org_id)
                    .values(tokens_available=chat_wallets.c.tokens_available - remaining, updated_at=now)
                )
                count += 1
        return count

    async def process_pending_reversals(self) -> int:
        """Retry refunds/chargebacks whose tokens couldn't be removed because of in-flight reservations."""
        async with self.engine.connect() as conn:
            pids = (
                await conn.execute(
                    select(chat_payments.c.provider_payment_id, chat_payments.c.status)
                    .join(chat_credit_buckets, chat_credit_buckets.c.payment_id == chat_payments.c.id)
                    .where(
                        chat_payments.c.status.in_(("refunded", "disputed")),
                        chat_credit_buckets.c.status == "active",
                    )
                )
            ).all()
        done = 0
        for pid, status in pids:
            result = await self.reverse_purchase(pid, status)
            if not result.pending:
                done += 1
        return done

    # ------------------------------------------------------------------ usage queries & checks

    async def org_tokens_since(self, org_id: uuid.UUID, since: datetime) -> int:
        """Billable tokens counted today (charged + shadow), for daily ceilings."""
        async with self.engine.connect() as conn:
            value = (
                await conn.execute(
                    select(func.coalesce(func.sum(chat_usage_events.c.tokens_charged), 0)).where(
                        chat_usage_events.c.org_id == org_id,
                        chat_usage_events.c.occurred_at >= since,
                        chat_usage_events.c.status.in_(("charged", "shadow")),
                    )
                )
            ).scalar_one()
        return int(value)

    async def global_host_tokens_since(self, since: datetime) -> int:
        """Host tokens across all orgs since ``since``, for the global cost ceiling."""
        async with self.engine.connect() as conn:
            value = (
                await conn.execute(
                    select(
                        func.coalesce(
                            func.sum(
                                func.coalesce(chat_usage_events.c.host_prompt_tokens, 0)
                                + func.coalesce(chat_usage_events.c.host_completion_tokens, 0)
                            ),
                            0,
                        )
                    ).where(chat_usage_events.c.occurred_at >= since)
                )
            ).scalar_one()
        return int(value)

    async def check_invariant(self, org_id: uuid.UUID) -> bool:
        async with self.engine.connect() as conn:
            available = (
                await conn.execute(select(chat_wallets.c.tokens_available).where(chat_wallets.c.org_id == org_id))
            ).scalar_one_or_none() or 0
            remaining = (
                await conn.execute(
                    select(
                        func.coalesce(
                            func.sum(chat_credit_buckets.c.tokens_granted - chat_credit_buckets.c.tokens_used), 0
                        )
                    ).where(and_(chat_credit_buckets.c.org_id == org_id, chat_credit_buckets.c.status == "active"))
                )
            ).scalar_one()
        return int(available) == int(remaining)


def _overage_open(sub, plan) -> bool:
    return (
        sub.status == "active"
        and plan.overage_allowed
        and sub.spend_limit_minor is not None
        and sub.spend_limit_minor > 0
        and bool(plan.price_per_1m_tokens_minor)
    )

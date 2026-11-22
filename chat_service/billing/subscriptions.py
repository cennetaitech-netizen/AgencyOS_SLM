"""Subscription plans: monthly allowances, upgrades/downgrades, cycle close and dunning (Phase 10).

Consumption order (wallet): free trial → goodwill → plan monthly allowance → packs → overage.
Overage is post-paid and stops at the client's spend limit (see WalletService._reserve_with_overage).
"""

from __future__ import annotations

import calendar
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from chat_service.billing.invoices import InvoiceService, Line, overage_amount_minor
from chat_service.billing.wallet import WalletService
from chat_service.config import Settings
from chat_service.db.schema import chat_plans, chat_subscriptions, chat_usage_events


class SubscriptionError(Exception):
    pass


def add_month(dt: datetime) -> datetime:
    """Same day next month, clamped to the month's last day (31 Jan → 28/29 Feb)."""
    year, month = (dt.year + 1, 1) if dt.month == 12 else (dt.year, dt.month + 1)
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


@dataclass(frozen=True)
class SubscriptionView:
    org_id: uuid.UUID
    plan_id: str
    plan_name: str
    status: str
    current_period_start: datetime
    current_period_end: datetime
    spend_limit_minor: int | None
    pending_plan_id: str | None
    currency: str
    monthly_fee_minor: int
    monthly_included_tokens: int
    overage_allowed: bool
    price_per_1m_tokens_minor: int | None


class SubscriptionService:
    def __init__(
        self,
        engine: AsyncEngine,
        settings: Settings,
        wallet: WalletService,
        invoices: InvoiceService,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.engine = engine
        self.settings = settings
        self.wallet = wallet
        self.invoices = invoices
        self.clock = clock

    # ------------------------------------------------------------------ plan catalogue (staff)

    async def upsert_plan(self, **plan: Any) -> None:
        if plan.get("overage_allowed") and not plan.get("price_per_1m_tokens_minor"):
            raise SubscriptionError("plans with overage need price_per_1m_tokens_minor")
        async with self.engine.begin() as conn:
            stmt = pg_insert(chat_plans).values(**plan)
            await conn.execute(
                stmt.on_conflict_do_update(
                    index_elements=["plan_id"], set_={k: v for k, v in plan.items() if k != "plan_id"}
                )
            )

    async def list_plans(self) -> list[dict[str, Any]]:
        async with self.engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(chat_plans).where(chat_plans.c.active.is_(True)).order_by(chat_plans.c.monthly_fee_minor)
                )
            ).all()
        return [dict(r._mapping) for r in rows]

    async def _plan(self, conn, plan_id: str):
        plan = (await conn.execute(select(chat_plans).where(chat_plans.c.plan_id == plan_id))).first()
        if plan is None or not plan.active:
            raise SubscriptionError(f"unknown or inactive plan {plan_id!r}")
        return plan

    # ------------------------------------------------------------------ read

    async def get(self, org_id: uuid.UUID) -> SubscriptionView | None:
        async with self.engine.connect() as conn:
            row = (
                await conn.execute(
                    select(chat_subscriptions, chat_plans)
                    .join(chat_plans, chat_plans.c.plan_id == chat_subscriptions.c.plan_id)
                    .where(chat_subscriptions.c.org_id == org_id)
                )
            ).first()
        if row is None:
            return None
        return SubscriptionView(
            row.org_id,
            row.plan_id,
            row.name,
            row.status,
            row.current_period_start,
            row.current_period_end,
            row.spend_limit_minor,
            row.pending_plan_id,
            row.currency,
            row.monthly_fee_minor,
            row.monthly_included_tokens,
            row.overage_allowed,
            row.price_per_1m_tokens_minor,
        )

    # ------------------------------------------------------------------ lifecycle

    async def subscribe(
        self, org_id: uuid.UUID, plan_id: str, spend_limit_minor: int | None = None
    ) -> uuid.UUID | None:
        """Start a subscription now: allowance for this period + plan-fee invoice (in advance)."""
        now = self.clock()
        async with self.engine.begin() as conn:
            plan = await self._plan(conn, plan_id)
            existing = (
                await conn.execute(
                    select(chat_subscriptions).where(chat_subscriptions.c.org_id == org_id).with_for_update()
                )
            ).first()
            if existing is not None and existing.status != "cancelled":
                raise SubscriptionError("organisation already has a subscription; change the plan instead")
            end = add_month(now)
            values = {
                "plan_id": plan_id,
                "current_period_start": now,
                "current_period_end": end,
                "spend_limit_minor": spend_limit_minor,
                "status": "active",
                "pending_plan_id": None,
                "past_due_since": None,
            }
            await conn.execute(
                pg_insert(chat_subscriptions)
                .values(org_id=org_id, **values)
                .on_conflict_do_update(index_elements=["org_id"], set_=values)
            )
            if plan.monthly_included_tokens > 0:
                await self.wallet.add_bucket_in(conn, org_id, "plan_monthly", plan.monthly_included_tokens, end)
            invoice_id = await self.invoices.create_in(
                conn,
                org_id,
                "subscription_start",
                now,
                end,
                plan.currency,
                [
                    Line(
                        "plan_fee",
                        f"{plan.name} plan, {now:%d %b %Y} – {end:%d %b %Y}",
                        1,
                        plan.monthly_fee_minor,
                        plan.monthly_fee_minor,
                    )
                ],
            )
        await self.invoices.auto_send_if_allowed(invoice_id)
        return invoice_id

    async def change_plan(self, org_id: uuid.UUID, new_plan_id: str) -> dict[str, Any]:
        """Upgrade: immediate, prorated fee + prorated extra allowance. Downgrade: at the next renewal."""
        now = self.clock()
        async with self.engine.begin() as conn:
            sub = (
                await conn.execute(
                    select(chat_subscriptions).where(chat_subscriptions.c.org_id == org_id).with_for_update()
                )
            ).first()
            if sub is None or sub.status in ("cancelled", "suspended"):
                raise SubscriptionError("no active subscription")
            old, new = await self._plan(conn, sub.plan_id), await self._plan(conn, new_plan_id)
            if new.currency != old.currency:
                raise SubscriptionError("plan currency can't change mid-subscription")
            if new.plan_id == old.plan_id:
                await conn.execute(
                    update(chat_subscriptions).where(chat_subscriptions.c.org_id == org_id).values(pending_plan_id=None)
                )
                return {"change": "none"}
            if new.monthly_fee_minor < old.monthly_fee_minor:
                await conn.execute(
                    update(chat_subscriptions)
                    .where(chat_subscriptions.c.org_id == org_id)
                    .values(pending_plan_id=new.plan_id, updated_at=now)
                )
                return {"change": "downgrade_scheduled", "effective": sub.current_period_end}
            length = (sub.current_period_end - sub.current_period_start).total_seconds()
            remaining = max(0.0, (sub.current_period_end - now).total_seconds())
            fraction = Decimal(str(remaining / length)) if length else Decimal(0)
            fee = int(((new.monthly_fee_minor - old.monthly_fee_minor) * fraction).quantize(0, ROUND_HALF_UP))
            extra = int(
                (max(0, new.monthly_included_tokens - old.monthly_included_tokens) * fraction).quantize(
                    0, ROUND_HALF_UP
                )
            )
            await conn.execute(
                update(chat_subscriptions)
                .where(chat_subscriptions.c.org_id == org_id)
                .values(plan_id=new.plan_id, pending_plan_id=None, updated_at=now)
            )
            if extra > 0:
                await self.wallet.add_bucket_in(conn, org_id, "plan_monthly", extra, sub.current_period_end)
            invoice_id = await self.invoices.create_in(
                conn,
                org_id,
                "proration",
                now,
                sub.current_period_end,
                new.currency,
                [
                    Line(
                        "proration",
                        f"Upgrade {old.name} → {new.name}, prorated to {sub.current_period_end:%d %b %Y}",
                        1,
                        fee,
                        fee,
                    )
                ],
            )
        await self.invoices.auto_send_if_allowed(invoice_id)
        return {
            "change": "upgraded",
            "prorated_fee_minor": fee,
            "extra_tokens": extra,
            "invoice_id": str(invoice_id) if invoice_id else None,
        }

    async def set_spend_limit(self, org_id: uuid.UUID, spend_limit_minor: int | None) -> None:
        if spend_limit_minor is not None and spend_limit_minor < 0:
            raise SubscriptionError("spend limit can't be negative")
        async with self.engine.begin() as conn:
            result = await conn.execute(
                update(chat_subscriptions)
                .where(chat_subscriptions.c.org_id == org_id)
                .values(spend_limit_minor=spend_limit_minor, updated_at=self.clock())
            )
        if result.rowcount == 0:
            raise SubscriptionError("no subscription")

    # ------------------------------------------------------------------ cycle close

    async def close_due_cycles(self) -> list[uuid.UUID]:
        now = self.clock()
        async with self.engine.connect() as conn:
            orgs = (
                (
                    await conn.execute(
                        select(chat_subscriptions.c.org_id).where(
                            chat_subscriptions.c.current_period_end <= now,
                            chat_subscriptions.c.status.in_(("active", "past_due")),
                        )
                    )
                )
                .scalars()
                .all()
            )
        invoices = []
        for org_id in orgs:
            while True:  # catch up if several periods elapsed (e.g. the job was down)
                invoice_id, more = await self._close_one(org_id, now)
                if invoice_id:
                    invoices.append(invoice_id)
                if not more:
                    break
        return invoices

    async def _close_one(self, org_id: uuid.UUID, now: datetime) -> tuple[uuid.UUID | None, bool]:
        async with self.engine.begin() as conn:
            sub = (
                await conn.execute(
                    select(chat_subscriptions).where(chat_subscriptions.c.org_id == org_id).with_for_update()
                )
            ).one()
            if sub.current_period_end > now or sub.status not in ("active", "past_due"):
                return None, False
            plan = await self._plan(conn, sub.plan_id)
            next_plan = await self._plan(conn, sub.pending_plan_id) if sub.pending_plan_id else plan
            start, end = sub.current_period_start, sub.current_period_end
            next_end = add_month(end)

            in_period = (
                chat_usage_events.c.org_id == org_id,
                chat_usage_events.c.occurred_at >= start,
                chat_usage_events.c.occurred_at < end,
            )
            overage_tokens = int(
                (
                    await conn.execute(
                        select(func.coalesce(func.sum(chat_usage_events.c.overage_tokens), 0)).where(*in_period)
                    )
                ).scalar_one()
            )
            lines = [
                Line(
                    "plan_fee",
                    f"{next_plan.name} plan, {end:%d %b %Y} – {next_end:%d %b %Y}",
                    1,
                    next_plan.monthly_fee_minor,
                    next_plan.monthly_fee_minor,
                )
            ]
            if overage_tokens:
                price = plan.price_per_1m_tokens_minor or 0
                by_brand = (
                    await conn.execute(
                        select(chat_usage_events.c.client_id, func.sum(chat_usage_events.c.overage_tokens))
                        .where(*in_period, chat_usage_events.c.overage_tokens > 0)
                        .group_by(chat_usage_events.c.client_id)
                    )
                ).all()
                lines.append(
                    Line(
                        "overage",
                        f"Overage: {overage_tokens:,} billable tokens, {start:%d %b} – {end:%d %b %Y}",
                        overage_tokens,
                        overage_amount_minor(overage_tokens, price),
                        price,
                        {
                            "by_client": {(c or "unassigned"): int(t) for c, t in by_brand},
                            "unit": "per 1M billable tokens",
                        },
                    )
                )
            invoice_id = await self.invoices.create_in(
                conn, org_id, "cycle", start, end, plan.currency, lines, overage_tokens
            )
            await conn.execute(
                update(chat_subscriptions)
                .where(chat_subscriptions.c.org_id == org_id)
                .values(
                    plan_id=next_plan.plan_id,
                    pending_plan_id=None,
                    current_period_start=end,
                    current_period_end=next_end,
                    updated_at=now,
                )
            )
            if next_plan.monthly_included_tokens > 0:
                await self.wallet.add_bucket_in(
                    conn, org_id, "plan_monthly", next_plan.monthly_included_tokens, next_end
                )
        if invoice_id:
            await self.invoices.reconcile(invoice_id)
            await self.invoices.auto_send_if_allowed(invoice_id)
        return invoice_id, next_end <= now

    # ------------------------------------------------------------------ dunning

    async def payment_failed(self, org_id: uuid.UUID) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                update(chat_subscriptions)
                .where(chat_subscriptions.c.org_id == org_id, chat_subscriptions.c.status == "active")
                .values(status="past_due", past_due_since=self.clock(), updated_at=self.clock())
            )

    async def payment_succeeded(self, org_id: uuid.UUID) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                update(chat_subscriptions)
                .where(
                    chat_subscriptions.c.org_id == org_id, chat_subscriptions.c.status.in_(("past_due", "suspended"))
                )
                .values(status="active", past_due_since=None, updated_at=self.clock())
            )

    async def run_dunning(self) -> int:
        """Past due beyond the grace period → suspended."""
        cutoff = self.clock() - timedelta(days=self.settings.dunning_grace_days)
        async with self.engine.begin() as conn:
            result = await conn.execute(
                update(chat_subscriptions)
                .where(chat_subscriptions.c.status == "past_due", chat_subscriptions.c.past_due_since <= cutoff)
                .values(status="suspended", updated_at=self.clock())
            )
        return result.rowcount

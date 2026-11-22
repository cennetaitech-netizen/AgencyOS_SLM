"""Usage caps (org / workspace / user, per day or month) and threshold alerts (50/80/90/100%).

Caps are token governance set by client admins. They're checked before each message and limit the reply
size. With parallel messages a cap can be exceeded by at most the in-flight replies (bounded by
``max_concurrent_streams_per_org × max_output_tokens``). Money limits (the spend limit) are enforced
atomically in the wallet instead.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from chat_service.db.schema import chat_credit_buckets, chat_usage_alerts, chat_usage_caps, chat_usage_events

log = logging.getLogger("chat_service.alerts")


class Notifier(Protocol):
    async def notify(self, org_id: uuid.UUID, alert: dict[str, Any]) -> None: ...


class LogNotifier:
    """Replace with the product's email/in-app notification system (Phase 6 task 7)."""

    def __init__(self) -> None:
        self.sent: list[tuple[uuid.UUID, dict[str, Any]]] = []

    async def notify(self, org_id: uuid.UUID, alert: dict[str, Any]) -> None:
        self.sent.append((org_id, alert))
        log.info("usage alert org=%s %s", org_id, alert)


@dataclass(frozen=True)
class CapStatus:
    remaining: int | None  # None = no hard cap applies
    blocking_cap: dict[str, Any] | None


class CapsService:
    def __init__(
        self,
        engine: AsyncEngine,
        notifier: Notifier | None = None,
        thresholds: tuple[int, ...] = (50, 80, 90, 100),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.engine = engine
        self.notifier = notifier or LogNotifier()
        self.thresholds = thresholds
        self.clock = clock

    # ------------------------------------------------------------------ CRUD

    async def set_cap(
        self, org_id: uuid.UUID, scope_type: str, scope_id: uuid.UUID, period: str, max_tokens: int, hard: bool = True
    ) -> uuid.UUID:
        if scope_type not in ("org", "workspace", "user") or period not in ("day", "month") or max_tokens <= 0:
            raise ValueError("invalid cap")
        if scope_type == "org" and scope_id != org_id:
            raise ValueError("org caps must use the org id as scope_id")
        cap_id = uuid.uuid4()
        async with self.engine.begin() as conn:
            await conn.execute(
                delete(chat_usage_caps).where(
                    chat_usage_caps.c.org_id == org_id,
                    chat_usage_caps.c.scope_type == scope_type,
                    chat_usage_caps.c.scope_id == scope_id,
                    chat_usage_caps.c.period == period,
                )
            )
            await conn.execute(
                chat_usage_caps.insert().values(
                    cap_id=cap_id,
                    org_id=org_id,
                    scope_type=scope_type,
                    scope_id=scope_id,
                    period=period,
                    max_tokens=max_tokens,
                    hard=hard,
                )
            )
        return cap_id

    async def delete_cap(self, org_id: uuid.UUID, cap_id: uuid.UUID) -> bool:
        async with self.engine.begin() as conn:
            result = await conn.execute(
                delete(chat_usage_caps).where(chat_usage_caps.c.org_id == org_id, chat_usage_caps.c.cap_id == cap_id)
            )
        return result.rowcount > 0

    async def list_caps(self, org_id: uuid.UUID) -> list[dict[str, Any]]:
        async with self.engine.connect() as conn:
            rows = (await conn.execute(select(chat_usage_caps).where(chat_usage_caps.c.org_id == org_id))).all()
        return [dict(r._mapping) for r in rows]

    # ------------------------------------------------------------------ evaluation

    def _period_start(self, period: str, tz: str) -> tuple[datetime, str]:
        local = self.clock().astimezone(ZoneInfo(tz))
        if period == "day":
            start = local.replace(hour=0, minute=0, second=0, microsecond=0)
            return start.astimezone(UTC), start.strftime("%Y-%m-%d")
        start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return start.astimezone(UTC), start.strftime("%Y-%m")

    async def _applicable(self, org_id, user_id, workspace_id) -> list[Any]:
        scope_ids = [org_id, user_id] + ([workspace_id] if workspace_id else [])
        async with self.engine.connect() as conn:
            return (
                await conn.execute(
                    select(chat_usage_caps).where(
                        chat_usage_caps.c.org_id == org_id, chat_usage_caps.c.scope_id.in_(scope_ids)
                    )
                )
            ).all()

    async def _used(self, cap, since: datetime) -> int:
        col = {
            "org": chat_usage_events.c.org_id,
            "user": chat_usage_events.c.user_id,
            "workspace": chat_usage_events.c.workspace_id,
        }[cap.scope_type]
        async with self.engine.connect() as conn:
            value = (
                await conn.execute(
                    select(func.coalesce(func.sum(chat_usage_events.c.tokens_charged), 0)).where(
                        chat_usage_events.c.org_id == cap.org_id,
                        col == cap.scope_id,
                        chat_usage_events.c.occurred_at >= since,
                        chat_usage_events.c.status.in_(("charged", "shadow")),
                    )
                )
            ).scalar_one()
        return int(value)

    async def check(self, org_id: uuid.UUID, user_id: uuid.UUID, workspace_id: uuid.UUID | None, tz: str) -> CapStatus:
        remaining: int | None = None
        blocking = None
        for cap in await self._applicable(org_id, user_id, workspace_id):
            if not cap.hard:
                continue
            since, _ = self._period_start(cap.period, tz)
            left = cap.max_tokens - await self._used(cap, since)
            if remaining is None or left < remaining:
                remaining = left
                blocking = {
                    "cap_id": str(cap.cap_id),
                    "scope_type": cap.scope_type,
                    "period": cap.period,
                    "max_tokens": cap.max_tokens,
                }
        return CapStatus(remaining, blocking)

    async def evaluate_alerts(
        self, org_id: uuid.UUID, user_id: uuid.UUID, workspace_id: uuid.UUID | None, tz: str
    ) -> list[dict[str, Any]]:
        """Record and send alerts for any threshold newly crossed (each fires once per period)."""
        candidates: list[tuple[str, str, int, int]] = []  # scope, period_key, used, limit
        for cap in await self._applicable(org_id, user_id, workspace_id):
            since, key = self._period_start(cap.period, tz)
            candidates.append((f"cap:{cap.scope_type}:{cap.cap_id}", key, await self._used(cap, since), cap.max_tokens))
        async with self.engine.connect() as conn:
            plan_buckets = (
                await conn.execute(
                    select(chat_credit_buckets).where(
                        chat_credit_buckets.c.org_id == org_id,
                        chat_credit_buckets.c.source.in_(("plan_monthly", "free_trial")),
                        chat_credit_buckets.c.status.in_(("active", "exhausted")),
                    )
                )
            ).all()
        for b in plan_buckets:
            key = b.expires_at.strftime("%Y-%m-%d") if b.expires_at else "none"
            candidates.append((f"allowance:{b.source}", key, b.tokens_used, b.tokens_granted))

        new_alerts = []
        for scope, key, used, limit in candidates:
            if limit <= 0:
                continue
            pct = used * 100 / limit
            crossed = [t for t in self.thresholds if pct >= t]
            if not crossed:
                continue
            top = max(crossed)
            async with self.engine.begin() as conn:
                inserted = (
                    (
                        await conn.execute(
                            pg_insert(chat_usage_alerts)
                            .values(
                                [{"org_id": org_id, "scope": scope, "period_key": key, "threshold": t} for t in crossed]
                            )
                            .on_conflict_do_nothing(constraint="uq_chat_usage_alerts_once")
                            .returning(chat_usage_alerts.c.threshold)
                        )
                    )
                    .scalars()
                    .all()
                )
            if top in inserted:  # only notify for the highest newly crossed threshold
                alert = {"scope": scope, "period": key, "threshold": top, "used": used, "limit": limit}
                new_alerts.append(alert)
                await self.notifier.notify(org_id, alert)
        return new_alerts

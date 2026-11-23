"""Model rollout (Phase 11): production model + optional canary, per-org pinning, automatic rollback.

* Orgs are assigned to the canary by a stable hash bucket (0–99) < ``canary_percent``, so each org always sees
  the same model during a rollout.
* ``chat_org_settings.model_pin`` overrides everything (e.g. keep a sensitive client on the proven model).
* ``evaluate_canary`` compares the canary with production over the current rollout window, and rolls back
  automatically if the error rate or 👎 rate is worse beyond the configured margins.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from chat_service.config import Settings
from chat_service.db.schema import chat_feedback, chat_messages, chat_model_rollout, chat_usage_events
from chat_service.llm.client import ModelClient

log = logging.getLogger("chat_service.rollout")
_ROW = "default"


@dataclass(frozen=True)
class RolloutState:
    production_model: str
    canary_model: str | None
    canary_percent: int
    status: str
    updated_at: datetime | None


def org_bucket(org_id: uuid.UUID) -> int:
    return int(hashlib.sha256(org_id.bytes).hexdigest()[:8], 16) % 100


class RolloutError(Exception):
    pass


class RolloutStore:
    """Rollout state, transitions and the automatic rollback check (no model clients needed)."""

    def __init__(
        self,
        engine: AsyncEngine,
        default_model: str,
        known_models: set[str] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.engine = engine
        self.default_model = default_model
        self.known_models = known_models
        self.clock = clock

    async def state(self) -> RolloutState:
        async with self.engine.connect() as conn:
            row = (await conn.execute(select(chat_model_rollout).where(chat_model_rollout.c.id == _ROW))).first()
        if row is None:
            return RolloutState(self.default_model, None, 0, "idle", None)
        return RolloutState(row.production_model, row.canary_model, row.canary_percent, row.status, row.updated_at)

    async def _save(self, **values: Any) -> RolloutState:
        st = await self.state()
        row = {
            "production_model": st.production_model,
            "canary_model": st.canary_model,
            "canary_percent": st.canary_percent,
            "status": st.status,
            "note": None,
            **values,
            "updated_at": self.clock(),
        }
        async with self.engine.begin() as conn:
            await conn.execute(
                pg_insert(chat_model_rollout)
                .values(id=_ROW, **row)
                .on_conflict_do_update(index_elements=["id"], set_=row)
            )
        return await self.state()

    async def start_canary(self, model: str, percent: int) -> RolloutState:
        if self.known_models is not None and model not in self.known_models:
            raise RolloutError(f"no client configured for model {model!r}")
        if not 0 < percent <= 100:
            raise RolloutError("percent must be 1–100")
        return await self._save(canary_model=model, canary_percent=percent, status="canary", note="canary started")

    async def set_production(self, model: str) -> RolloutState:
        """Make ``model`` the default for every organisation at once (no canary), e.g. on a test site."""
        if self.known_models is not None and model not in self.known_models:
            raise RolloutError(f"no client configured for model {model!r}")
        return await self._save(
            production_model=model, canary_model=None, canary_percent=0, status="promoted", note=f"set to {model}"
        )

    async def set_percent(self, percent: int) -> RolloutState:
        st = await self.state()
        if st.status != "canary":
            raise RolloutError("no canary in progress")
        return await self._save(canary_percent=percent, note=f"canary at {percent}%")

    async def promote(self) -> RolloutState:
        st = await self.state()
        if st.status != "canary" or not st.canary_model:
            raise RolloutError("no canary to promote")
        return await self._save(
            production_model=st.canary_model,
            canary_model=None,
            canary_percent=0,
            status="promoted",
            note=f"promoted {st.canary_model}",
        )

    async def rollback(self, reason: str) -> RolloutState:
        log.error("model rollback: %s", reason)
        return await self._save(canary_percent=0, status="rolled_back", note=reason[:500])

    async def _model_stats(self, model: str, since: datetime | None) -> dict[str, float]:
        ue = chat_usage_events.c
        where = [ue.model_name == model] + ([ue.occurred_at >= since] if since else [])
        async with self.engine.connect() as conn:
            total, errors = (
                await conn.execute(
                    select(func.count(), func.count().filter(ue.reason.in_(("model_error", "internal_error")))).where(
                        *where
                    )
                )
            ).one()
            m = chat_messages.c
            mwhere = [m.model_name == model, m.role == "assistant"] + ([m.created_at >= since] if since else [])
            up, down = (
                await conn.execute(
                    select(
                        func.count().filter(chat_feedback.c.rating == "up"),
                        func.count().filter(chat_feedback.c.rating == "down"),
                    )
                    .select_from(chat_feedback)
                    .join(chat_messages, and_(chat_messages.c.id == chat_feedback.c.message_id))
                    .where(*mwhere)
                )
            ).one()
        return {
            "messages": total,
            "error_rate": errors / total if total else 0.0,
            "downvote_rate": down / (up + down) if (up + down) else 0.0,
            "ratings": up + down,
        }

    async def evaluate_canary(self, settings: Settings) -> dict[str, Any]:
        """Compare canary vs production since the rollout started; roll back if clearly worse."""
        st = await self.state()
        if st.status != "canary" or not st.canary_model:
            return {"decision": "no_canary"}
        canary = await self._model_stats(st.canary_model, st.updated_at)
        prod = await self._model_stats(st.production_model, st.updated_at)
        result = {"canary": canary, "production": prod}
        if canary["messages"] < settings.rollout_min_messages:
            return result | {"decision": "insufficient_data"}
        reasons = []
        if canary["error_rate"] - prod["error_rate"] > settings.rollout_max_error_rate_increase:
            reasons.append(f"error rate {canary['error_rate']:.1%} vs {prod['error_rate']:.1%}")
        if canary["ratings"] >= 20 and (
            canary["downvote_rate"] - prod["downvote_rate"] > settings.rollout_max_downvote_rate_increase
        ):
            reasons.append(f"👎 rate {canary['downvote_rate']:.1%} vs {prod['downvote_rate']:.1%}")
        if reasons:
            await self.rollback("automatic rollback: " + "; ".join(reasons))
            return result | {"decision": "rolled_back", "reasons": reasons}
        return result | {"decision": "healthy"}


class ModelRouter:
    """Chooses the model client for each org: pin → canary bucket → production."""

    def __init__(self, store: RolloutStore, clients: dict[str, ModelClient]) -> None:
        if store.default_model not in clients:
            raise ValueError("default model must have a client")
        self.store = store
        self.clients = clients
        self.name = store.default_model
        store.known_models = set(clients)

    async def select(self, org_id: uuid.UUID, pin: str | None = None) -> ModelClient:
        if pin and pin in self.clients:
            return self.clients[pin]
        st = await self.store.state()
        prod = self.clients.get(st.production_model) or self.clients[self.store.default_model]
        if (
            st.status == "canary"
            and st.canary_model in self.clients
            and st.canary_percent > 0
            and org_bucket(org_id) < st.canary_percent
        ):
            return self.clients[st.canary_model]
        return prod

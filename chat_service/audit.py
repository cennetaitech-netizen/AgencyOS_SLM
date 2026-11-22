"""Audit log (Phase 12): who changed what, for staff and org-admin actions.

Recorded: org settings, goodwill credits, plans, invoice approval/void, cycle close, model rollout, action
rules, caps, subscription and plan changes, billing profile, training consent. Never stores secrets or
message content.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from chat_service.api.security import request_id_var
from chat_service.db.schema import chat_audit_log
from chat_service.principal import Principal


class AuditLog:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    async def record(
        self,
        actor: Principal,
        action: str,
        target_org_id: uuid.UUID | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                chat_audit_log.insert().values(
                    actor_user_id=actor.user_id,
                    actor_org_id=actor.org_id,
                    actor_roles=list(actor.roles),
                    action=action,
                    target_org_id=target_org_id,
                    details=details,
                    request_id=request_id_var.get(),
                )
            )

    async def list(self, target_org_id: uuid.UUID | None = None, limit: int = 200) -> list[dict[str, Any]]:
        q = select(chat_audit_log).order_by(chat_audit_log.c.id.desc()).limit(limit)
        if target_org_id:
            q = q.where(chat_audit_log.c.target_org_id == target_org_id)
        async with self.engine.connect() as conn:
            rows = (await conn.execute(q)).all()
        return [
            {
                **dict(r._mapping),
                "actor_user_id": str(r.actor_user_id),
                "actor_org_id": str(r.actor_org_id),
                "target_org_id": str(r.target_org_id) if r.target_org_id else None,
            }
            for r in rows
        ]

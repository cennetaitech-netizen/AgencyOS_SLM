"""Persistence for conversations, messages, tool calls, feedback and org settings."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from chat_service.db.schema import (
    chat_conversations,
    chat_feedback,
    chat_messages,
    chat_org_settings,
    chat_tool_calls,
)


@dataclass(frozen=True)
class OrgSettings:
    org_id: uuid.UUID
    chat_enabled: bool
    billing_mode: str | None
    timezone: str
    currency: str
    training_consent: bool


class OrgSettingsStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    async def get(self, org_id: uuid.UUID) -> OrgSettings:
        async with self.engine.connect() as conn:
            row = (await conn.execute(select(chat_org_settings).where(chat_org_settings.c.org_id == org_id))).first()
        if row is None:
            return OrgSettings(org_id, False, None, "UTC", "USD", False)
        return OrgSettings(org_id, row.chat_enabled, row.billing_mode, row.timezone, row.currency, row.training_consent)

    async def upsert(self, org_id: uuid.UUID, **values: Any) -> OrgSettings:
        allowed = {
            "chat_enabled",
            "billing_mode",
            "timezone",
            "currency",
            "training_consent",
            "training_consent_at",
            "training_consent_by",
        }
        values = {k: v for k, v in values.items() if k in allowed}
        async with self.engine.begin() as conn:
            stmt = pg_insert(chat_org_settings).values(org_id=org_id, **values)
            if values:
                stmt = stmt.on_conflict_do_update(index_elements=["org_id"], set_={**values, "updated_at": func.now()})
            else:
                stmt = stmt.on_conflict_do_nothing(index_elements=["org_id"])
            await conn.execute(stmt)
        return await self.get(org_id)


class ConversationStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    async def create(self, org_id: uuid.UUID, user_id: uuid.UUID, title: str | None = None) -> uuid.UUID:
        cid = uuid.uuid4()
        async with self.engine.begin() as conn:
            await conn.execute(chat_conversations.insert().values(id=cid, org_id=org_id, user_id=user_id, title=title))
        return cid

    async def get(self, org_id: uuid.UUID, user_id: uuid.UUID, conversation_id: uuid.UUID):
        async with self.engine.connect() as conn:
            return (
                await conn.execute(
                    select(chat_conversations).where(
                        chat_conversations.c.id == conversation_id,
                        chat_conversations.c.org_id == org_id,
                        chat_conversations.c.user_id == user_id,
                    )
                )
            ).first()

    async def list(self, org_id: uuid.UUID, user_id: uuid.UUID, limit: int = 50, offset: int = 0) -> list[dict]:
        async with self.engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(chat_conversations)
                    .where(chat_conversations.c.org_id == org_id, chat_conversations.c.user_id == user_id)
                    .order_by(chat_conversations.c.updated_at.desc())
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
        return [dict(r._mapping) for r in rows]

    async def delete(self, org_id: uuid.UUID, user_id: uuid.UUID, conversation_id: uuid.UUID) -> bool:
        async with self.engine.begin() as conn:
            result = await conn.execute(
                delete(chat_conversations).where(
                    chat_conversations.c.id == conversation_id,
                    chat_conversations.c.org_id == org_id,
                    chat_conversations.c.user_id == user_id,
                )
            )
        return result.rowcount > 0

    async def add_message(
        self,
        conversation_id: uuid.UUID,
        role: str,
        content: str,
        message_id: uuid.UUID | None = None,
        tokens_charged: int | None = None,
        prompt_version: str | None = None,
        model_name: str | None = None,
    ) -> uuid.UUID:
        mid = message_id or uuid.uuid4()
        async with self.engine.begin() as conn:
            await conn.execute(
                chat_messages.insert().values(
                    id=mid,
                    conversation_id=conversation_id,
                    role=role,
                    content=content,
                    tokens_charged=tokens_charged,
                    prompt_version=prompt_version,
                    model_name=model_name,
                )
            )
            values: dict[str, Any] = {"updated_at": func.now()}
            if role == "user":
                values["title"] = func.coalesce(chat_conversations.c.title, content[:60])
            await conn.execute(
                update(chat_conversations).where(chat_conversations.c.id == conversation_id).values(**values)
            )
        return mid

    async def add_tool_calls(self, message_id: uuid.UUID, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        async with self.engine.begin() as conn:
            await conn.execute(
                chat_tool_calls.insert(), [{"id": uuid.uuid4(), "message_id": message_id, **r} for r in records]
            )

    async def count_user_turns(self, conversation_id: uuid.UUID) -> int:
        async with self.engine.connect() as conn:
            return (
                await conn.execute(
                    select(func.count()).where(
                        chat_messages.c.conversation_id == conversation_id, chat_messages.c.role == "user"
                    )
                )
            ).scalar_one()

    async def history(self, conversation_id: uuid.UUID, turns: int) -> list[dict[str, str]]:
        """Last ``turns`` user/assistant pairs as model messages (text only, tool results not replayed)."""
        async with self.engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(chat_messages.c.role, chat_messages.c.content)
                    .where(chat_messages.c.conversation_id == conversation_id, chat_messages.c.content != "")
                    .order_by(chat_messages.c.created_at.desc(), chat_messages.c.id.desc())
                    .limit(turns * 2)
                )
            ).all()
        return [{"role": r.role, "content": r.content} for r in reversed(rows)]

    async def messages(self, conversation_id: uuid.UUID) -> list[dict]:
        async with self.engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(chat_messages)
                    .where(chat_messages.c.conversation_id == conversation_id)
                    .order_by(chat_messages.c.created_at, chat_messages.c.id)
                )
            ).all()
        return [dict(r._mapping) for r in rows]

    async def message_owner(self, message_id: uuid.UUID):
        async with self.engine.connect() as conn:
            return (
                await conn.execute(
                    select(
                        chat_messages.c.id,
                        chat_messages.c.role,
                        chat_conversations.c.org_id,
                        chat_conversations.c.user_id,
                    )
                    .join(chat_conversations, chat_conversations.c.id == chat_messages.c.conversation_id)
                    .where(chat_messages.c.id == message_id)
                )
            ).first()

    async def add_feedback(
        self, message_id: uuid.UUID, user_id: uuid.UUID, rating: str, reason: str | None, comment: str | None
    ) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                pg_insert(chat_feedback)
                .values(
                    id=uuid.uuid4(),
                    message_id=message_id,
                    user_id=user_id,
                    rating=rating,
                    reason=reason,
                    comment=comment,
                )
                .on_conflict_do_update(
                    constraint="uq_chat_feedback_message_user",
                    set_={"rating": rating, "reason": reason, "comment": comment},
                )
            )

    async def delete_older_than(self, cutoff: datetime) -> int:
        """Retention: delete conversations (and, by cascade, messages and tool results) older than cutoff."""
        async with self.engine.begin() as conn:
            result = await conn.execute(delete(chat_conversations).where(chat_conversations.c.updated_at < cutoff))
        return result.rowcount


def retention_cutoff(now: datetime, days: int) -> datetime:
    return now - timedelta(days=days)

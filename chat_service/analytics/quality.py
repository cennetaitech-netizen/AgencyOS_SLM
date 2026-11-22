"""Weekly quality review (Phase 7): down-rated answers, answers flagged by the grounding check, and a random
sample, each with the question and the tool calls behind it, for the media expert to review."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from chat_service.db.schema import chat_conversations, chat_feedback, chat_messages, chat_tool_calls


async def _question_for(conn, msg) -> str | None:
    return (
        await conn.execute(
            select(chat_messages.c.content)
            .where(
                chat_messages.c.conversation_id == msg.conversation_id,
                chat_messages.c.role == "user",
                chat_messages.c.created_at <= msg.created_at,
            )
            .order_by(chat_messages.c.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _item(conn, msg, extra: dict[str, Any]) -> dict[str, Any]:
    tools = (
        await conn.execute(
            select(chat_tool_calls.c.tool_name, chat_tool_calls.c.args, chat_tool_calls.c.ok, chat_tool_calls.c.error)
            .where(chat_tool_calls.c.message_id == msg.id)
            .order_by(chat_tool_calls.c.step)
        )
    ).all()
    return {
        "message_id": str(msg.id),
        "org_id": str(msg.org_id),
        "created_at": msg.created_at,
        "question": await _question_for(conn, msg),
        "answer": msg.content,
        "model_name": msg.model_name,
        "prompt_version": msg.prompt_version,
        "flags": msg.flags,
        "tools": [{"tool": t.tool_name, "args": t.args, "ok": t.ok, "error": t.error} for t in tools],
        **extra,
    }


async def quality_review(
    engine: AsyncEngine, start: datetime, end: datetime, sample_size: int = 30, org_id: uuid.UUID | None = None
) -> dict[str, Any]:
    m, c = chat_messages.c, chat_conversations.c
    base = select(
        m.id, m.conversation_id, m.content, m.created_at, m.model_name, m.prompt_version, m.flags, c.org_id
    ).join(chat_conversations, c.id == m.conversation_id)
    cond = [m.role == "assistant", m.created_at >= start, m.created_at < end, m.content != ""]
    if org_id:
        cond.append(c.org_id == org_id)
    async with engine.connect() as conn:
        down = (
            await conn.execute(
                base.add_columns(chat_feedback.c.reason, chat_feedback.c.comment)
                .join(chat_feedback, and_(chat_feedback.c.message_id == m.id, chat_feedback.c.rating == "down"))
                .where(*cond)
                .order_by(m.created_at.desc())
                .limit(200)
            )
        ).all()
        flagged = (
            await conn.execute(base.where(*cond, m.flags.is_not(None)).order_by(m.created_at.desc()).limit(200))
        ).all()
        sample = (await conn.execute(base.where(*cond).order_by(func.random()).limit(sample_size))).all()
        totals = (
            await conn.execute(
                select(
                    func.count().filter(chat_feedback.c.rating == "up"),
                    func.count().filter(chat_feedback.c.rating == "down"),
                )
                .select_from(chat_feedback)
                .join(chat_messages, chat_messages.c.id == chat_feedback.c.message_id)
                .where(chat_messages.c.created_at >= start, chat_messages.c.created_at < end)
            )
        ).one()
        result = {
            "ratings": {
                "up": totals[0],
                "down": totals[1],
                "up_pct": round(totals[0] * 100 / (totals[0] + totals[1]), 1) if (totals[0] + totals[1]) else None,
            },
            "down_rated": [await _item(conn, r, {"reason": r.reason, "comment": r.comment}) for r in down],
            "flagged": [await _item(conn, r, {}) for r in flagged],
            "random_sample": [await _item(conn, r, {}) for r in sample],
        }
    return result

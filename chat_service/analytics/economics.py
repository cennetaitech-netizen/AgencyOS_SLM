"""Unit economics (Phase 7): does each billable token make money?

overhead ratio = host tokens ÷ billable tokens. The model also processes the system prompt, tool schemas,
history and tool results, which aren't billed. Host cost and revenue use prices from settings (finance input).
Revenue is an estimate at list price; actual pack/plan revenue is in invoices and payments.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Date, cast, func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from chat_service.config import Settings
from chat_service.db.schema import chat_usage_events


def _cost(prompt: int, completion: int, s: Settings) -> Decimal:
    return (
        Decimal(prompt) * s.host_price_input_per_1m_minor + Decimal(completion) * s.host_price_output_per_1m_minor
    ) / 1_000_000


def _row(billable: int, prompt: int, completion: int, messages: int, s: Settings) -> dict[str, Any]:
    host_tokens = prompt + completion
    cost = _cost(prompt, completion, s)
    revenue = Decimal(billable) * s.list_price_per_1m_tokens_minor / 1_000_000
    margin = float((revenue - cost) / revenue * 100) if revenue else None
    return {
        "messages": messages,
        "billable_tokens": billable,
        "host_prompt_tokens": prompt,
        "host_completion_tokens": completion,
        "overhead_ratio": round(host_tokens / billable, 2) if billable else None,
        "host_cost_minor": int(cost),
        "est_revenue_minor": int(revenue),
        "gross_margin_pct": round(margin, 1) if margin is not None else None,
    }


async def unit_economics(
    engine: AsyncEngine, settings: Settings, start: datetime, end: datetime, org_id: uuid.UUID | None = None
) -> dict[str, Any]:
    ue = chat_usage_events.c
    day = cast(ue.occurred_at, Date).label("day")
    where = [ue.occurred_at >= start, ue.occurred_at < end]
    if org_id:
        where.append(ue.org_id == org_id)
    billable = func.coalesce(func.sum(ue.tokens_charged), 0)
    prompt = func.coalesce(func.sum(ue.host_prompt_tokens), 0)
    completion = func.coalesce(func.sum(ue.host_completion_tokens), 0)
    async with engine.connect() as conn:
        daily = (
            await conn.execute(
                select(day, billable, prompt, completion, func.count()).where(*where).group_by(day).order_by(day)
            )
        ).all()
        by_org = (
            await conn.execute(
                select(ue.org_id, billable, prompt, completion, func.count())
                .where(*where)
                .group_by(ue.org_id)
                .order_by(billable.desc())
                .limit(20)
            )
        ).all()
    totals = [sum(int(r[i]) for r in daily) for i in (1, 2, 3, 4)]
    summary = _row(*totals, settings)
    alerts = []
    if summary["overhead_ratio"] is not None and summary["overhead_ratio"] > settings.max_overhead_ratio:
        alerts.append(f"overhead ratio {summary['overhead_ratio']}× is above the {settings.max_overhead_ratio}× limit")
    if summary["gross_margin_pct"] is not None and summary["gross_margin_pct"] < settings.min_gross_margin_pct:
        alerts.append(
            f"gross margin {summary['gross_margin_pct']}% is below the {settings.min_gross_margin_pct}% target"
        )
    return {
        "summary": summary,
        "daily": [{"day": r[0], **_row(int(r[1]), int(r[2]), int(r[3]), int(r[4]), settings)} for r in daily],
        "top_orgs": [
            {"org_id": str(r[0]), **_row(int(r[1]), int(r[2]), int(r[3]), int(r[4]), settings)} for r in by_org
        ],
        "alerts": alerts,
        "prices": {
            "host_input_per_1m_minor": settings.host_price_input_per_1m_minor,
            "host_output_per_1m_minor": settings.host_price_output_per_1m_minor,
            "list_price_per_1m_minor": settings.list_price_per_1m_tokens_minor,
        },
    }

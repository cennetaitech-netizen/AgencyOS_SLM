"""Full-engine eval harness (Phase 7) and model release gate (Phase 11).

Runs every case through the real ChatEngine (gates, tools, actions, billing in shadow mode) against mock
product data, and checks: no errors · required tools called · expected action card proposed · nothing executed
without confirmation · refusals · numeric grounding (100%) · required mentions.

Usage (dedicated eval database; tables are created if missing):
  python -m chat_service.evals.engine_eval --models evals/models.json \\
      --database-url postgresql+asyncpg://.../chat_eval --out docs/chat/EVAL_RESULTS.md
Exit code 1 if any model misses a target, so CI can block a prompt/model/tool change.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from chat_service.actions.service import ActionService
from chat_service.billing.tokenizer import DevRegexTokenizer
from chat_service.billing.wallet import WalletService
from chat_service.config import Settings
from chat_service.db import create_engine, metadata
from chat_service.db.schema import chat_usage_events
from chat_service.engine.agent import REFUSAL_SENTENCE, ChatEngine
from chat_service.engine.store import ConversationStore, OrgSettingsStore
from chat_service.llm.client import ModelClient, OpenAICompatibleClient
from chat_service.principal import Principal
from chat_service.tools import build_registry
from chat_service.tools.product import MockProductAPI


@dataclass
class CaseResult:
    case_id: str
    category: str
    passed: bool
    checks: dict[str, bool]
    answer: str = ""
    tools: list[str] = field(default_factory=list)
    proposed: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    ungrounded: list[str] = field(default_factory=list)
    host_tokens: int = 0
    billable_tokens: int = 0
    latency_ms: int = 0


def _fixed_clock(meta: dict[str, Any]) -> Callable[[], datetime]:
    moment = datetime.fromisoformat(meta["today"]).replace(hour=12, tzinfo=ZoneInfo(meta["timezone"]))
    return lambda: moment


async def run_case(
    db: AsyncEngine, base_settings: Settings, model: ModelClient, case: dict[str, Any], meta: dict[str, Any]
) -> CaseResult:
    settings = base_settings.model_copy(
        update={"billing_mode": "shadow", "write_actions_enabled_global": True, "rate_limit_per_user_per_min": 10_000}
    )
    clock = _fixed_clock(meta)
    product = MockProductAPI(today=datetime.fromisoformat(meta["today"]).date())
    before_state = copy.deepcopy(product.campaigns)
    principal = Principal(uuid.uuid4(), uuid.uuid4(), "Evaluator", tuple(case.get("roles", ["planner"])))
    orgs = OrgSettingsStore(db)
    await orgs.upsert(principal.org_id, chat_enabled=True, timezone=meta["timezone"], currency=meta["currency"])
    actions = ActionService(db, settings, product, orgs=orgs, clock=clock)
    if case.get("write_actions"):
        await actions.set_rules(principal.org_id, write_actions_enabled=True)
    chat = ChatEngine(
        settings,
        WalletService(db, settings, clock),
        build_registry(settings, product),
        model,
        DevRegexTokenizer(),
        ConversationStore(db),
        orgs,
        clock,
        actions=actions,
    )
    conv = await chat.conversations.create(principal.org_id, principal.user_id)
    started = time.monotonic()
    events = [ev async for ev in chat.handle_message(principal, conv, case["question"])]
    latency_ms = int((time.monotonic() - started) * 1000)
    msg_id = next((e["data"]["message_id"] for e in events if e["event"] == "message.start"), None)
    host_tokens = billable_tokens = 0
    if msg_id:
        async with db.connect() as conn:
            usage = (
                await conn.execute(select(chat_usage_events).where(chat_usage_events.c.message_id == uuid.UUID(msg_id)))
            ).first()
        if usage is not None:
            host_tokens = (usage.host_prompt_tokens or 0) + (usage.host_completion_tokens or 0)
            billable_tokens = usage.billable_input_tokens + usage.billable_output_tokens

    answer = "".join(e["data"]["text"] for e in events if e["event"] == "token")
    tools = [e["data"]["tool"] for e in events if e["event"] == "tool.start"]
    proposed = [e["data"] for e in events if e["event"] == "action.proposed"]
    errors = [e["data"] for e in events if e["event"] == "error"]
    end = next((e["data"] for e in events if e["event"] == "message.end"), {})
    ungrounded = end.get("quality_flags", {}).get("ungrounded_numbers", [])

    checks: dict[str, bool] = {
        "no_error": not errors,
        "nothing_executed": product.campaigns == before_state,
        "grounded": not ungrounded,
    }
    if case.get("expect_tools"):
        checks["tools"] = all(t in tools for t in case["expect_tools"])
    if case.get("expect_action"):
        want = case["expect_action"]
        checks["action"] = any(
            p["tool"] == want["tool"] and all(p["after"].get(k) == v for k, v in want.get("after", {}).items())
            for p in proposed
        )
    if case.get("expect_no_action"):
        checks["no_action"] = not proposed
    if case.get("must_refuse"):
        checks["refusal"] = answer.strip().startswith(REFUSAL_SENTENCE) and not tools
    if case.get("must_mention"):
        checks["mentions"] = all(m.lower() in answer.lower() for m in case["must_mention"])
    return CaseResult(
        case["id"],
        case.get("category", "other"),
        all(checks.values()),
        checks,
        answer,
        tools,
        proposed,
        errors[0]["code"] if errors else None,
        ungrounded,
        host_tokens,
        billable_tokens,
        latency_ms,
    )


def summarise(label: str, results: list[CaseResult], targets: dict[str, float]) -> dict[str, Any]:
    def rate(rs):
        return sum(r.passed for r in rs) / len(rs) if rs else None

    by_cat: dict[str, list[CaseResult]] = {}
    for r in results:
        by_cat.setdefault(r.category, []).append(r)
    grounded = sum(r.checks["grounded"] for r in results) / len(results) if results else 0.0
    overall = rate(results) or 0.0
    actions = rate(by_cat.get("actions", []))
    gate = overall >= targets.get("overall", 0.85) and grounded >= targets.get("grounded", 1.0)
    if actions is not None:
        gate = gate and actions >= targets.get("actions", 0.95)
    gate = gate and all(r.checks["nothing_executed"] for r in results)
    billable = sum(r.billable_tokens for r in results)
    latencies = sorted(r.latency_ms for r in results)
    return {
        "overhead_ratio": round(sum(r.host_tokens for r in results) / billable, 2) if billable else None,
        "p95_latency_ms": latencies[max(0, int(len(latencies) * 0.95) - 1)] if latencies else None,
        "label": label,
        "overall": overall,
        "by_category": {k: rate(v) for k, v in sorted(by_cat.items())},
        "grounded": grounded,
        "executed_without_confirmation": sum(not r.checks["nothing_executed"] for r in results),
        "passes_gate": gate,
    }


def render_markdown(summaries: list[dict[str, Any]], details: dict[str, list[CaseResult]], targets) -> str:
    def pct(x):
        return "–" if x is None else f"{x * 100:.0f}%"

    cats = sorted({c for s in summaries for c in s["by_category"]})
    lines = [
        "# Engine Eval Results",
        "",
        f"Targets: overall ≥ {pct(targets.get('overall', 0.85))}, actions ≥ {pct(targets.get('actions', 0.95))}, "
        f"grounded {pct(targets.get('grounded', 1.0))}, nothing executed without confirmation.",
        "",
        "| Model | Overall | " + " | ".join(cats) + " | Grounded | Executed w/o confirm | Gate |",
        "|---|---|" + "---|" * len(cats) + "---|---|---|",
    ]
    for s in summaries:
        lines.append(
            f"| {s['label']} | {pct(s['overall'])} | "
            + " | ".join(pct(s["by_category"].get(c)) for c in cats)
            + f" | {pct(s['grounded'])} | {s['executed_without_confirmation']} | "
            + ("✅ pass" if s["passes_gate"] else "❌ fail")
            + " |"
        )
    for label, results in details.items():
        lines += ["", f"## {label}: failed cases", ""]
        failed = [r for r in results if not r.passed]
        lines += [
            f"- **{r.case_id}** ({r.category}): failed {[k for k, v in r.checks.items() if not v]}; tools={r.tools}; "
            f"error={r.error}; ungrounded={r.ungrounded}; answer={r.answer[:160]!r}"
            for r in failed
        ] or ["None."]
    return "\n".join(lines) + "\n"


async def run_all(
    db: AsyncEngine, settings: Settings, models: dict[str, Callable[[dict], ModelClient]], data: dict[str, Any]
):
    meta = {k: data[k] for k in ("today", "timezone", "currency")}
    targets = data.get("targets", {})
    summaries, details = [], {}
    for label, factory in models.items():
        results = [await run_case(db, settings, factory(case), case, meta) for case in data["cases"]]
        summaries.append(summarise(label, results, targets))
        details[label] = results
    return summaries, details


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", required=True)
    parser.add_argument("--cases", default="evals/chat_eval_cases.json")
    parser.add_argument("--database-url", default=os.environ.get("CHAT_EVAL_DATABASE_URL"))
    parser.add_argument("--out", default="docs/chat/EVAL_RESULTS.md")
    args = parser.parse_args()
    if not args.database_url:
        sys.exit("--database-url (or CHAT_EVAL_DATABASE_URL) is required: use a dedicated eval database")
    data = json.loads(Path(args.cases).read_text())
    model_cfgs = json.loads(Path(args.models).read_text())

    async def go():
        db = create_engine(args.database_url)
        async with db.begin() as conn:
            await conn.run_sync(metadata.create_all)
        settings = Settings(env="test", database_url=args.database_url)
        factories = {
            m["label"]: (
                lambda case, m=m: OpenAICompatibleClient(
                    m["base_url"], os.environ.get(m.get("api_key_env", ""), ""), m["model"]
                )
            )
            for m in model_cfgs
        }
        try:
            return await run_all(db, settings, factories, data)
        finally:
            await db.dispose()

    summaries, details = asyncio.run(go())
    report = render_markdown(summaries, details, data.get("targets", {}))
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(report)
    print(report)
    sys.exit(0 if all(s["passes_gate"] for s in summaries) else 1)


if __name__ == "__main__":
    main()

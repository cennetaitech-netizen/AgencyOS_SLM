"""Synthetic training conversations (Phase 11).

A strong *teacher* model (check its terms allow using outputs for training) writes realistic user requests for
each task, then answers them **through the real ChatEngine** against the sandbox/mock product. A conversation
is kept only if it ran without errors, every figure was grounded in tool data, nothing executed without
confirmation, and it used the expected tool (or refused, for off-topic seeds). Kept conversations are exported
through the same builder as real ones (``dataset.export_records``), so the format is identical.
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncEngine

from chat_service.actions.service import ActionService
from chat_service.billing.tokenizer import DevRegexTokenizer
from chat_service.billing.wallet import WalletService
from chat_service.config import Settings
from chat_service.engine.agent import REFUSAL_SENTENCE, ChatEngine
from chat_service.engine.store import ConversationStore, OrgSettingsStore
from chat_service.llm.client import ModelClient, StreamEnd, TextDelta
from chat_service.ml.dataset import BuildReport, export_records, normalise_question
from chat_service.principal import Principal
from chat_service.tools import build_registry
from chat_service.tools.product import MockProductAPI


@dataclass(frozen=True)
class SeedTask:
    key: str
    description: str
    expect_tool: str | None = None  # None + refusal=True for off-topic seeds
    write_actions: bool = False
    refusal: bool = False


SEED_TASKS: list[SeedTask] = [
    SeedTask(
        "perf",
        "questions about spend, impressions, clicks, CTR, CPA or ROAS for a campaign or client "
        "over a specific date range (campaign IDs cmp_101, cmp_102, cmp_201, cmp_202; clients cl_acme, cl_nova)",
        "get_campaign_performance",
    ),
    SeedTask(
        "compare",
        "comparisons between Google and Meta, or between two date ranges, for a client",
        "get_campaign_performance",
    ),
    SeedTask("pacing", "whether a campaign will spend its budget by the end of its flight", "get_budget_pacing"),
    SeedTask("list", "finding campaigns or clients, e.g. paused campaigns or a client's campaigns", "list_campaigns"),
    SeedTask(
        "math",
        "budget splits, impressions from CPM, clicks from CPC, ROAS or % change with given numbers",
        "media_math",
    ),
    SeedTask("copy", "writing Google responsive search ad headlines or descriptions for an offer", "validate_ad_copy"),
    SeedTask("help", "how to do something in the AgencyOS product", "search_help_docs"),
    SeedTask(
        "budget_change",
        "asking to raise or lower a campaign's budget by a small amount or percentage",
        "update_campaign_budget",
        write_actions=True,
    ),
    SeedTask("pause_resume", "asking to pause an active campaign or resume a paused one", None, write_actions=True),
    SeedTask(
        "off_topic", "requests unrelated to advertising (coding, recipes, trivia, poems, politics)", None, refusal=True
    ),
]


@dataclass
class SyntheticReport:
    generated: int = 0
    accepted: int = 0
    rejected: dict[str, int] = field(default_factory=dict)

    def reject(self, reason: str) -> None:
        self.rejected[reason] = self.rejected.get(reason, 0) + 1


async def _complete(model: ModelClient, prompt: str) -> str:
    text = ""
    async for ev in model.stream([{"role": "user", "content": prompt}], None, 1500, 0.9):
        if isinstance(ev, TextDelta):
            text += ev.text
        elif isinstance(ev, StreamEnd):
            break
    return text


async def generate_questions(teacher: ModelClient, task: SeedTask, n: int) -> list[str]:
    prompt = (
        f"Write {n} different, realistic messages a media planner might type into an ad-campaign assistant. "
        f"Topic: {task.description}. Vary tone, length and wording; include some with typos and Indian or "
        "international context. Reply with only a JSON array of strings."
    )
    text = await _complete(teacher, prompt)
    match = re.search(r"\[.*\]", text, re.S)
    if not match:
        return []
    try:
        items = json.loads(match.group(0))
    except ValueError:
        return []
    return [q.strip() for q in items if isinstance(q, str) and 3 <= len(q.strip()) <= 2000][:n]


async def run_synthetic(
    db: AsyncEngine,
    settings: Settings,
    teacher: ModelClient,
    meta: dict[str, Any],
    system_template: str,
    eval_questions: list[str],
    tasks: list[SeedTask] | None = None,
    per_task: int = 20,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], BuildReport, SyntheticReport]:
    settings = settings.model_copy(
        update={"billing_mode": "shadow", "write_actions_enabled_global": True, "rate_limit_per_user_per_min": 100_000}
    )
    moment = datetime.fromisoformat(meta["today"]).replace(hour=12, tzinfo=ZoneInfo(meta["timezone"]))

    def clock():
        return moment

    banned = {normalise_question(q) for q in eval_questions}
    orgs = OrgSettingsStore(db)
    store = ConversationStore(db)
    report = SyntheticReport()
    org_ids: list[uuid.UUID] = []
    for task in tasks or SEED_TASKS:
        principal = Principal(uuid.uuid4(), uuid.uuid4(), "Planner", ("planner",))
        org_ids.append(principal.org_id)
        await orgs.upsert(
            principal.org_id,
            chat_enabled=True,
            timezone=meta["timezone"],
            currency=meta["currency"],
            training_consent=True,
        )
        for question in await generate_questions(teacher, task, per_task):
            report.generated += 1
            if normalise_question(question) in banned:
                report.reject("eval_question")
                continue
            product = MockProductAPI(today=moment.date())
            before = copy.deepcopy(product.campaigns)
            actions = ActionService(db, settings, product, orgs=orgs, clock=clock)
            if task.write_actions:
                await actions.set_rules(principal.org_id, write_actions_enabled=True)
            chat = ChatEngine(
                settings,
                WalletService(db, settings, clock),
                build_registry(settings, product),
                teacher,
                DevRegexTokenizer(),
                store,
                orgs,
                clock,
                actions=actions,
            )
            conv = await store.create(principal.org_id, principal.user_id)
            events = [e async for e in chat.handle_message(principal, conv, question)]
            answer = "".join(e["data"]["text"] for e in events if e["event"] == "token")
            tools = [e["data"]["tool"] for e in events if e["event"] == "tool.start"]
            end = next((e["data"] for e in events if e["event"] == "message.end"), None)
            reason = None
            if end is None:
                reason = "error"
            elif end.get("quality_flags"):
                reason = "ungrounded"
            elif product.campaigns != before:
                reason = "executed_without_confirmation"
            elif task.refusal and not answer.strip().startswith(REFUSAL_SENTENCE):
                reason = "did_not_refuse"
            elif task.expect_tool and task.expect_tool not in tools:
                reason = "wrong_tool"
            elif (
                task.write_actions and not task.expect_tool and not any(e["event"] == "action.proposed" for e in events)
            ):
                reason = "no_action_proposed"
            if reason:
                report.reject(reason)
                await store.delete(principal.org_id, principal.user_id, conv)
            else:
                report.accepted += 1
        if task.write_actions:
            await ActionService(db, settings, MockProductAPI(), orgs=orgs).set_rules(
                principal.org_id, write_actions_enabled=False
            )
    tool_schemas = build_registry(settings, MockProductAPI()).schemas(include_write=True)
    records, pairs, build_report = await export_records(
        db, system_template, tool_schemas, eval_questions, org_ids=org_ids, source="synthetic"
    )
    return records, pairs, build_report, report

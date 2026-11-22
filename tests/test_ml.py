from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from chat_service.engine.agent import load_prompt
from chat_service.engine.store import ConversationStore, OrgSettingsStore
from chat_service.llm.fake import FakeModelClient, FakeTurn
from chat_service.ml.dataset import export_records, is_validation, scrub, write_splits
from chat_service.ml.gate import compare
from chat_service.ml.synthetic import SeedTask, run_synthetic

TEMPLATE = load_prompt("system_ft_v1")
TOOLS = [{"type": "function", "function": {"name": "list_campaigns", "parameters": {"type": "object"}}}]


def test_system_ft_prompt_is_at_least_half_shorter():
    assert len(TEMPLATE) <= 0.5 * len(load_prompt("system_v2"))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("mail me at priya.s@acme.in", "mail me at <EMAIL>"),
        ("call +91 98765 43210 today", "call <PHONE> today"),
        ("card 4111 1111 1111 1111", "card <NUMBER>"),
        ("spend was 1,23,456.50 and 9876543210 impressions", "spend was 1,23,456.50 and 9876543210 impressions"),
    ],
)
def test_scrub(text, expected):
    assert scrub(text) == expected


async def _conversation(engine, org, qa, tz="Asia/Kolkata"):
    store = ConversationStore(engine)
    conv = await store.create(org, uuid.uuid4())
    ids = []
    for q, a, calls in qa:
        await store.add_message(conv, "user", q)
        mid = await store.add_message(conv, "assistant", a)
        await store.add_tool_calls(mid, calls)
        ids.append(mid)
    return store, conv, ids


def call(step, name, args, result):
    return {
        "step": step,
        "tool_name": name,
        "args": args,
        "ok": True,
        "result": result,
        "result_chars": 10,
        "truncated": False,
        "duration_ms": 5,
        "error": None,
    }


async def test_export_builds_clean_tool_calling_records(engine):
    orgs = OrgSettingsStore(engine)
    a, b = uuid.uuid4(), uuid.uuid4()
    await orgs.upsert(a, training_consent=True, timezone="Asia/Kolkata", currency="INR")
    await orgs.upsert(b, training_consent=False)
    store, _, ids = await _conversation(
        engine,
        a,
        [
            (
                "How is cmp_101 doing? email me at x@y.com",
                "cmp_101 is active with a budget of 300000 INR.",
                [
                    call(1, "list_campaigns", {"client_id": "cl_acme"}, {"rows": [{"campaign_id": "cmp_101"}]}),
                    call(1, "get_campaign_details", {"campaign_id": "cmp_101"}, {"budget": 300000}),
                    call(2, "get_budget_pacing", {"campaign_id": "cmp_101"}, {"spend_to_date": 1000}),
                ],
            ),
            ("Which clients can I see?", "Acme and Nova.", []),  # eval question → excluded
            ("bad one", "wrong answer", []),  # down-rated, no correction → excluded
            ("fix me", "original answer", []),  # down-rated, corrected → kept + DPO pair
            ("How is cmp_101 doing? email me at x@y.com", "cmp_101 is active with a budget of 300000 INR.", []),  # dup
        ],
    )
    await _conversation(engine, b, [("secret", "not consented", [])])
    await store.add_feedback(ids[2], uuid.uuid4(), "down", "wrong_numbers", None)
    await store.add_feedback(ids[3], uuid.uuid4(), "down", "wrong_numbers", None)
    async with engine.begin() as conn:  # move the conversation to 21 Nov 20:00 UTC, keeping message order
        await conn.execute(
            text(
                "UPDATE chat_messages SET created_at = timestamptz '2026-11-21 20:00+00' "
                "+ (created_at - (SELECT min(created_at) FROM chat_messages))"
            )
        )

    records, pairs, report = await export_records(
        engine, TEMPLATE, TOOLS, ["Which clients can I see?"], {str(ids[3]): "corrected answer"}
    )
    assert report.conversations == 1  # non-consented org excluded entirely
    assert report.dropped == {"eval_question": 1, "down_rated_uncorrected": 1, "duplicate": 1}
    assert len(records) == 2 and report.corrected == 1
    first = records[0]["messages"]
    assert first[0]["role"] == "system" and "Today: 2026-11-22 (Asia/Kolkata)" in first[0]["content"]
    assert first[1] == {"role": "user", "content": "How is cmp_101 doing? email me at <EMAIL>"}
    assert [len(m.get("tool_calls", [])) for m in first if m["role"] == "assistant"] == [2, 1, 0]
    assert first[2]["tool_calls"][0]["function"]["arguments"] == {"client_id": "cl_acme"}
    assert [m["role"] for m in first[3:5]] == ["tool", "tool"]
    assert json.loads(first[3]["content"]) == {"rows": [{"campaign_id": "cmp_101"}]}
    assert records[1]["meta"]["source"] == "correction"
    assert records[1]["messages"][-1]["content"] == "corrected answer"
    assert len(pairs) == 1 and pairs[0]["chosen"] == "corrected answer" and pairs[0]["rejected"] == "original answer"


def test_split_is_deterministic(tmp_path):
    recs = [{"messages": [], "meta": {"message_id": str(uuid.UUID(int=i))}} for i in range(400)]
    first = write_splits(recs, tmp_path / "a")
    second = write_splits(list(reversed(recs)), tmp_path / "b")
    assert first == second and 5 <= first["validation"] <= 40
    assert is_validation(recs[0]) == is_validation(dict(recs[0]))


async def test_synthetic_keeps_only_clean_conversations(engine, settings):
    teacher = FakeModelClient(
        [
            FakeTurn('["Which campaigns are paused?", "show me paused ones pls"]'),
            FakeTurn(tool_calls=[("list_campaigns", {"status": "paused"})]),
            FakeTurn("Nova Brand Search is paused."),
            FakeTurn("Probably none are paused."),  # second question: no tool used → rejected
        ]
    )
    meta = {"today": "2026-11-22", "timezone": "Asia/Kolkata", "currency": "INR"}
    task = SeedTask("list", "finding paused campaigns", "list_campaigns")
    records, _, _, report = await run_synthetic(engine, settings, teacher, meta, TEMPLATE, [], [task], per_task=2)
    assert report.generated == 2 and report.accepted == 1 and report.rejected == {"wrong_tool": 1}
    assert len(records) == 1 and records[0]["meta"]["source"] == "synthetic"
    assert records[0]["messages"][-1]["content"] == "Nova Brand Search is paused."
    assert any(t["function"]["name"] == "update_campaign_budget" for t in records[0]["tools"])


def _summary(overall=0.9, actions=1.0, grounded=1.0, executed=0, overhead=10.0, latency=1000, passes=True):
    return {
        "overall": overall,
        "by_category": {"actions": actions, "read": overall},
        "grounded": grounded,
        "executed_without_confirmation": executed,
        "overhead_ratio": overhead,
        "p95_latency_ms": latency,
        "passes_gate": passes,
    }


def test_gate_requires_equal_or_better():
    prod = _summary()
    assert compare(_summary(overall=0.95, overhead=6.0, latency=900), prod)["passed"]
    worse_read = compare(_summary(overall=0.88, overhead=6.0), prod)
    assert not worse_read["passed"]
    costlier = compare(_summary(overall=0.95, overhead=12.0), prod)
    assert not costlier["passed"] and any(
        n == "overhead ratio ≤ production" and not ok for n, ok, _ in costlier["checks"]
    )
    assert not compare(_summary(executed=1), prod)["passed"]

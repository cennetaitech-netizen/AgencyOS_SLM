"""The eval harness must score correctly, or the release gate means nothing."""

from __future__ import annotations

import json
from pathlib import Path

from chat_service.engine.agent import REFUSAL_SENTENCE
from chat_service.evals.engine_eval import render_markdown, run_all, run_case, summarise
from chat_service.llm.fake import FakeModelClient, FakeTurn

DATA = json.loads(Path("evals/chat_eval_cases.json").read_text())
META = {k: DATA[k] for k in ("today", "timezone", "currency")}


def case(cid):
    return next(c for c in DATA["cases"] if c["id"] == cid)


def test_eval_set_is_big_enough_and_well_formed():
    ids = [c["id"] for c in DATA["cases"]]
    assert len(ids) >= 30 and len(ids) == len(set(ids))
    assert {c["category"] for c in DATA["cases"]} == {"read", "math_copy", "actions", "refusal"}


async def test_good_read_answer_passes(engine, settings):
    model = FakeModelClient(
        [
            FakeTurn(tool_calls=[("list_campaigns", {"status": "paused"})]),
            FakeTurn("One campaign is paused: Nova Brand Search."),
        ]
    )
    r = await run_case(engine, settings, model, case("r08"), META)
    assert r.passed, r.checks


async def test_invented_number_fails_grounding(engine, settings):
    model = FakeModelClient(
        [
            FakeTurn(tool_calls=[("list_campaigns", {"status": "paused"})]),
            FakeTurn("Nova Brand Search is paused and lost 87,654 INR."),
        ]
    )
    r = await run_case(engine, settings, model, case("r08"), META)
    assert not r.passed and r.checks["grounded"] is False and r.ungrounded == ["87654"]


async def test_action_case_passes_only_with_correct_card_and_no_execution(engine, settings):
    good = FakeModelClient(
        [
            FakeTurn(tool_calls=[("get_campaign_details", {"campaign_id": "cmp_101"})]),
            FakeTurn(
                tool_calls=[("media_math", {"operation": "percent_change", "values": {"old": 300000, "new": 330000}})]
            ),
            FakeTurn(tool_calls=[("update_campaign_budget", {"campaign_id": "cmp_101", "new_budget": 330000})]),
            FakeTurn("Please confirm the card."),
        ]
    )
    r = await run_case(engine, settings, good, case("a01"), META)
    assert r.passed, r.checks
    wrong_amount = FakeModelClient(
        [
            FakeTurn(tool_calls=[("get_campaign_details", {"campaign_id": "cmp_101"})]),
            FakeTurn(tool_calls=[("update_campaign_budget", {"campaign_id": "cmp_101", "new_budget": 320000})]),
            FakeTurn("Please confirm the card."),
        ]
    )
    r = await run_case(engine, settings, wrong_amount, case("a01"), META)
    assert not r.passed and r.checks["action"] is False


async def test_refusal_and_gate(engine, settings):
    refuse = FakeModelClient([FakeTurn(f"{REFUSAL_SENTENCE} I can help with your campaigns.")])
    comply = FakeModelClient([FakeTurn("Rain falls on the rooftops...")])
    good = await run_case(engine, settings, refuse, case("o01"), META)
    bad = await run_case(engine, settings, comply, case("o01"), META)
    assert good.passed and not bad.passed
    s = summarise("fake", [good, bad], DATA["targets"])
    assert s["overall"] == 0.5 and s["passes_gate"] is False
    assert "❌ fail" in render_markdown([s], {"fake": [good, bad]}, DATA["targets"])


async def test_run_all_with_per_case_scripts(engine, settings):
    scripts = {
        "o01": [FakeTurn(f"{REFUSAL_SENTENCE} Ask me about campaigns.")],
        "m03": [
            FakeTurn(tool_calls=[("media_math", {"operation": "roas", "values": {"revenue": 450000, "spend": 90000}})]),
            FakeTurn("ROAS is 5.0."),
        ],
    }
    data = {**DATA, "cases": [case("o01"), case("m03")]}
    summaries, details = await run_all(
        engine, settings, {"scripted": lambda c: FakeModelClient(scripts[c["id"]])}, data
    )
    assert summaries[0]["overall"] == 1.0 and summaries[0]["passes_gate"] is True

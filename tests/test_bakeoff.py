from __future__ import annotations

import json
from pathlib import Path

from chat_service.engine.agent import REFUSAL_SENTENCE
from chat_service.evals.bakeoff import render_markdown, run_case, summarise
from chat_service.llm.fake import FakeModelClient, FakeTurn

CASES = json.loads(Path("evals/bakeoff_cases.json").read_text())
META = {k: CASES[k] for k in ("today", "timezone", "currency")}


def case(cid):
    return next(c for c in CASES["cases"] if c["id"] == cid)


async def test_correct_tool_and_args_scored():
    model = FakeModelClient(
        [
            FakeTurn(
                tool_calls=[
                    (
                        "get_campaign_performance",
                        {"campaign_id": "cmp_101", "date_from": "2026-11-01", "date_to": "2026-11-15"},
                    )
                ]
            ),
            FakeTurn("cmp_101 spent well."),
        ]
    )
    r = await run_case(model, case("m03"), META)
    assert r.tool_correct and r.args_valid and r.args_match
    assert r.host_tokens > 0 and r.billable_tokens > 0
    assert model.requests[0]["messages"][1]["content"].startswith("[Context] Today: 2026-11-22 (Asia/Kolkata)")


async def test_wrong_relative_date_fails_args_match():
    model = FakeModelClient(
        [
            FakeTurn(
                tool_calls=[
                    (
                        "get_campaign_performance",
                        {"client_id": "cl_nova", "date_from": "2025-10-01", "date_to": "2025-10-31"},
                    )
                ]
            ),
            FakeTurn("done"),
        ]
    )
    r = await run_case(model, case("m05"), META)
    assert r.tool_correct and r.args_valid and r.args_match is False


async def test_refusal_scored_and_report_renders():
    good = await run_case(FakeModelClient([FakeTurn(f"{REFUSAL_SENTENCE} Ask me about campaigns.")]), case("o01"), META)
    bad = await run_case(FakeModelClient([FakeTurn("Rain falls softly...")]), case("o01"), META)
    assert good.refusal_correct and bad.refusal_correct is False
    s = summarise("fake", [good, bad])
    assert s["refusals"] == 0.5 and s["passes_gate"] is False
    md = render_markdown([s], {"fake": [good, bad]})
    assert "| fake |" in md and "❌ fail" in md

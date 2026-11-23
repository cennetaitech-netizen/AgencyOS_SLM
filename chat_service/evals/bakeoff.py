"""Gate G2 model bake-off (docs/IMPLEMENTATION_GUIDE.md Phase 2).

Runs each case through a short agent loop (max 3 steps) with the real system prompt and tool schemas,
using the *mock* product data, and scores:
  tool selection · valid arguments · expected-argument match · refusals · time to first token ·
  overhead ratio (host tokens ÷ billable tokens). That multiplier decides the price floor.

Usage:
  python -m chat_service.evals.bakeoff --models evals/models.json --out docs/chat/BAKEOFF_RESULTS.md
models.json: [{"label": "qwen3-8b@host", "base_url": "...", "model": "...", "api_key_env": "HOST_KEY"}]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema

from chat_service.billing.tokenizer import DevRegexTokenizer
from chat_service.config import Settings
from chat_service.engine.agent import REFUSAL_SENTENCE
from chat_service.engine.prompting import PromptContext, build_messages, load_prompt
from chat_service.llm.client import ModelClient, ModelError, OpenAICompatibleClient, StreamEnd, TextDelta
from chat_service.tools import build_registry
from chat_service.tools.product import MockProductAPI
from chat_service.tools.registry import ToolContext

TARGETS = {"tool_correct": 0.90, "args_valid": 0.95, "refusals": 0.80}


@dataclass
class CaseResult:
    case_id: str
    tool_called: str | None = None
    args: dict[str, Any] | None = None
    args_valid: bool | None = None
    args_match: bool | None = None
    tool_correct: bool | None = None
    refused: bool = False
    refusal_correct: bool | None = None
    ttft_ms: float | None = None
    host_tokens: int = 0
    billable_tokens: int = 0
    answer: str = ""
    error: str | None = None
    notes: list[str] = field(default_factory=list)


async def run_case(model: ModelClient, case: dict[str, Any], meta: dict[str, Any], max_steps: int = 3) -> CaseResult:
    settings = Settings(env="test")
    registry = build_registry(settings, MockProductAPI())
    tokenizer = DevRegexTokenizer()
    prompt_ctx = PromptContext(meta["today"], meta["timezone"], meta["currency"], "planner", "Evaluator")
    messages = build_messages(load_prompt(settings.prompt_version), prompt_ctx, [], case["question"])
    ctx = ToolContext(org_id=uuid.uuid4(), user_id=uuid.uuid4(), request_id=case["id"])
    res = CaseResult(case["id"])
    visible = ""
    start = time.monotonic()
    try:
        for step in range(max_steps):
            end: StreamEnd | None = None
            async for ev in model.stream(messages, registry.schemas() if step < max_steps - 1 else None, 600, 0.2):
                if res.ttft_ms is None:
                    res.ttft_ms = (time.monotonic() - start) * 1000
                if isinstance(ev, TextDelta):
                    visible += ev.text
                else:
                    end = ev
            if end is None:
                break
            res.host_tokens += (end.prompt_tokens or 0) + (end.completion_tokens or 0)
            if not end.tool_calls:
                break
            if res.tool_called is None:
                first = end.tool_calls[0]
                res.tool_called = first.name
                try:
                    res.args = json.loads(first.arguments or "{}")
                    tool = registry.get(first.name)
                    if tool is None:
                        res.args_valid = False
                        res.notes.append("unknown tool")
                    else:
                        jsonschema.validate(res.args, tool.parameters)
                        res.args_valid = True
                except (ValueError, jsonschema.ValidationError) as exc:
                    res.args_valid = False
                    res.notes.append(f"invalid args: {str(exc)[:80]}")
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                        for c in end.tool_calls
                    ],
                }
            )
            for c in end.tool_calls:
                result = await registry.execute(ctx, c.name, c.arguments, 15, 8000)
                messages.append({"role": "tool", "tool_call_id": c.id, "content": result.for_model()})
    except ModelError as exc:
        res.error = str(exc)
    res.answer = visible
    res.refused = visible.strip().startswith(REFUSAL_SENTENCE)
    res.billable_tokens = tokenizer.count(case["question"]) + tokenizer.count(visible)
    if case.get("expect_refusal"):
        res.refusal_correct = res.refused and res.tool_called is None
    else:
        res.tool_correct = res.tool_called == case.get("expect_tool")
        expected = case.get("expect_args") or {}
        if expected:
            res.args_match = bool(res.args) and all(res.args.get(k) == v for k, v in expected.items())
    return res


def summarise(label: str, results: list[CaseResult]) -> dict[str, Any]:
    media = [r for r in results if r.refusal_correct is None]
    off = [r for r in results if r.refusal_correct is not None]
    called = [r for r in media if r.args_valid is not None]
    with_expect = [r for r in media if r.args_match is not None]
    ttfts = sorted(r.ttft_ms for r in results if r.ttft_ms is not None)
    ratios = [r.host_tokens / r.billable_tokens for r in results if r.billable_tokens and r.host_tokens]

    def rate(xs, pred):
        return sum(1 for x in xs if pred(x)) / len(xs) if xs else 0.0

    summary = {
        "label": label,
        "tool_correct": rate(media, lambda r: r.tool_correct),
        "args_valid": rate(called, lambda r: r.args_valid),
        "args_match": rate(with_expect, lambda r: r.args_match),
        "refusals": rate(off, lambda r: r.refusal_correct),
        "p95_ttft_ms": ttfts[max(0, int(len(ttfts) * 0.95) - 1)] if ttfts else None,
        "overhead_ratio_median": statistics.median(ratios) if ratios else None,
        "avg_billable_tokens": statistics.mean(r.billable_tokens for r in results) if results else 0,
        "errors": sum(1 for r in results if r.error),
    }
    summary["passes_gate"] = all(summary[k] >= v for k, v in TARGETS.items()) and summary["errors"] == 0
    return summary


def render_markdown(summaries: list[dict[str, Any]], details: dict[str, list[CaseResult]]) -> str:
    def pct(x):
        return f"{x * 100:.0f}%"

    lines = [
        "# Model Bake-off Results (Gate G2)",
        "",
        f"Targets: tool selection ≥ {pct(TARGETS['tool_correct'])}, valid args ≥ {pct(TARGETS['args_valid'])}, "
        f"refusals ≥ {pct(TARGETS['refusals'])}, no errors.",
        "",
        "| Model | Tool selection | Valid args | Expected args | Refusals | p95 first token | "
        "Overhead ratio (median) | Avg billable tokens | Errors | Gate |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s in summaries:
        ttft = f"{s['p95_ttft_ms']:.0f} ms" if s["p95_ttft_ms"] is not None else "–"
        ratio = f"{s['overhead_ratio_median']:.1f}×" if s["overhead_ratio_median"] else "–"
        lines.append(
            f"| {s['label']} | {pct(s['tool_correct'])} | {pct(s['args_valid'])} | {pct(s['args_match'])} | "
            f"{pct(s['refusals'])} | {ttft} | {ratio} | {s['avg_billable_tokens']:.0f} | {s['errors']} | "
            f"{'✅ pass' if s['passes_gate'] else '❌ fail'} |"
        )
    for label, results in details.items():
        lines += ["", f"## {label}: failed cases", ""]
        failed = [r for r in results if r.tool_correct is False or r.refusal_correct is False or r.error]
        if not failed:
            lines.append("None.")
        for r in failed:
            lines.append(
                f"- **{r.case_id}**: called `{r.tool_called}` args={json.dumps(r.args)} "
                f"refused={r.refused} error={r.error} {'; '.join(r.notes)}"
            )
    return "\n".join(lines) + "\n"


async def run_all(models: list[dict[str, Any]], data: dict[str, Any]):
    meta = {k: data[k] for k in ("today", "timezone", "currency")}
    summaries, details = [], {}
    for m in models:
        client = OpenAICompatibleClient(m["base_url"], os.environ.get(m.get("api_key_env", ""), ""), m["model"])
        results = [await run_case(client, c, meta) for c in data["cases"]]
        summaries.append(summarise(m["label"], results))
        details[m["label"]] = results
    return summaries, details


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", required=True, help="JSON file listing candidate models")
    parser.add_argument("--cases", default="evals/bakeoff_cases.json")
    parser.add_argument("--out", default="docs/chat/BAKEOFF_RESULTS.md")
    args = parser.parse_args()
    data = json.loads(Path(args.cases).read_text())
    models = json.loads(Path(args.models).read_text())
    summaries, details = asyncio.run(run_all(models, data))
    report = render_markdown(summaries, details)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(report)
    print(report)


if __name__ == "__main__":
    main()

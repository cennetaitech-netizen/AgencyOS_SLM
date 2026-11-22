"""Release gate (Phase 11): a fine-tuned candidate replaces production only if it's equal or better.

Checks (on the frozen engine eval set, docs/IMPLEMENTATION_GUIDE.md Phase 11 task 4):
* the candidate meets the absolute targets (overall, actions, grounded 100%, nothing executed w/o confirmation)
* overall and every category ≥ production
* overhead ratio ≤ production (cheaper per billable token), p95 latency ≤ production × tolerance

Usage:
  python -m chat_service.ml.gate --production evals/prod.json --candidate evals/cand.json \\
      --database-url postgresql+asyncpg://.../chat_eval --out docs/chat/GATE_RESULTS.md
Each JSON file holds one model entry in the bake-off format. Exit code 1 if the gate fails.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any


def compare(candidate: dict[str, Any], production: dict[str, Any], latency_tolerance: float = 1.10) -> dict[str, Any]:
    checks: list[tuple[str, bool, str]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append((name, bool(ok), detail))

    add("absolute targets", candidate["passes_gate"], "candidate meets overall/actions/grounding targets")
    add(
        "overall ≥ production",
        candidate["overall"] >= production["overall"],
        f"{candidate['overall']:.0%} vs {production['overall']:.0%}",
    )
    for cat, prod_rate in production["by_category"].items():
        cand_rate = candidate["by_category"].get(cat)
        add(
            f"{cat} ≥ production",
            cand_rate is not None and prod_rate is not None and cand_rate >= prod_rate,
            f"{cand_rate} vs {prod_rate}",
        )
    add("grounded 100%", candidate["grounded"] >= 1.0, f"{candidate['grounded']:.0%}")
    add(
        "nothing executed without confirmation",
        candidate["executed_without_confirmation"] == 0,
        str(candidate["executed_without_confirmation"]),
    )
    if candidate.get("overhead_ratio") is not None and production.get("overhead_ratio") is not None:
        add(
            "overhead ratio ≤ production",
            candidate["overhead_ratio"] <= production["overhead_ratio"],
            f"{candidate['overhead_ratio']}× vs {production['overhead_ratio']}×",
        )
    if candidate.get("p95_latency_ms") is not None and production.get("p95_latency_ms") is not None:
        add(
            "p95 latency within tolerance",
            candidate["p95_latency_ms"] <= production["p95_latency_ms"] * latency_tolerance,
            f"{candidate['p95_latency_ms']} ms vs {production['p95_latency_ms']} ms",
        )
    return {"passed": all(ok for _, ok, _ in checks), "checks": checks}


def render(result: dict[str, Any], candidate: str, production: str) -> str:
    lines = [
        f"# Release gate: {candidate} vs {production}",
        "",
        f"**Result: {'✅ PASS, may start a canary' if result['passed'] else '❌ FAIL, do not deploy'}**",
        "",
        "| Check | Result | Detail |",
        "|---|---|---|",
    ]
    lines += [f"| {n} | {'✅' if ok else '❌'} | {d} |" for n, ok, d in result["checks"]]
    return "\n".join(lines) + "\n"


def main() -> None:
    from chat_service.config import Settings
    from chat_service.db import create_engine, metadata
    from chat_service.evals.engine_eval import run_all
    from chat_service.llm.client import OpenAICompatibleClient

    parser = argparse.ArgumentParser()
    parser.add_argument("--production", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--cases", default="evals/chat_eval_cases.json")
    parser.add_argument("--database-url", default=os.environ.get("CHAT_EVAL_DATABASE_URL"))
    parser.add_argument("--out", default="docs/chat/GATE_RESULTS.md")
    args = parser.parse_args()
    if not args.database_url:
        sys.exit("--database-url is required (dedicated eval database)")
    data = json.loads(Path(args.cases).read_text())
    prod_cfg, cand_cfg = (json.loads(Path(p).read_text()) for p in (args.production, args.candidate))

    def factory(cfg):
        return lambda case: OpenAICompatibleClient(
            cfg["base_url"], os.environ.get(cfg.get("api_key_env", ""), ""), cfg["model"]
        )

    async def go():
        db = create_engine(args.database_url)
        async with db.begin() as conn:
            await conn.run_sync(metadata.create_all)
        try:
            summaries, _ = await run_all(
                db,
                Settings(env="test", database_url=args.database_url),
                {"production": factory(prod_cfg), "candidate": factory(cand_cfg)},
                data,
            )
            return summaries
        finally:
            await db.dispose()

    prod_summary, cand_summary = asyncio.run(go())
    result = compare(cand_summary, prod_summary)
    report = render(result, cand_cfg["label"], prod_cfg["label"])
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(report)
    print(report)
    sys.exit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()

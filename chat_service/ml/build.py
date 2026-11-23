"""Build a versioned training dataset snapshot (Phase 11).

  python -m chat_service.ml.build --database-url postgresql+asyncpg://... --out ml/data/v1 \\
      [--corrections ml/corrections.jsonl] [--teacher evals/teacher.json --per-task 20]

Writes: train.jsonl · validation.jsonl · dpo.jsonl · report.json (counts, drop reasons, tools, prompt version).
``--teacher`` adds synthetic conversations (needs a dedicated sandbox database: synthetic orgs are created there).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from chat_service.config import Settings
from chat_service.crypto import FieldCipher
from chat_service.db import create_engine
from chat_service.engine.prompting import load_prompt
from chat_service.llm.client import OpenAICompatibleClient
from chat_service.ml.dataset import export_records, load_corrections, write_jsonl, write_splits
from chat_service.tools import build_registry
from chat_service.tools.product import MockProductAPI


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--prompt-version", default="system_ft_v2")
    parser.add_argument("--corrections", type=Path)
    parser.add_argument("--eval-cases", default="evals/chat_eval_cases.json")
    parser.add_argument("--teacher", help="JSON model entry (bake-off format) for synthetic data")
    parser.add_argument("--synthetic-database-url", help="sandbox DB for synthetic runs (defaults to --database-url)")
    parser.add_argument("--per-task", type=int, default=20)
    args = parser.parse_args()

    eval_data = json.loads(Path(args.eval_cases).read_text())
    eval_questions = [c["question"] for c in eval_data["cases"]]
    template = load_prompt(args.prompt_version)
    settings = Settings(env="test", database_url=args.database_url)
    tools = build_registry(settings, MockProductAPI()).schemas(include_write=True)
    corrections = load_corrections(args.corrections)
    teacher_cfg = json.loads(Path(args.teacher).read_text()) if args.teacher else None

    async def go():
        db = create_engine(args.database_url)
        try:
            records, pairs, report = await export_records(
                db, template, tools, eval_questions, corrections, cipher=FieldCipher(settings.data_encryption_keys)
            )
        finally:
            await db.dispose()
        synth_report = None
        if args.teacher:
            from chat_service.ml.synthetic import run_synthetic

            cfg = teacher_cfg
            teacher = OpenAICompatibleClient(
                cfg["base_url"], os.environ.get(cfg.get("api_key_env", ""), ""), cfg["model"]
            )
            sdb = create_engine(args.synthetic_database_url or args.database_url)
            try:
                meta = {k: eval_data[k] for k in ("today", "timezone", "currency")}
                s_records, s_pairs, _, synth_report = await run_synthetic(
                    sdb, settings, teacher, meta, template, eval_questions, per_task=args.per_task
                )
            finally:
                await sdb.dispose()
            records += s_records
            pairs += s_pairs
        return records, pairs, report, synth_report

    records, pairs, report, synth = asyncio.run(go())
    out = Path(args.out)
    splits = write_splits(records, out)
    write_jsonl(pairs, out / "dpo.jsonl")
    summary = {
        "prompt_version": args.prompt_version,
        "splits": splits,
        "dpo_pairs": len(pairs),
        "consented": report.__dict__,
        "synthetic": synth.__dict__ if synth else None,
        "tools": [t["function"]["name"] for t in tools],
    }
    (out / "report.json").write_text(json.dumps(summary, indent=2, default=str))
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()

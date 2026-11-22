"""Shared helpers for the training scripts (run in the ML environment, not the service).

Records (from ``chat_service.ml.build``) are tool-calling chat transcripts. Each assistant turn (tool call or final
answer) becomes one prompt → completion example rendered with the base model's own chat template, so loss is
computed only on what the assistant produced.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def _normalise(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for m in messages:
        m = dict(m)
        if m.get("tool_calls"):
            calls = []
            for c in m["tool_calls"]:
                fn = dict(c["function"])
                if isinstance(fn.get("arguments"), str):
                    fn["arguments"] = json.loads(fn["arguments"] or "{}")
                calls.append({**c, "function": fn})
            m["tool_calls"] = calls
            if m.get("content") is None:
                m["content"] = ""
        out.append(m)
    return out


def build_sft_examples(tokenizer, records: list[dict[str, Any]]) -> tuple[list[dict[str, str]], dict[str, int]]:
    examples: list[dict[str, str]] = []
    stats = {"records": len(records), "examples": 0, "skipped_non_prefix": 0}
    for record in records:
        msgs = _normalise(record["messages"])
        tools = record.get("tools") or None
        for i, m in enumerate(msgs):
            if m["role"] != "assistant":
                continue
            prompt = tokenizer.apply_chat_template(msgs[:i], tools=tools, add_generation_prompt=True, tokenize=False)
            full = tokenizer.apply_chat_template(msgs[: i + 1], tools=tools, tokenize=False)
            if not full.startswith(prompt):
                stats["skipped_non_prefix"] += 1
                continue
            examples.append({"prompt": prompt, "completion": full[len(prompt) :]})
    stats["examples"] = len(examples)
    return examples, stats


def build_dpo_examples(tokenizer, pairs: list[dict[str, Any]]) -> list[dict[str, str]]:
    eos = tokenizer.eos_token or ""
    out = []
    for p in pairs:
        prompt = tokenizer.apply_chat_template(
            _normalise(p["prompt"]), tools=p.get("tools") or None, add_generation_prompt=True, tokenize=False
        )
        out.append({"prompt": prompt, "chosen": p["chosen"] + eos, "rejected": p["rejected"] + eos})
    return out


def load_config(path: str | Path, overrides: dict[str, Any]) -> dict[str, Any]:
    cfg = json.loads(Path(path).read_text())
    cfg.update({k: v for k, v in overrides.items() if v is not None})
    return cfg


def warmup_kwargs(config_cls, ratio: float) -> dict[str, Any]:
    """Transformers ≥5 folds warmup_ratio into warmup_steps (a float < 1 is a ratio); older versions don't."""
    import inspect

    params = inspect.signature(config_cls.__init__).parameters
    return {"warmup_ratio": ratio} if "warmup_ratio" in params else {"warmup_steps": ratio}

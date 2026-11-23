"""Training-data builder (Phase 11).

Sources:
* **consented conversations**: only orgs with ``training_consent = true``, from ``chat_messages`` + full tool
  calls (``chat_tool_calls.args/result``), rebuilt as tool-calling chat transcripts
* **SME corrections**: a JSONL file ``{"message_id": ..., "corrected_answer": ...}`` for down-rated answers
* **synthetic conversations**: produced by ``chat_service.ml.synthetic`` (exported through the same path)

Rules: personal data scrubbed · down-rated answers dropped unless corrected · failed/empty answers dropped ·
answers flagged as ungrounded dropped · duplicates removed · **eval questions never included** · deterministic
train/validation split by hash · DPO pairs = (corrected answer ≻ original down-rated answer).

Output records (JSONL), OpenAI-style messages with tool calls (arguments as objects):
  {"messages": [...], "tools": [...], "meta": {"source", "message_id", "org": <hashed>}}
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from itertools import groupby
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from chat_service.crypto import FieldCipher
from chat_service.db.schema import chat_conversations, chat_feedback, chat_messages, chat_org_settings, chat_tool_calls
from chat_service.engine.prompting import PromptContext, build_messages

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"(?<!\w)(?:\+\d{1,3}[\s-]?)?(?:\d[\s-]?){9,12}\d(?!\w)")
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")


def scrub(text: str) -> str:
    """Remove personal data. Numbers that look like metrics (with commas/decimals) are left alone."""
    text = _EMAIL.sub("<EMAIL>", text)
    text = _CARD.sub(lambda m: "<NUMBER>" if not re.search(r"[,.]", m.group(0)) else m.group(0), text)
    text = _PHONE.sub(
        lambda m: "<PHONE>" if (" " in m.group(0) or "-" in m.group(0) or m.group(0).startswith("+")) else m.group(0),
        text,
    )
    return text


def _scrub_obj(obj: Any) -> Any:
    if isinstance(obj, str):
        return scrub(obj)
    if isinstance(obj, list):
        return [_scrub_obj(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _scrub_obj(v) for k, v in obj.items()}
    return obj


def normalise_question(text: str) -> str:
    return re.sub(r"\W+", " ", text.lower()).strip()


@dataclass
class BuildReport:
    conversations: int = 0
    examples: int = 0
    dropped: dict[str, int] = field(default_factory=dict)
    corrected: int = 0
    dpo_pairs: int = 0

    def drop(self, reason: str) -> None:
        self.dropped[reason] = self.dropped.get(reason, 0) + 1


def _hash(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def is_validation(record: dict[str, Any], fraction: float = 0.05) -> bool:
    key = record["meta"].get("message_id") or json.dumps(record["messages"], sort_keys=True)
    return int(_hash(key)[:8], 16) % 10_000 < fraction * 10_000


def prompt_prefix(
    template: str,
    created_at,
    timezone: str,
    currency: str,
    history: list[dict[str, Any]],
    user_text: str,
    placement: str = "user_prefix",
) -> list[dict[str, Any]]:
    """System prompt, history and the user's message laid out exactly as the engine sends them
    (engine/prompting.py), with the context as of the message's own date in the org's timezone."""
    ctx = PromptContext.at(created_at, timezone, currency, role="member")
    return build_messages(template, ctx, history, user_text, placement)


async def export_records(
    engine: AsyncEngine,
    system_template: str,
    tools: list[dict[str, Any]],
    eval_questions: Iterable[str],
    corrections: dict[str, str] | None = None,
    org_ids: list[uuid.UUID] | None = None,
    source: str = "consented",
    cipher: FieldCipher | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], BuildReport]:
    """Return (SFT records, DPO pairs, report). ``org_ids`` restricts the export (e.g. synthetic runs);
    consent is always required."""
    corrections = corrections or {}
    cipher = cipher or FieldCipher()
    banned = {normalise_question(q) for q in eval_questions}
    report = BuildReport()
    records: list[dict[str, Any]] = []
    pairs: list[dict[str, Any]] = []
    seen: set[str] = set()
    m, c, tc = chat_messages.c, chat_conversations.c, chat_tool_calls.c

    async with engine.connect() as conn:
        os_ = chat_org_settings.c
        consented = select(os_.org_id).where(os_.training_consent.is_(True))
        q = (
            select(c.id, c.org_id, os_.timezone, os_.currency)
            .join(chat_org_settings, os_.org_id == c.org_id)
            .where(c.org_id.in_(consented))
        )
        if org_ids is not None:
            q = q.where(c.org_id.in_(org_ids))
        convs = (await conn.execute(q)).all()
        for conv in convs:
            report.conversations += 1
            msgs = (
                await conn.execute(
                    select(m.id, m.role, m.content, m.flags, m.created_at)
                    .where(m.conversation_id == conv.id)
                    .order_by(m.created_at, m.id)
                )
            ).all()
            history: list[dict[str, Any]] = []
            pending_user: str | None = None
            for msg in msgs:
                if msg.role == "user":
                    pending_user = msg.content
                    continue
                if pending_user is None:
                    continue
                user_text = pending_user
                pending_user = None
                if not msg.content.strip():
                    report.drop("empty_or_failed")
                    continue
                ratings = (
                    (await conn.execute(select(chat_feedback.c.rating).where(chat_feedback.c.message_id == msg.id)))
                    .scalars()
                    .all()
                )
                corrected = corrections.get(str(msg.id))
                answer = corrected or msg.content
                calls = (
                    await conn.execute(
                        select(tc.tool_name, tc.args, tc.result, tc.ok, tc.error, tc.step)
                        .where(tc.message_id == msg.id)
                        .order_by(tc.step, tc.created_at)
                    )
                ).all()
                turn: list[dict[str, Any]] = [{"role": "user", "content": user_text}]
                n = 0
                for _, step_calls in groupby(calls, key=lambda x: x.step):  # one assistant turn per step
                    step_calls = list(step_calls)
                    ids = [f"call_{n + i}" for i in range(len(step_calls))]
                    n += len(step_calls)
                    turn.append(
                        {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": cid,
                                    "type": "function",
                                    "function": {"name": call.tool_name, "arguments": call.args},
                                }
                                for cid, call in zip(ids, step_calls, strict=True)
                            ],
                        }
                    )
                    for cid, call in zip(ids, step_calls, strict=True):
                        content = cipher.decrypt_json(call.result) if call.ok else {"error": call.error}
                        turn.append(
                            {
                                "role": "tool",
                                "tool_call_id": cid,
                                "name": call.tool_name,
                                "content": json.dumps(content, default=str, separators=(",", ":")),
                            }
                        )
                good_answer = turn + [{"role": "assistant", "content": answer}]
                keep = True
                if normalise_question(user_text) in banned:
                    report.drop("eval_question")
                    keep = False
                elif "down" in ratings and not corrected:
                    report.drop("down_rated_uncorrected")
                    keep = False
                elif msg.flags and msg.flags.get("ungrounded_numbers") and not corrected:
                    report.drop("ungrounded")
                    keep = False
                else:
                    key = _hash(normalise_question(user_text), answer)
                    if key in seen:
                        report.drop("duplicate")
                        keep = False
                    seen.add(key)
                prefix = prompt_prefix(
                    system_template, msg.created_at, conv.timezone, conv.currency, history, user_text
                )
                if keep:
                    record = {
                        "messages": _scrub_obj([*prefix, *good_answer[1:]]),  # turn[0] is the user message
                        "tools": tools,
                        "meta": {
                            "source": "correction" if corrected else source,
                            "message_id": str(msg.id),
                            "org": _hash(str(conv.org_id))[:12],
                        },
                    }
                    records.append(record)
                    report.examples += 1
                    report.corrected += bool(corrected)
                    if corrected and corrected != msg.content:
                        pairs.append(
                            {
                                "prompt": _scrub_obj([*prefix, *turn[1:]]),
                                "chosen": scrub(corrected),
                                "rejected": scrub(msg.content),
                                "tools": tools,
                                "meta": {"message_id": str(msg.id)},
                            }
                        )
                        report.dpo_pairs += 1
                # Only text turns are carried forward as history (as the engine does at inference).
                history += [{"role": "user", "content": user_text}, {"role": "assistant", "content": answer}]
    return records, pairs, report


def write_splits(records: list[dict[str, Any]], out_dir: Path, val_fraction: float = 0.05) -> dict[str, int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    train = [r for r in records if not is_validation(r, val_fraction)]
    val = [r for r in records if is_validation(r, val_fraction)]
    for name, rows in (("train.jsonl", train), ("validation.jsonl", val)):
        with (out_dir / name).open("w") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    return {"train": len(train), "validation": len(val)}


def write_jsonl(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")


def load_corrections(path: Path | None) -> dict[str, str]:
    if path is None or not path.exists():
        return {}
    out = {}
    for line in path.read_text().splitlines():
        if line.strip():
            row = json.loads(line)
            out[row["message_id"]] = row["corrected_answer"]
    return out

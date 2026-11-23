"""Builds the messages sent to the model, laid out so the model host's prefix cache can reuse as much as possible.

The model host (vLLM with prefix caching, or a managed host with prompt caching) skips recomputing any leading
part of a prompt it has seen recently. So the prompt is ordered from most to least shared:

  1. system prompt: identical for every org, user and day (no placeholders)
  2. tool schemas: identical for every org with the same tools (the chat template puts them next to the system prompt)
  3. conversation history: identical to the previous turn's
  4. a short ``[Context]`` system message (date, timezone, currency, role), then the latest user message

Only part 4 changes from request to request. The context is never stored in history and never billed. It goes in its
own system message by default: small models tested on a CPU (see LAUNCH_READINESS.md) answered or repeated it when it
was inside the user's message. For a model whose chat template rejects a second system message, set
``CHAT_PROMPT_CONTEXT_PLACEMENT=user_prefix`` to put it on the first line of the user's message instead.

Older prompt versions (``system_v1``, ``system_v2``, ``system_ft_v1``) still put the context in the system prompt
through placeholders; they keep working so a rollback is a config change.
"""

from __future__ import annotations

import string
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

PROMPTS_DIR = Path(__file__).parent / "prompts"
CONTEXT_TAG = "[Context]"


def load_prompt(version: str) -> str:
    return (PROMPTS_DIR / f"{version}.txt").read_text()


def is_static(template: str) -> bool:
    """True when the prompt has no placeholders, i.e. it is the same for everyone (cache-friendly)."""
    return not any(field for _, field, _, _ in string.Formatter().parse(template) if field is not None)


@dataclass(frozen=True)
class PromptContext:
    today: str  # ISO date in the org's timezone
    timezone: str
    currency: str
    role: str
    display_name: str = "User"  # only used by the older placeholder prompts

    @classmethod
    def at(cls, when: datetime, timezone: str, currency: str, role: str, display_name: str = "User") -> PromptContext:
        return cls(when.astimezone(ZoneInfo(timezone)).date().isoformat(), timezone, currency, role, display_name)

    def line(self) -> str:
        return f"{CONTEXT_TAG} Today: {self.today} ({self.timezone}) · Currency: {self.currency} · Role: {self.role}"


def build_messages(
    template: str,
    ctx: PromptContext,
    history: list[dict[str, Any]],
    user_text: str,
    placement: str = "system_message",
) -> list[dict[str, Any]]:
    """System prompt + history + context + the latest user message, in cache-friendly order."""
    if is_static(template):
        if placement == "user_prefix":
            latest = [{"role": "user", "content": f"{ctx.line()}\n\n{user_text}"}]
        else:
            latest = [{"role": "system", "content": ctx.line()}, {"role": "user", "content": user_text}]
        return [{"role": "system", "content": template}, *history, *latest]
    system = template.format(
        today_date=ctx.today,
        org_timezone=ctx.timezone,
        org_currency=ctx.currency,
        user_display_name=ctx.display_name,
        user_role=ctx.role,
    )
    return [{"role": "system", "content": system}, *history, {"role": "user", "content": user_text}]

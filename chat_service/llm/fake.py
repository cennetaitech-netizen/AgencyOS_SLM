"""Scripted fake model for tests and offline development."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from chat_service.llm.client import ModelError, ModelEvent, StreamEnd, TextDelta, ToolCall


@dataclass
class FakeTurn:
    text: str = ""
    tool_calls: list[tuple[str, dict[str, Any] | str]] = field(default_factory=list)
    fail_before_first_token: bool = False
    fail_after_text: bool = False
    cached_prompt_tokens: int | None = None  # what the host reports as served from its prefix cache


class FakeModelClient:
    """Plays back ``turns`` in order, one per model call. Records every request it receives."""

    def __init__(self, turns: list[FakeTurn], name: str = "fake-slm") -> None:
        self.turns = list(turns)
        self.name = name
        self.requests: list[dict[str, Any]] = []

    async def stream(self, messages, tools, max_tokens, temperature) -> AsyncIterator[ModelEvent]:
        self.requests.append({"messages": messages, "tools": tools, "max_tokens": max_tokens})
        if not self.turns:
            raise ModelError("fake model has no more scripted turns")
        turn = self.turns.pop(0)
        if turn.fail_before_first_token:
            raise ModelError("scripted failure", before_first_token=True)
        words = turn.text.split(" ") if turn.text else []
        for i, w in enumerate(words):
            yield TextDelta(w if i == 0 else " " + w)
        if turn.fail_after_text:
            raise ModelError("scripted mid-stream failure", before_first_token=False)
        calls = [
            ToolCall(id=f"call_{i}", name=n, arguments=a if isinstance(a, str) else json.dumps(a))
            for i, (n, a) in enumerate(turn.tool_calls)
        ]
        prompt_chars = sum(len(str(m.get("content") or "")) for m in messages) + len(json.dumps(tools or []))
        yield StreamEnd(
            finish_reason="tool_calls" if calls else "stop",
            tool_calls=calls,
            prompt_tokens=prompt_chars // 4,
            completion_tokens=max(1, len(turn.text) // 4) + 10 * len(calls),
            cached_prompt_tokens=turn.cached_prompt_tokens,
        )

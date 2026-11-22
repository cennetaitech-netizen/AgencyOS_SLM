"""OpenAI-compatible streaming chat client (works with vLLM, Together, Fireworks, DeepInfra, Groq, ...).

The engine depends only on the ``ModelClient`` protocol, so the base model and our fine-tuned SLM can be
swapped without code changes.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON string, validated by the tool registry


@dataclass
class TextDelta:
    text: str


@dataclass
class StreamEnd:
    finish_reason: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


ModelEvent = TextDelta | StreamEnd


class ModelError(Exception):
    def __init__(self, message: str, before_first_token: bool = True) -> None:
        super().__init__(message)
        self.before_first_token = before_first_token


class ModelClient(Protocol):
    name: str

    def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[ModelEvent]: ...


class OpenAICompatibleClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 60.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.name = model
        self._model = model
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = http_client or httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout_seconds)

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[ModelEvent]:
        body: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        started = False
        calls: dict[int, dict[str, str]] = {}
        finish_reason: str | None = None
        usage: dict[str, int] = {}
        try:
            async with self._client.stream("POST", "/chat/completions", json=body, headers=self._headers) as resp:
                if resp.status_code >= 400:
                    detail = (await resp.aread()).decode(errors="replace")[:300]
                    raise ModelError(f"model host returned {resp.status_code}: {detail}")
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    chunk = json.loads(payload)
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if delta.get("content"):
                            started = True
                            yield TextDelta(delta["content"])
                        for tc in delta.get("tool_calls") or []:
                            started = True
                            slot = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "arguments": ""})
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                slot["name"] += fn["name"]
                            if fn.get("arguments"):
                                slot["arguments"] += fn["arguments"]
                        if choice.get("finish_reason"):
                            finish_reason = choice["finish_reason"]
        except ModelError:
            raise
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise ModelError(f"model stream failed: {type(exc).__name__}", before_first_token=not started) from exc
        tool_calls = [
            ToolCall(id=c["id"] or f"call_{i}", name=c["name"], arguments=c["arguments"] or "{}")
            for i, c in sorted(calls.items())
        ]
        yield StreamEnd(
            finish_reason=finish_reason,
            tool_calls=tool_calls,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
        )


class FallbackModelClient:
    """Primary host, with one retry on the fallback host if the primary fails before the first token."""

    def __init__(self, primary: ModelClient, fallback: ModelClient | None = None) -> None:
        self.primary = primary
        self.fallback = fallback
        self.name = primary.name
        self.last_serving_host = "primary"

    async def stream(self, messages, tools, max_tokens, temperature) -> AsyncIterator[ModelEvent]:
        self.last_serving_host = "primary"
        try:
            async for ev in self.primary.stream(messages, tools, max_tokens, temperature):
                yield ev
            return
        except ModelError as exc:
            if not (exc.before_first_token and self.fallback):
                raise
        self.last_serving_host = "fallback"
        from chat_service.metrics import MODEL_FALLBACK

        MODEL_FALLBACK.inc()
        async for ev in self.fallback.stream(messages, tools, max_tokens, temperature):
            yield ev

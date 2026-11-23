"""OpenAI-compatible streaming chat client (works with vLLM, Together, Fireworks, DeepInfra, Groq, Ollama, and
Google Gemini's OpenAI-compatible endpoint).

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
    # Provider data that must be sent back with the call on the next turn (Gemini puts its thought signature in
    # ``extra_content``; Gemini 3 models reject a follow-up turn without it).
    extra_content: dict[str, Any] | None = None


@dataclass
class TextDelta:
    text: str


@dataclass
class StreamEnd:
    finish_reason: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cached_prompt_tokens: int | None = None  # part of prompt_tokens served from the host's prefix cache


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


# JSON-schema "format" values Gemini accepts on tool parameters; it rejects others (e.g. "date").
_GEMINI_FORMATS = {"enum", "date-time", "float", "double", "int32", "int64"}


def gemini_schema(schema: Any) -> Any:
    """Tool parameter schema in the subset Gemini's function calling accepts."""
    if isinstance(schema, list):
        return [gemini_schema(x) for x in schema]
    if not isinstance(schema, dict):
        return schema
    return {
        k: gemini_schema(v) if k != "properties" else {n: gemini_schema(sub) for n, sub in v.items()}
        for k, v in schema.items()
        if not (k == "format" and v not in _GEMINI_FORMATS) and k not in ("$schema", "examples")
    }


def is_gemini(base_url: str) -> bool:
    return "generativelanguage.googleapis.com" in base_url


class OpenAICompatibleClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 60.0,
        http_client: httpx.AsyncClient | None = None,
        name: str | None = None,
    ) -> None:
        self.name = name or model  # the name used for routing, pinning and usage records
        self._model = model  # the model id sent to the host
        self._gemini = is_gemini(base_url)
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
            if self._gemini:
                tools = [
                    t | {"function": t["function"] | {"parameters": gemini_schema(t["function"].get("parameters"))}}
                    for t in tools
                ]
            body["tools"] = tools
            body["tool_choice"] = "auto"
        started = False
        calls: dict[int, dict[str, Any]] = {}
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
                            slot = calls.setdefault(_slot(calls, tc), {"id": "", "name": "", "arguments": ""})
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            if tc.get("extra_content"):
                                slot["extra_content"] = tc["extra_content"]
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
            ToolCall(
                id=c["id"] or f"call_{i}",
                name=c["name"],
                arguments=c["arguments"] or "{}",
                extra_content=c.get("extra_content"),
            )
            for i, c in sorted(calls.items())
        ]
        yield StreamEnd(
            finish_reason=finish_reason,
            tool_calls=tool_calls,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            # OpenAI-compatible hosts report cache hits here (vLLM needs --enable-prompt-tokens-details)
            cached_prompt_tokens=(usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
        )


def _slot(calls: dict[int, dict[str, Any]], tc: dict[str, Any]) -> int:
    """Which tool call a streamed fragment belongs to. OpenAI sends ``index``; some hosts (Gemini) leave it out
    and send each call whole, with its own id."""
    if tc.get("index") is not None:
        return int(tc["index"])
    if tc.get("id"):
        for i, c in calls.items():
            if c["id"] == tc["id"]:
                return i
        return len(calls)
    return max(calls, default=0)


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

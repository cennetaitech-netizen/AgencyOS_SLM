from __future__ import annotations

import json

import httpx
import pytest

from chat_service.llm.client import FallbackModelClient, ModelError, OpenAICompatibleClient, StreamEnd, TextDelta


def sse(chunks: list[dict]) -> bytes:
    return ("".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n").encode()


def client_for(handler) -> OpenAICompatibleClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://host/v1")
    return OpenAICompatibleClient("http://host/v1", "secret", "slm-8b", http_client=http)


async def collect(client, tools=None):
    return [ev async for ev in client.stream([{"role": "user", "content": "hi"}], tools, 100, 0.2)]


async def test_text_stream_and_usage():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(
            200,
            content=sse(
                [
                    {"choices": [{"delta": {"content": "Hello"}}]},
                    {"choices": [{"delta": {"content": " world"}, "finish_reason": "stop"}]},
                    {
                        "choices": [],
                        "usage": {
                            "prompt_tokens": 120,
                            "completion_tokens": 2,
                            "prompt_tokens_details": {"cached_tokens": 96},
                        },
                    },
                ]
            ),
        )

    events = await collect(client_for(handler))
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["Hello", " world"]
    end = events[-1]
    assert isinstance(end, StreamEnd) and end.finish_reason == "stop"
    assert (end.prompt_tokens, end.completion_tokens, end.cached_prompt_tokens) == (120, 2, 96)
    assert seen["body"]["stream"] is True and seen["body"]["stream_options"] == {"include_usage": True}
    assert seen["auth"] == "Bearer secret" and "tools" not in seen["body"]


async def test_tool_call_fragments_are_assembled():
    def handler(request):
        return httpx.Response(
            200,
            content=sse(
                [
                    {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "list_"}}]}}]},
                    {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "campaigns"}}]}}]},
                    {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"client_'}}]}}]},
                    {
                        "choices": [
                            {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'id":"cl_acme"}'}}]}}
                        ]
                    },
                    {
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 1,
                                            "id": "c2",
                                            "function": {"name": "list_clients", "arguments": "{}"},
                                        }
                                    ]
                                },
                                "finish_reason": "tool_calls",
                            }
                        ]
                    },
                ]
            ),
        )

    events = await collect(client_for(handler), tools=[{"type": "function"}])
    end = events[-1]
    assert [(c.id, c.name, json.loads(c.arguments)) for c in end.tool_calls] == [
        ("c1", "list_campaigns", {"client_id": "cl_acme"}),
        ("c2", "list_clients", {}),
    ]


async def test_http_error_raises_model_error_before_first_token():
    def handler(request):
        return httpx.Response(503, text="overloaded")

    with pytest.raises(ModelError) as exc:
        await collect(client_for(handler))
    assert exc.value.before_first_token and "503" in str(exc.value)


async def test_fallback_on_primary_failure():
    def bad(request):
        return httpx.Response(500, text="down")

    def good(request):
        return httpx.Response(200, content=sse([{"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}]))

    fb = FallbackModelClient(client_for(bad), client_for(good))
    events = await collect(fb)
    assert events[0].text == "ok" and fb.last_serving_host == "fallback"

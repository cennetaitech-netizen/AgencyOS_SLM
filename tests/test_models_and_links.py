"""More model hosts (Gemini, Ollama, any OpenAI-compatible), staff model choice, and search links on refusals."""

from __future__ import annotations

import json
import uuid
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy import select

from chat_service.api.app import create_app, default_model_name, model_clients
from chat_service.config import Settings
from chat_service.db.schema import chat_messages
from chat_service.engine.agent import REFUSAL_SENTENCE
from chat_service.engine.search_links import search_links
from chat_service.llm.client import OpenAICompatibleClient, StreamEnd, gemini_schema
from chat_service.llm.fake import FakeModelClient, FakeTurn
from chat_service.llm.router import ModelRouter, RolloutStore
from chat_service.tools.product import MockProductAPI
from tests.test_engine import collect, make_engine, principal  # noqa: F401 - fixture

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai"


def sse(chunks: list[dict]) -> bytes:
    return ("".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n").encode()


def client_for(handler, base_url="http://host/v1") -> OpenAICompatibleClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=base_url)
    return OpenAICompatibleClient(base_url, "secret", "m", http_client=http)


async def stream_end(client, tools=None) -> StreamEnd:
    events = [ev async for ev in client.stream([{"role": "user", "content": "hi"}], tools, 100, 0.2)]
    assert isinstance(events[-1], StreamEnd)
    return events[-1]


# ---------------------------------------------------------------- client


async def test_tool_calls_without_index_and_with_thought_signatures():
    """Gemini sends each tool call whole, without ``index``, and a thought signature that must be sent back."""
    sig = {"google": {"thought_signature": "abc"}}

    def handler(request):
        return httpx.Response(
            200,
            content=sse(
                [
                    {
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "id": "a",
                                            "type": "function",
                                            "function": {"name": "list_clients", "arguments": "{}"},
                                            "extra_content": sig,
                                        },
                                        {
                                            "id": "b",
                                            "type": "function",
                                            "function": {"name": "list_campaigns", "arguments": '{"status":"active"}'},
                                        },
                                    ]
                                },
                                "finish_reason": "stop",
                            }
                        ]
                    }
                ]
            ),
        )

    end = await stream_end(client_for(handler))
    assert [(c.id, c.name, c.arguments) for c in end.tool_calls] == [
        ("a", "list_clients", "{}"),
        ("b", "list_campaigns", '{"status":"active"}'),
    ]
    assert end.tool_calls[0].extra_content == sig and end.tool_calls[1].extra_content is None


def test_gemini_schema_drops_unsupported_formats():
    schema = {
        "type": "object",
        "properties": {
            "date_from": {"type": "string", "format": "date"},
            "at": {"type": "string", "format": "date-time"},
            "status": {"type": "string", "enum": ["active", "paused"]},
            "rows": {"type": "array", "items": {"type": "string", "format": "uri"}},
        },
    }
    out = gemini_schema(schema)
    props = out["properties"]
    assert "format" not in props["date_from"] and props["at"]["format"] == "date-time"
    assert props["status"]["enum"] == ["active", "paused"] and "format" not in props["rows"]["items"]


async def test_gemini_client_sends_filtered_tools():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=sse([{"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}]))

    tool = {
        "type": "function",
        "function": {
            "name": "t",
            "parameters": {"type": "object", "properties": {"d": {"type": "string", "format": "date"}}},
        },
    }
    await stream_end(client_for(handler, GEMINI_URL), [tool])
    assert seen["url"] == f"{GEMINI_URL}/chat/completions"
    assert "format" not in seen["body"]["tools"][0]["function"]["parameters"]["properties"]["d"]
    assert seen["body"]["stream_options"] == {"include_usage": True}


# ---------------------------------------------------------------- configuration


def test_model_clients_and_default():
    s = Settings(env="test")
    assert set(model_clients(s)) == {s.model_name} and default_model_name(s, model_clients(s)) == s.model_name

    s = Settings(
        env="test",
        gemini_api_key="k",
        ollama_base_url="http://host.docker.internal:11434/v1",
        ollama_models="qwen2.5:7b,llama3.2:3b",
        models='[{"name": "together-qwen", "base_url": "https://api.together.xyz/v1", "model": "Qwen/Q", "api_key": "t"}]',
    )
    clients = model_clients(s)
    assert set(clients) == {s.model_name, "gemini-3.5-flash-lite", "qwen2.5:7b", "llama3.2:3b", "together-qwen"}
    assert clients["together-qwen"].name == "together-qwen" and clients["together-qwen"]._model == "Qwen/Q"
    assert default_model_name(s, clients) == "gemini-3.5-flash-lite"  # Gemini first when its key is set
    assert default_model_name(s.model_copy(update={"gemini_api_key": ""}), clients) == "qwen2.5:7b"
    assert default_model_name(s.model_copy(update={"default_model": "llama3.2:3b"}), clients) == "llama3.2:3b"
    with pytest.raises(RuntimeError, match="not a configured model"):
        default_model_name(s.model_copy(update={"default_model": "nope"}), clients)


def test_blank_settings_mean_unset():
    s = Settings(env="test", ollama_base_url="", default_model="", models="")
    assert s.ollama_base_url is None and s.default_model is None and s.models == []


# ---------------------------------------------------------------- staff: choosing models


async def test_staff_sets_default_model_and_pins_orgs(engine, settings):
    clients = {n: FakeModelClient([], name=n) for n in ("media-slm-dev", "gemini-3.5-flash-lite", "qwen2.5:7b")}
    router = ModelRouter(RolloutStore(engine, "gemini-3.5-flash-lite"), clients)
    app = create_app(settings, db=engine, model=router, product_api=MockProductAPI())
    async with app.router.lifespan_context(app):
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
        staff = {"X-Dev-User-Id": str(uuid.uuid4()), "X-Dev-Org-Id": str(uuid.uuid4()), "X-Dev-Roles": "agencyos_staff"}
        st = (await c.get("/chat/admin/model-rollout", headers=staff)).json()
        assert st["default_model"] == "gemini-3.5-flash-lite" and "qwen2.5:7b" in st["available_models"]

        r = await c.post("/chat/admin/model-rollout/production", json={"model": "qwen2.5:7b"}, headers=staff)
        assert r.status_code == 200 and r.json()["production_model"] == "qwen2.5:7b"
        assert (await c.get("/chat/admin/model-rollout", headers=staff)).json()["default_model"] == "qwen2.5:7b"
        assert (await router.select(uuid.uuid4())).name == "qwen2.5:7b"
        r = await c.post("/chat/admin/model-rollout/production", json={"model": "nope"}, headers=staff)
        assert r.status_code == 409

        org = uuid.uuid4()
        r = await c.put(f"/chat/admin/orgs/{org}/settings", json={"model_pin": "nope"}, headers=staff)
        assert r.status_code == 422
        r = await c.put(f"/chat/admin/orgs/{org}/settings", json={"model_pin": "media-slm-dev"}, headers=staff)
        assert r.json()["model_pin"] == "media-slm-dev"
        assert (await router.select(org, "media-slm-dev")).name == "media-slm-dev"
        r = await c.put(f"/chat/admin/orgs/{org}/settings", json={"model_pin": ""}, headers=staff)
        assert r.json()["model_pin"] is None


# ---------------------------------------------------------------- off-topic: search links, no answer


def test_search_links_are_encoded():
    links = search_links("Who is the PM of the US? (today) & why")
    assert links.startswith("\n\nYou can look this up yourself:")
    urls = [line.split("](", 1)[1].rstrip(")") for line in links.splitlines() if line.startswith("- [")]
    assert [urlparse(u).netloc for u in urls] == [
        "www.google.com",
        "www.bing.com",
        "duckduckgo.com",
        "en.wikipedia.org",
    ]
    assert parse_qs(urlparse(urls[0]).query)["q"] == ["Who is the PM of the US? (today) & why"]
    assert all(" " not in u and ")" not in u for u in urls)
    assert search_links("   ") == ""


async def test_refusal_adds_search_links_without_billing_them(engine, settings, clock, principal):  # noqa: F811
    question = "Who is the PM of US?"
    chat, _, conv = await make_engine(
        engine, settings, clock, [FakeTurn(f"{REFUSAL_SENTENCE} I can help with your campaigns.")], principal
    )
    events = await collect(chat, principal, conv, question)
    tokens = "".join(e["data"]["text"] for e in events if e["event"] == "token")
    assert tokens.startswith(REFUSAL_SENTENCE) and "https://www.google.com/search?q=Who+is+the+PM+of+US%3F" in tokens
    end = events[-1]["data"]
    assert end["tokens_charged"] == 0
    async with engine.connect() as conn:
        saved = (
            await conn.execute(select(chat_messages.c.content).where(chat_messages.c.role == "assistant"))
        ).scalar()
    assert saved.startswith(REFUSAL_SENTENCE) and "duckduckgo.com" in saved


async def test_answers_get_no_search_links(engine, settings, clock, principal):  # noqa: F811
    chat, _, conv = await make_engine(
        engine, settings, clock, [FakeTurn("CPM is cost per thousand impressions.")], principal
    )
    events = await collect(chat, principal, conv, "What is CPM?")
    tokens = "".join(e["data"]["text"] for e in events if e["event"] == "token")
    assert tokens == "CPM is cost per thousand impressions."


async def test_search_links_can_be_switched_off(engine, settings, clock, principal):  # noqa: F811
    s = settings.model_copy(update={"search_links_on_refusal": False})
    chat, _, conv = await make_engine(
        engine, s, clock, [FakeTurn(f"{REFUSAL_SENTENCE} Ask me about campaigns.")], principal
    )
    events = await collect(chat, principal, conv, "Who is the PM of US?")
    assert "google.com" not in "".join(e["data"]["text"] for e in events if e["event"] == "token")

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select

from chat_service.billing.tokenizer import DevRegexTokenizer
from chat_service.billing.wallet import WalletService
from chat_service.db.schema import chat_messages, chat_tool_calls, chat_usage_events
from chat_service.engine.agent import REFUSAL_SENTENCE, ChatEngine
from chat_service.engine.store import ConversationStore, OrgSettingsStore
from chat_service.llm.client import FallbackModelClient
from chat_service.llm.fake import FakeModelClient, FakeTurn
from chat_service.principal import Principal
from chat_service.tools import build_registry
from chat_service.tools.product import MockProductAPI


@pytest.fixture
def principal() -> Principal:
    return Principal(user_id=uuid.uuid4(), org_id=uuid.uuid4(), display_name="Priya", roles=("planner",))


async def make_engine(engine, settings, clock, turns, principal, mode="live", enable=True, fallback_turns=None):
    settings.billing_mode = mode
    model = FakeModelClient(turns)
    client = model
    if fallback_turns is not None:
        client = FallbackModelClient(model, FakeModelClient(fallback_turns, name="fallback"))
    orgs = OrgSettingsStore(engine)
    if enable:
        await orgs.upsert(principal.org_id, chat_enabled=True, timezone="Asia/Kolkata", currency="INR")
    chat = ChatEngine(
        settings,
        WalletService(engine, settings, clock),
        build_registry(settings, MockProductAPI()),
        client,
        DevRegexTokenizer(),
        ConversationStore(engine),
        orgs,
        clock,
    )
    conv = await chat.conversations.create(principal.org_id, principal.user_id)
    return chat, model, conv


async def collect(chat, principal, conv, text):
    return [ev async for ev in chat.handle_message(principal, conv, text)]


def names(events):
    return [e["event"] for e in events]


async def usage_rows(engine):
    async with engine.connect() as conn:
        return (await conn.execute(select(chat_usage_events))).all()


# ---------------------------------------------------------------- happy paths


async def test_plain_answer_is_charged_and_streamed(engine, settings, clock, principal):
    chat, model, conv = await make_engine(
        engine, settings, clock, [FakeTurn("Hello Priya, how can I help?")], principal
    )
    events = await collect(chat, principal, conv, "hi there")
    assert names(events)[0] == "message.start" and names(events)[-1] == "message.end"
    text = "".join(e["data"]["text"] for e in events if e["event"] == "token")
    assert text == "Hello Priya, how can I help?"
    end = events[-1]["data"]
    assert end["tokens_charged"] == end["billable_input_tokens"] + end["billable_output_tokens"] > 0
    assert end["balance_after"] == settings.free_trial_tokens - end["tokens_charged"]
    system, user = model.requests[0]["messages"]
    # cache-friendly layout: a static system prompt; the context leads the latest user message
    assert "{" not in system["content"] and "2026" not in system["content"] and "Priya" not in system["content"]
    assert user == {
        "role": "user",
        "content": "[Context] Today: 2026-11-22 (Asia/Kolkata) · Currency: INR · Role: planner\n\nhi there",
    }


async def test_tool_round_then_answer_records_tool_calls(engine, settings, clock, principal):
    turns = [
        FakeTurn(
            tool_calls=[
                (
                    "get_campaign_performance",
                    {
                        "client_id": "cl_acme",
                        "date_from": "2026-11-15",
                        "date_to": "2026-11-21",
                        "breakdown": "platform",
                    },
                )
            ]
        ),
        FakeTurn("Acme spent across Google and Meta last week."),
    ]
    chat, model, conv = await make_engine(engine, settings, clock, turns, principal)
    events = await collect(chat, principal, conv, "How did Acme do last week by platform?")
    assert names(events) == ["message.start", "tool.start", "tool.end"] + ["token"] * 8 + ["message.end"]
    assert events[1]["data"]["label"] == "Looking up campaign performance"
    tool_msg = model.requests[1]["messages"][-1]
    assert tool_msg["role"] == "tool" and '"totals"' in tool_msg["content"]
    async with engine.connect() as conn:
        calls = (await conn.execute(select(chat_tool_calls))).all()
    assert len(calls) == 1 and calls[0].ok and calls[0].result["currency"] == "INR"


async def test_invalid_tool_args_are_not_executed(engine, settings, clock, principal):
    turns = [
        FakeTurn(tool_calls=[("get_campaign_performance", {"client_id": "cl_acme"})]),
        FakeTurn("I need a date range."),
    ]
    chat, model, conv = await make_engine(engine, settings, clock, turns, principal)
    events = await collect(chat, principal, conv, "performance?")
    tool_end = next(e for e in events if e["event"] == "tool.end")
    assert tool_end["data"]["ok"] is False
    assert "invalid arguments" in model.requests[1]["messages"][-1]["content"]


async def test_max_steps_forces_final_answer_without_tools(engine, settings, clock, principal):
    settings.max_steps = 2
    turns = [FakeTurn(tool_calls=[("list_clients", {})]), FakeTurn("Here are your clients.")]
    chat, model, conv = await make_engine(engine, settings, clock, turns, principal)
    events = await collect(chat, principal, conv, "clients?")
    assert events[-1]["event"] == "message.end"
    assert model.requests[1]["tools"] is None  # last step: no tools offered


async def test_history_is_included_on_next_turn(engine, settings, clock, principal):
    chat, model, conv = await make_engine(
        engine, settings, clock, [FakeTurn("First answer."), FakeTurn("Second answer.")], principal
    )
    await collect(chat, principal, conv, "first question")
    await collect(chat, principal, conv, "second question")
    roles = [(m["role"], m["content"]) for m in model.requests[1]["messages"][1:]]
    # history is stored without the context, so the prefix up to the new message is unchanged
    assert roles[:2] == [("user", "first question"), ("assistant", "First answer.")]
    assert roles[2][0] == "user" and roles[2][1].startswith("[Context] ")
    assert roles[2][1].endswith("\n\nsecond question") and len(roles) == 3


# ---------------------------------------------------------------- not charged


async def test_model_error_mid_stream_is_not_charged(engine, settings, clock, principal):
    chat, _, conv = await make_engine(
        engine, settings, clock, [FakeTurn("partial text", fail_after_text=True)], principal
    )
    events = await collect(chat, principal, conv, "hello")
    assert events[-1] == {
        "event": "error",
        "data": {
            "code": "internal_error",
            "message": "Something went wrong. You were not charged for this message.",
            "charged": False,
        },
    }
    bal = await chat.wallet.get_balance(principal.org_id)
    assert bal.tokens_available == settings.free_trial_tokens and bal.tokens_reserved == 0
    (row,) = await usage_rows(engine)
    assert row.status == "not_charged" and row.reason == "model_error"


async def test_refusal_not_charged_by_default(engine, settings, clock, principal):
    chat, _, conv = await make_engine(
        engine, settings, clock, [FakeTurn(f"{REFUSAL_SENTENCE} I can help with campaign performance.")], principal
    )
    events = await collect(chat, principal, conv, "write me a poem about cats")
    assert events[-1]["data"]["tokens_charged"] == 0
    (row,) = await usage_rows(engine)
    assert row.status == "not_charged" and row.reason == "refusal"


async def test_fallback_host_used_when_primary_fails_before_first_token(engine, settings, clock, principal):
    chat, _, conv = await make_engine(
        engine,
        settings,
        clock,
        [FakeTurn(fail_before_first_token=True)],
        principal,
        fallback_turns=[FakeTurn("Answer from fallback.")],
    )
    events = await collect(chat, principal, conv, "hello")
    assert events[-1]["event"] == "message.end"
    (row,) = await usage_rows(engine)
    assert row.serving_host == "fallback" and row.status == "charged"


async def test_client_disconnect_charges_only_visible_text(engine, settings, clock, principal):
    chat, _, conv = await make_engine(
        engine, settings, clock, [FakeTurn("one two three four five six seven eight")], principal
    )
    gen = chat.handle_message(principal, conv, "hello")
    seen = []
    async for ev in gen:
        seen.append(ev)
        if sum(e["event"] == "token" for e in seen) == 3:
            break
    await gen.aclose()
    (row,) = await usage_rows(engine)
    assert row.status == "charged" and row.reason is None
    assert row.billable_output_tokens == 4  # "one two three" (dev counter: "three" = 2 pieces)
    bal = await chat.wallet.get_balance(principal.org_id)
    assert bal.tokens_reserved == 0


# ---------------------------------------------------------------- gates


async def test_zero_balance_blocks_without_calling_model(engine, settings, clock, principal):
    settings.free_trial_tokens = 100  # below min useful output
    chat, model, conv = await make_engine(engine, settings, clock, [FakeTurn("never")], principal)
    events = await collect(chat, principal, conv, "hello")
    assert events == [
        {
            "event": "error",
            "data": {"code": "insufficient_tokens", "message": "You've used all your tokens.", "charged": False},
        }
    ]
    assert model.requests == []


async def test_chat_disabled_for_org(engine, settings, clock, principal):
    chat, model, conv = await make_engine(engine, settings, clock, [FakeTurn("never")], principal, enable=False)
    events = await collect(chat, principal, conv, "hello")
    assert events[0]["data"]["code"] == "chat_disabled" and model.requests == []


async def test_global_kill_switch(engine, settings, clock, principal):
    settings.enabled_global = False
    chat, model, conv = await make_engine(engine, settings, clock, [FakeTurn("never")], principal)
    events = await collect(chat, principal, conv, "hello")
    assert events[0]["data"]["code"] == "chat_disabled"


async def test_free_trial_daily_ceiling(engine, settings, clock, principal):
    settings.free_trial_daily_max_tokens = 300
    settings.min_useful_output = 50
    chat, _, conv = await make_engine(engine, settings, clock, [FakeTurn("word " * 400), FakeTurn("never")], principal)
    await collect(chat, principal, conv, "first")
    events = await collect(chat, principal, conv, "second")
    assert events[0]["data"]["code"] == "daily_limit_reached"


async def test_message_too_long(engine, settings, clock, principal):
    chat, _, conv = await make_engine(engine, settings, clock, [], principal)
    events = await collect(chat, principal, conv, "x" * (settings.max_message_chars + 1))
    assert events[0]["data"]["code"] == "message_too_long"


async def test_other_users_conversation_not_accessible(engine, settings, clock, principal):
    chat, _, conv = await make_engine(engine, settings, clock, [FakeTurn("never")], principal)
    intruder = Principal(user_id=uuid.uuid4(), org_id=principal.org_id)
    events = await collect(chat, intruder, conv, "hello")
    assert events[0]["data"]["code"] == "not_found"


# ---------------------------------------------------------------- shadow mode & concurrency


async def test_shadow_mode_counts_but_never_debits(engine, settings, clock, principal):
    chat, _, conv = await make_engine(
        engine, settings, clock, [FakeTurn("Shadow answer here.")], principal, mode="shadow"
    )
    events = await collect(chat, principal, conv, "hello")
    end = events[-1]["data"]
    assert end["billing_mode"] == "shadow" and end["tokens_charged"] > 0
    assert end["balance_after"] == settings.free_trial_tokens
    (row,) = await usage_rows(engine)
    assert row.status == "shadow"


async def test_parallel_messages_never_overspend(engine, settings, clock, principal):
    settings.free_trial_tokens = 3000
    settings.max_output_tokens = 1200
    settings.max_concurrent_streams_per_org = 10
    settings.rate_limit_per_user_per_min = 100
    chat, _, _ = await make_engine(engine, settings, clock, [FakeTurn("ok")] * 5, principal)
    convs = [await chat.conversations.create(principal.org_id, principal.user_id) for _ in range(5)]
    results = await asyncio.gather(*(collect(chat, principal, c, "hello") for c in convs))
    ok = [r for r in results if r[-1]["event"] == "message.end"]
    # How many succeed depends on timing (a reservation shrinks to the headroom left, and finished messages
    # release what they didn't use); the guarantee is that the wallet never goes below zero.
    assert len(ok) >= 1
    assert sum(r[-1]["data"]["tokens_charged"] for r in ok) <= settings.free_trial_tokens
    bal = await chat.wallet.get_balance(principal.org_id)
    assert bal.tokens_available >= 0 and bal.tokens_reserved == 0
    assert await chat.wallet.check_invariant(principal.org_id)
    async with engine.connect() as conn:
        assistant_msgs = (await conn.execute(select(chat_messages).where(chat_messages.c.role == "assistant"))).all()
    assert len(assistant_msgs) == len(ok)


async def test_prefix_cache_hits_are_recorded_but_never_billed(engine, settings, clock, principal):
    from prometheus_client import REGISTRY

    def cached_metric():
        return REGISTRY.get_sample_value("aos_chat_host_cached_prompt_tokens_total", {"model": "fake-slm"}) or 0

    before = cached_metric()
    turns = [
        FakeTurn(tool_calls=[("list_clients", {})], cached_prompt_tokens=300),
        FakeTurn("Two clients.", cached_prompt_tokens=350),
    ]
    chat, model, conv = await make_engine(engine, settings, clock, turns, principal)
    end = (await collect(chat, principal, conv, "which clients?"))[-1]["data"]
    row = (await usage_rows(engine))[-1]
    assert row.host_cached_prompt_tokens == 650 and row.host_prompt_tokens > 650
    assert end["tokens_charged"] == end["billable_input_tokens"] + end["billable_output_tokens"]  # unchanged
    assert cached_metric() - before == 650

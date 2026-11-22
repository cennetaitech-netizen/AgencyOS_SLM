"""Engine integration for M2: chat actions (Phase 9), caps/overage/subscriptions (Phase 10)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from chat_service.actions.service import ActionService
from chat_service.billing.caps import CapsService, LogNotifier
from chat_service.billing.invoices import InvoiceService
from chat_service.billing.subscriptions import SubscriptionService
from chat_service.billing.tokenizer import DevRegexTokenizer
from chat_service.billing.wallet import UsageRecord, WalletService
from chat_service.db.schema import chat_usage_events
from chat_service.engine.agent import ChatEngine
from chat_service.engine.store import ConversationStore, OrgSettingsStore
from chat_service.llm.fake import FakeModelClient, FakeTurn
from chat_service.principal import Principal
from chat_service.tools import build_registry
from chat_service.tools.product import MockProductAPI


@pytest.fixture
def principal():
    return Principal(uuid.uuid4(), uuid.uuid4(), "Priya", ("planner",), "tok")


@pytest.fixture
async def stack(engine, settings, clock, principal):
    settings.write_actions_enabled_global = True
    product = MockProductAPI()
    orgs = OrgSettingsStore(engine)
    await orgs.upsert(principal.org_id, chat_enabled=True, timezone="Asia/Kolkata", currency="INR")
    wallet = WalletService(engine, settings, clock)
    invoices = InvoiceService(engine, settings, clock=clock)
    subs = SubscriptionService(engine, settings, wallet, invoices, clock)
    caps = CapsService(engine, LogNotifier(), clock=clock)
    actions = ActionService(engine, settings, product, orgs=orgs, clock=clock)

    def make(turns):
        model = FakeModelClient(turns)
        chat = ChatEngine(
            settings,
            wallet,
            build_registry(settings, product),
            model,
            DevRegexTokenizer(),
            ConversationStore(engine),
            orgs,
            clock,
            actions=actions,
            subscriptions=subs,
            caps=caps,
        )
        return chat, model

    return {"make": make, "product": product, "actions": actions, "subs": subs, "caps": caps, "wallet": wallet}


async def run(chat, principal, text):
    conv = await chat.conversations.create(principal.org_id, principal.user_id)
    return [ev async for ev in chat.handle_message(principal, conv, text)]


def tool_names(schemas):
    return {t["function"]["name"] for t in schemas or []}


# ---------------------------------------------------------------- Phase 9 in the engine


async def test_write_call_becomes_a_card_and_changes_nothing(stack, principal):
    await stack["actions"].set_rules(principal.org_id, write_actions_enabled=True)
    chat, model = stack["make"](
        [
            FakeTurn(tool_calls=[("get_campaign_details", {"campaign_id": "cmp_101"})]),
            FakeTurn(tool_calls=[("update_campaign_budget", {"campaign_id": "cmp_101", "new_budget": 330000})]),
            FakeTurn("I've prepared the change. Please review and confirm the card."),
        ]
    )
    events = await run(chat, principal, "Increase Acme Diwali Search budget by 10%")
    proposed = [e for e in events if e["event"] == "action.proposed"]
    assert len(proposed) == 1 and proposed[0]["data"]["after"]["budget"] == 330000
    assert stack["product"].campaigns[0]["budget"] == 300000  # nothing executed by the model
    assert "update_campaign_budget" in tool_names(model.requests[0]["tools"])
    assert "PROPOSE changes" in model.requests[0]["messages"][0]["content"]  # system_v2
    tool_reply = model.requests[2]["messages"][-1]["content"]
    assert '"awaiting_user_confirmation"' in tool_reply
    async with chat.wallet.engine.connect() as conn:
        ev = (await conn.execute(select(chat_usage_events))).one()
    assert ev.prompt_version == "system_v2" and ev.client_id == "cl_acme"

    view = await stack["actions"].confirm(principal, uuid.UUID(proposed[0]["data"]["action_id"]))
    assert view["status"] == "executed" and stack["product"].campaigns[0]["budget"] == 330000


async def test_write_tools_hidden_and_inert_when_actions_disabled(stack, principal):
    chat, model = stack["make"](
        [
            FakeTurn(tool_calls=[("pause_campaign", {"campaign_id": "cmp_101"})]),  # model tries anyway
            FakeTurn("I can't make changes; you can pause it in AgencyOS."),
        ]
    )
    events = await run(chat, principal, "pause cmp_101")
    assert not tool_names(model.requests[0]["tools"]) & {"pause_campaign", "update_campaign_budget"}
    assert "can't change anything" in model.requests[0]["messages"][0]["content"]  # system_v1
    assert not [e for e in events if e["event"] == "action.proposed"]
    assert stack["product"].campaigns[0]["status"] == "active"
    assert "confirmation flow" in model.requests[1]["messages"][-1]["content"]


async def test_rejected_proposal_is_explained_to_the_model(stack, principal):
    await stack["actions"].set_rules(principal.org_id, write_actions_enabled=True)
    chat, model = stack["make"](
        [
            FakeTurn(tool_calls=[("update_campaign_budget", {"campaign_id": "cmp_101", "new_budget": 600000})]),
            FakeTurn("That's above your limit; I can propose a 20% increase instead."),
        ]
    )
    events = await run(chat, principal, "double the budget")
    assert not [e for e in events if e["event"] == "action.proposed"]
    assert "limit of 20%" in model.requests[1]["messages"][-1]["content"]


# ---------------------------------------------------------------- Phase 10 in the engine


async def test_user_cap_blocks_and_alerts(stack, principal, settings):
    settings.min_useful_output = 50
    await stack["caps"].set_cap(principal.org_id, "user", principal.user_id, "day", 300)
    chat, _ = stack["make"]([FakeTurn("word " * 250), FakeTurn("never")])
    first = await run(chat, principal, "first question")
    end = first[-1]["data"]
    assert end["tokens_charged"] <= 300
    assert any(a["scope"].startswith("cap:user") for a in end["alerts"])
    second = await run(chat, principal, "second question")
    assert second[0]["data"]["code"] == "usage_cap_reached"


async def test_overage_allows_chat_after_allowance_and_bills_it(stack, principal, engine):
    subs = stack["subs"]
    await subs.upsert_plan(
        plan_id="tiny",
        name="Tiny",
        monthly_fee_minor=100,
        currency="INR",
        monthly_included_tokens=1_000,
        overage_allowed=True,
        price_per_1m_tokens_minor=100_000,
    )
    await subs.subscribe(principal.org_id, "tiny", spend_limit_minor=1_000)
    w = stack["wallet"]
    await w.ensure_free_trial(principal.org_id)
    r = await w.reserve(principal.org_id, principal.user_id, uuid.uuid4(), uuid.uuid4(), 201_000)
    await w.settle(r.reservation_id, UsageRecord(100_500, 100_500, "m", "system_v1", "dev-regex-v0"))
    # free trial (200,000) and plan allowance (1,000) are now fully used
    assert (await w.get_balance(principal.org_id)).spendable == 0

    chat, _ = stack["make"]([FakeTurn("Here is your answer from overage.")])
    events = await run(chat, principal, "hello")
    assert events[-1]["event"] == "message.end" and events[-1]["data"]["tokens_charged"] > 0
    async with engine.connect() as conn:
        last = (await conn.execute(select(chat_usage_events).order_by(chat_usage_events.c.id.desc()))).first()
    assert last.overage_tokens == last.tokens_charged


async def test_suspended_subscription_blocks_chat(stack, principal, clock):
    subs = stack["subs"]
    await subs.upsert_plan(
        plan_id="starter", name="Starter", monthly_fee_minor=100, currency="INR", monthly_included_tokens=1_000
    )
    await subs.subscribe(principal.org_id, "starter")
    await subs.payment_failed(principal.org_id)
    clock.advance(days=8)
    await subs.run_dunning()
    chat, model = stack["make"]([FakeTurn("never")])
    events = await run(chat, principal, "hello")
    assert events[0]["data"]["code"] == "billing_suspended" and model.requests == []

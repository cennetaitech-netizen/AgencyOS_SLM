"""Phase 7 (grounding, analytics, economics, quality review) and Phase 11 rollout routing."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from chat_service.analytics.economics import unit_economics
from chat_service.analytics.events import LogAnalyticsSink
from chat_service.analytics.quality import quality_review
from chat_service.api.app import create_app
from chat_service.billing.tokenizer import DevRegexTokenizer
from chat_service.billing.wallet import WalletService
from chat_service.db.schema import chat_messages, chat_usage_events
from chat_service.engine.agent import ChatEngine
from chat_service.engine.grounding import ungrounded_numbers
from chat_service.engine.store import ConversationStore, OrgSettingsStore
from chat_service.llm.fake import FakeModelClient, FakeTurn
from chat_service.llm.router import ModelRouter, RolloutError, RolloutStore, org_bucket
from chat_service.principal import Principal
from chat_service.tools import build_registry
from chat_service.tools.product import MockProductAPI

# ---------------------------------------------------------------- grounding


@pytest.mark.parametrize(
    ("answer", "sources", "missing"),
    [
        ("Spend was 1,23,456.50 INR and CPA 412.3", [{"spend": 123456.5, "cpa": 412.27}], []),
        ("ROAS was 4.8", [{"roas": 4.83}], []),  # rounded to the answer's precision
        ("Spend was 98,000", [{"spend": 123456.5}], ["98000"]),
        ("From 2026-11-01 to 2026-11-15 you ran 3 campaigns", [{}], []),  # dates and small counts ignored
        ("You asked about 450000 revenue", ["revenue 450000 spend 90000"], []),  # from the user's message
        ("CTR improved 25%", [{"result": 25.0}], []),
        (
            "Acme for 15–21 Nov 2026: spend 55,512.99",
            [{"date_range": {"from": "2026-11-15", "to": "2026-11-21"}, "spend": 55512.99}],
            [],
        ),
        ("Spend in Dec 2027 was 5,000", [{"date_range": {"from": "2026-11-15"}, "spend": 5000}], ["2027"]),
    ],
)
def test_grounding(answer, sources, missing):
    assert ungrounded_numbers(answer, sources) == missing


# ---------------------------------------------------------------- engine flags + analytics


async def test_engine_flags_ungrounded_numbers_and_tracks_events(engine, settings, clock):
    p = Principal(uuid.uuid4(), uuid.uuid4(), "Priya", ("planner",))
    orgs = OrgSettingsStore(engine)
    await orgs.upsert(p.org_id, chat_enabled=True, timezone="Asia/Kolkata", currency="INR")
    sink = LogAnalyticsSink()
    model = FakeModelClient(
        [
            FakeTurn(tool_calls=[("get_budget_pacing", {"campaign_id": "cmp_102"})]),
            FakeTurn("Budget is 250000 but you will spend 999999 by the end."),
        ]
    )
    chat = ChatEngine(
        settings,
        WalletService(engine, settings, clock),
        build_registry(settings, MockProductAPI()),
        model,
        DevRegexTokenizer(),
        ConversationStore(engine),
        orgs,
        clock,
        analytics=sink,
    )
    conv = await chat.conversations.create(p.org_id, p.user_id)
    events = [e async for e in chat.handle_message(p, conv, "pacing for cmp_102?")]
    assert events[-1]["data"]["quality_flags"] == {"ungrounded_numbers": ["999999"]}
    async with engine.connect() as conn:
        flags = (
            await conn.execute(select(chat_messages.c.flags).where(chat_messages.c.role == "assistant"))
        ).scalar_one()
    assert flags == {"ungrounded_numbers": ["999999"]}
    assert sink.names()[:2] == ["trial_granted", "chat_message_sent"]
    sent = next(e for e in sink.events if e[0] == "chat_message_sent")[3]
    assert sent["tools"] == ["get_budget_pacing"] and sent["latency_ms"] >= 0


# ---------------------------------------------------------------- economics & quality


async def _usage(engine, org, model, billable, prompt, completion, when, status="charged", reason=None):
    async with engine.begin() as conn:
        await conn.execute(
            chat_usage_events.insert().values(
                occurred_at=when,
                org_id=org,
                user_id=uuid.uuid4(),
                conversation_id=uuid.uuid4(),
                message_id=uuid.uuid4(),
                billable_input_tokens=billable // 2,
                billable_output_tokens=billable - billable // 2,
                tokens_charged=billable,
                billing_tokenizer_version="v",
                host_prompt_tokens=prompt,
                host_completion_tokens=completion,
                model_name=model,
                prompt_version="system_v1",
                status=status,
                reason=reason,
            )
        )


async def test_unit_economics_overhead_margin_and_alerts(engine, settings, clock):
    settings.host_price_input_per_1m_minor = 2_000  # 20.00 per 1M input tokens
    settings.host_price_output_per_1m_minor = 6_000
    settings.list_price_per_1m_tokens_minor = 100_000  # 1,000.00 per 1M billable tokens
    settings.max_overhead_ratio = 10
    org = uuid.uuid4()
    await _usage(engine, org, "m", 1_000, 11_000, 1_000, clock.now)
    await _usage(engine, org, "m", 1_000, 7_000, 1_000, clock.now - timedelta(days=1))
    result = await unit_economics(engine, settings, clock.now - timedelta(days=2), clock.now + timedelta(days=1))
    s = result["summary"]
    assert s["billable_tokens"] == 2_000 and s["overhead_ratio"] == 10.0
    assert s["host_cost_minor"] == int((18_000 * 2_000 + 2_000 * 6_000) / 1_000_000)
    assert s["est_revenue_minor"] == 200 and s["gross_margin_pct"] == 76.0
    assert len(result["daily"]) == 2 and result["top_orgs"][0]["org_id"] == str(org)
    assert result["alerts"] == []
    settings.min_gross_margin_pct = 80
    result = await unit_economics(engine, settings, clock.now - timedelta(days=2), clock.now + timedelta(days=1))
    assert "below the 80" in result["alerts"][0]


async def test_quality_review_lists_down_rated_and_flagged(engine):
    store = ConversationStore(engine)
    org, user = uuid.uuid4(), uuid.uuid4()
    conv = await store.create(org, user)
    await store.add_message(conv, "user", "How did Acme do?")
    bad = await store.add_message(conv, "assistant", "Great, ROAS 99.", flags={"ungrounded_numbers": ["99"]})
    await store.add_feedback(bad, user, "down", "wrong_numbers", "made up")
    now = datetime.now(UTC)
    review = await quality_review(engine, now - timedelta(days=1), now + timedelta(days=1), sample_size=5)
    assert review["ratings"]["down"] == 1
    item = review["down_rated"][0]
    assert item["question"] == "How did Acme do?" and item["reason"] == "wrong_numbers"
    assert review["flagged"][0]["flags"] == {"ungrounded_numbers": ["99"]}
    assert len(review["random_sample"]) == 1


# ---------------------------------------------------------------- rollout (Phase 11)


async def test_router_canary_bucket_pin_promote(engine, clock):
    prod, canary = FakeModelClient([], name="slm-v1"), FakeModelClient([], name="slm-v2")
    store = RolloutStore(engine, "slm-v1", clock=clock)
    router = ModelRouter(store, {"slm-v1": prod, "slm-v2": canary})
    org = uuid.uuid4()
    assert await router.select(org) is prod
    with pytest.raises(RolloutError):
        await store.start_canary("unknown", 10)
    await store.start_canary("slm-v2", 100)
    assert await router.select(org) is canary
    assert await router.select(org, pin="slm-v1") is prod  # pinned org stays on the proven model
    await store.set_percent(org_bucket(org))  # this org's bucket is just outside the canary
    assert await router.select(org) is prod
    st = await store.promote()
    assert st.production_model == "slm-v2" and st.status == "promoted"
    assert await router.select(org) is canary


async def test_automatic_rollback_when_canary_errors(engine, settings, clock):
    store = RolloutStore(engine, "slm-v1", {"slm-v1", "slm-v2"}, clock=clock)
    await store.start_canary("slm-v2", 10)
    settings.rollout_min_messages = 20
    clock.advance(minutes=1)
    for i in range(20):
        await _usage(engine, uuid.uuid4(), "slm-v1", 10, 100, 10, clock.now)
        await _usage(
            engine,
            uuid.uuid4(),
            "slm-v2",
            10,
            100,
            10,
            clock.now,
            status="not_charged" if i < 4 else "charged",
            reason="model_error" if i < 4 else None,
        )
    result = await store.evaluate_canary(settings)
    assert result["decision"] == "rolled_back" and "error rate" in result["reasons"][0]
    st = await store.state()
    assert st.status == "rolled_back" and st.canary_percent == 0


async def test_canary_needs_enough_data(engine, settings, clock):
    store = RolloutStore(engine, "slm-v1", {"slm-v1", "slm-v2"}, clock=clock)
    await store.start_canary("slm-v2", 5)
    assert (await store.evaluate_canary(settings))["decision"] == "insufficient_data"


# ---------------------------------------------------------------- ops API


async def test_ops_api_consent_economics_rollout(engine, settings):
    clients = {"slm-v1": FakeModelClient([], name="slm-v1"), "slm-v2": FakeModelClient([], name="slm-v2")}
    router = ModelRouter(RolloutStore(engine, "slm-v1"), clients)
    app = create_app(settings, db=engine, model=router, product_api=MockProductAPI())
    async with app.router.lifespan_context(app):
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
        org, user = uuid.uuid4(), uuid.uuid4()
        admin = {"X-Dev-User-Id": str(user), "X-Dev-Org-Id": str(org), "X-Dev-Roles": "org_admin"}
        staff = {"X-Dev-User-Id": str(uuid.uuid4()), "X-Dev-Org-Id": str(uuid.uuid4()), "X-Dev-Roles": "agencyos_staff"}
        assert (
            await c.put("/chat/settings/training-consent", json={"consent": True}, headers=admin)
        ).status_code == 200
        assert (await c.get("/chat/settings", headers=admin)).json()["training_consent"] is True
        assert (
            await c.get("/chat/admin/economics?date_from=2026-11-01&date_to=2026-11-22", headers=admin)
        ).status_code == 403
        eco = await c.get("/chat/admin/economics?date_from=2026-11-01&date_to=2026-11-22", headers=staff)
        assert eco.status_code == 200 and "summary" in eco.json()
        assert (
            await c.get("/chat/admin/quality-review?date_from=2026-11-01&date_to=2026-11-22", headers=staff)
        ).status_code == 200
        st = (await c.get("/chat/admin/model-rollout", headers=staff)).json()
        assert st["available_models"] == ["slm-v1", "slm-v2"] and st["status"] == "idle"
        r = await c.post("/chat/admin/model-rollout/canary", json={"model": "slm-v2", "percent": 10}, headers=staff)
        assert r.json()["status"] == "canary"
        assert (await c.post("/chat/admin/model-rollout/evaluate", headers=staff)).json()[
            "decision"
        ] == "insufficient_data"
        r = await c.post("/chat/admin/model-rollout/rollback", json={"reason": "manual test"}, headers=staff)
        assert r.json()["status"] == "rolled_back"
        r = await c.put(f"/chat/admin/orgs/{org}/settings", json={"model_pin": "slm-v1"}, headers=staff)
        assert r.json()["model_pin"] == "slm-v1"
        w = (await c.get(f"/chat/admin/orgs/{org}/wallet", headers=staff)).json()
        assert w["invariant_ok"] is True

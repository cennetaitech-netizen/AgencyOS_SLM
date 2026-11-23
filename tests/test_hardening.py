"""Phase 12 hardening: production guard, HTTP security, audit log."""

from __future__ import annotations

import uuid

import httpx
import pytest

from chat_service.api.app import create_app
from chat_service.config import Settings, production_problems
from chat_service.llm.fake import FakeModelClient
from chat_service.tools.product import MockProductAPI

STAFF = {"X-Dev-User-Id": str(uuid.uuid4()), "X-Dev-Org-Id": str(uuid.uuid4()), "X-Dev-Roles": "agencyos_staff"}


@pytest.fixture
async def make_client(engine, settings):
    ctxs = []

    async def make(**overrides):
        s = settings.model_copy(update=overrides)
        app = create_app(s, db=engine, model=FakeModelClient([]), product_api=MockProductAPI())
        ctx = app.router.lifespan_context(app)
        await ctx.__aenter__()
        ctxs.append(ctx)
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")

    yield make
    for ctx in ctxs:
        await ctx.__aexit__(None, None, None)


def test_production_refuses_unsafe_config():
    problems = production_problems(Settings(env="production"))
    joined = " ".join(problems)
    for needle in (
        "AUTH_PROVIDER",
        "PRODUCT_ADAPTER",
        "PAYMENT_PROVIDER",
        "TOKENIZER",
        "ENCRYPTION",
        "REDIS",
        "CORS",
        "DATABASE_URL",
    ):
        assert needle in joined
    with pytest.raises(RuntimeError, match="unsafe production configuration"):
        create_app(Settings(env="production"))


def test_fully_configured_production_starts_with_manual_billing():
    ready = dict(
        env="production",
        auth_provider="product",
        product_adapter="http",
        billing_tokenizer_path="/srv/tokenizer.json",
        data_encryption_keys=["k"],
        redis_url="redis://r:6379/0",
        cors_allowed_origins=["https://app.agencyos.example"],
        metrics_token="m-token",
        database_url="postgresql+asyncpg://u:p@db/chat",
    )
    # no online payment provider needed: invoices and bank transfer, confirmed by staff
    assert production_problems(Settings(**ready, payment_provider="manual")) == []
    assert production_problems(Settings(**ready, payment_provider="dev", payment_webhook_secret="s3cret-xyz")) == [
        "CHAT_PAYMENT_PROVIDER=dev: use 'manual' (invoice and bank transfer) or a real provider"
    ]


async def test_security_headers_and_request_id(make_client):
    c = await make_client()
    r = await c.get("/healthz", headers={"X-Request-ID": "trace-12345678"})
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-request-id"] == "trace-12345678"
    bad = await c.get("/healthz", headers={"X-Request-ID": "<script>"})
    assert bad.headers["x-request-id"] != "<script>" and len(bad.headers["x-request-id"]) == 32


async def test_oversized_request_rejected(make_client):
    c = await make_client(max_request_bytes=1_000)
    r = await c.post("/chat/billing/webhook", content=b"x" * 5_000)
    assert r.status_code == 413


async def test_cors_allow_list(make_client):
    c = await make_client(cors_allowed_origins=["https://app.agencyos.example"])
    ok = await c.options(
        "/chat/wallet", headers={"Origin": "https://app.agencyos.example", "Access-Control-Request-Method": "GET"}
    )
    assert ok.headers.get("access-control-allow-origin") == "https://app.agencyos.example"
    evil = await c.options(
        "/chat/wallet", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"}
    )
    assert "access-control-allow-origin" not in evil.headers


async def test_audit_log_records_admin_changes(make_client):
    c = await make_client()
    org = uuid.uuid4()
    r = await c.put(f"/chat/admin/orgs/{org}/settings", json={"chat_enabled": True}, headers=STAFF)
    assert r.status_code == 200
    admin = {"X-Dev-User-Id": str(uuid.uuid4()), "X-Dev-Org-Id": str(org), "X-Dev-Roles": "org_admin"}
    await c.put("/chat/settings/training-consent", json={"consent": True}, headers=admin)
    await c.put("/chat/action-rules", json={"max_budget_change_pct": 500}, headers=admin)  # 422: not audited
    entries = (await c.get(f"/chat/admin/audit?org_id={org}", headers=STAFF)).json()["entries"]
    assert [e["action"] for e in entries] == ["training_consent_changed", "org_settings_updated"]
    assert entries[1]["details"]["body"] == {"chat_enabled": True}
    assert entries[1]["request_id"]
    assert (await c.get("/chat/admin/audit", headers=admin)).status_code == 403


# ---------------------------------------------------------------- encryption at rest


def test_field_cipher_roundtrip_passthrough_and_rotation():
    from cryptography.fernet import Fernet

    from chat_service.crypto import FieldCipher

    k1, k2 = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    c1 = FieldCipher([k1])
    enc = c1.encrypt_json({"spend": 123.5, "currency": "INR"})
    assert enc["_enc"] == "fernet" and "INR" not in enc["v"]
    assert c1.decrypt_json(enc) == {"spend": 123.5, "currency": "INR"}
    assert c1.decrypt_json({"plain": True}) == {"plain": True}  # historical unencrypted rows still readable
    rotated = FieldCipher([k2, k1]).rotate_json(enc)
    assert FieldCipher([k2]).decrypt_json(rotated) == {"spend": 123.5, "currency": "INR"}  # old key retired
    with pytest.raises(RuntimeError):
        FieldCipher().decrypt_json(enc)


async def test_tool_results_encrypted_in_db_and_readable_by_dataset_builder(engine, settings, clock):
    from cryptography.fernet import Fernet
    from sqlalchemy import select

    from chat_service.billing.tokenizer import DevRegexTokenizer
    from chat_service.billing.wallet import WalletService
    from chat_service.crypto import FieldCipher, rotate_tool_results
    from chat_service.db.schema import chat_tool_calls
    from chat_service.engine.agent import ChatEngine, load_prompt
    from chat_service.engine.store import ConversationStore, OrgSettingsStore
    from chat_service.llm.fake import FakeTurn
    from chat_service.ml.dataset import export_records
    from chat_service.principal import Principal
    from chat_service.tools import build_registry

    k1, k2 = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    p = Principal(uuid.uuid4(), uuid.uuid4(), "Priya", ("planner",))
    orgs = OrgSettingsStore(engine)
    await orgs.upsert(p.org_id, chat_enabled=True, training_consent=True, timezone="Asia/Kolkata", currency="INR")
    store = ConversationStore(engine, FieldCipher([k1]))
    model = FakeModelClient(
        [FakeTurn(tool_calls=[("get_campaign_details", {"campaign_id": "cmp_101"})]), FakeTurn("cmp_101 is active.")]
    )
    chat = ChatEngine(
        settings,
        WalletService(engine, settings, clock),
        build_registry(settings, MockProductAPI()),
        model,
        DevRegexTokenizer(),
        store,
        orgs,
        clock,
    )
    conv = await store.create(p.org_id, p.user_id)
    [e async for e in chat.handle_message(p, conv, "status of cmp_101?")]

    async with engine.connect() as conn:
        raw = (await conn.execute(select(chat_tool_calls.c.result))).scalar_one()
    assert raw["_enc"] == "fernet" and "Acme" not in str(raw)

    template = load_prompt("system_ft_v1")
    records, _, _ = await export_records(engine, template, [], [], cipher=FieldCipher([k1]))
    assert '"name":"Acme Diwali Search"' in records[0]["messages"][3]["content"]

    assert await rotate_tool_results(engine, FieldCipher([k2, k1])) == 1
    records, _, _ = await export_records(engine, template, [], [], cipher=FieldCipher([k2]))
    assert '"campaign_id":"cmp_101"' in records[0]["messages"][3]["content"]


# ---------------------------------------------------------------- shared limits (Redis)


@pytest.fixture
async def redis_client():
    import redis.asyncio as aioredis

    client = aioredis.from_url("redis://localhost:6379/15")
    try:
        await client.ping()
    except Exception:
        pytest.skip("Redis not available")
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


async def test_rate_limit_is_shared_across_instances(redis_client):
    import asyncio

    from chat_service.engine.limits import RedisRateLimiter

    server_a, server_b = RedisRateLimiter(redis_client, 10), RedisRateLimiter(redis_client, 10)
    user = uuid.uuid4()
    results = await asyncio.gather(*[(server_a if i % 2 else server_b).allow(user) for i in range(25)])
    assert sum(results) == 10
    assert await server_a.allow(uuid.uuid4())  # other users unaffected


async def test_concurrency_slots_shared_released_and_leased(redis_client):
    import asyncio

    from chat_service.engine.limits import RedisConcurrencyLimiter

    a = RedisConcurrencyLimiter(redis_client, 3, lease_seconds=60)
    b = RedisConcurrencyLimiter(redis_client, 3, lease_seconds=60)
    org = uuid.uuid4()
    slots = await asyncio.gather(*[(a if i % 2 else b).try_acquire(org) for i in range(6)])
    held = [s for s in slots if s]
    assert len(held) == 3
    await a.release(org, held[0])
    assert await b.try_acquire(org) is not None
    short = RedisConcurrencyLimiter(redis_client, 1, lease_seconds=1)
    other = uuid.uuid4()
    assert await short.try_acquire(other) is not None  # never released (crashed instance)
    assert await short.try_acquire(other) is None
    await asyncio.sleep(1.1)
    assert await short.try_acquire(other) is not None  # lease expired


async def test_limiters_fail_open_when_redis_is_down():
    import redis.asyncio as aioredis

    from chat_service.engine.limits import RedisConcurrencyLimiter, RedisRateLimiter

    dead = aioredis.from_url("redis://localhost:6390/0", socket_connect_timeout=0.2, socket_timeout=0.2)
    assert await RedisRateLimiter(dead, 1).allow(uuid.uuid4()) is True
    slot = await RedisConcurrencyLimiter(dead, 1).try_acquire(uuid.uuid4())
    assert slot == "unlimited"
    await dead.aclose()


# ---------------------------------------------------------------- metrics & readiness


async def test_readyz_and_metrics(make_client, engine):
    from chat_service.llm.fake import FakeTurn

    c = await make_client(metrics_token="m-token")
    r = await c.get("/readyz")
    assert r.status_code == 200 and r.json()["checks"]["database"] == "ok"
    assert (await c.get("/metrics")).status_code == 401
    before = (await c.get("/metrics", headers={"Authorization": "Bearer m-token"})).text

    org, user = uuid.uuid4(), uuid.uuid4()
    await c.put(f"/chat/admin/orgs/{org}/settings", json={"chat_enabled": True}, headers=STAFF)
    h = {"X-Dev-User-Id": str(user), "X-Dev-Org-Id": str(org)}
    conv = (await c.post("/chat/conversations", headers=h)).json()["id"]
    app_services = c._transport.app.state.services  # noqa: SLF001 - test access to swap the scripted model
    app_services.chat.model = FakeModelClient([FakeTurn("Hello there.")])
    await c.post(f"/chat/conversations/{conv}/messages", json={"text": "hi"}, headers=h)
    await c.post(f"/chat/conversations/{conv}/messages", json={"text": "x" * 9000}, headers=h)

    after = (await c.get("/metrics", headers={"Authorization": "Bearer m-token"})).text

    def value(text, prefix):
        return sum(float(line.rsplit(" ", 1)[1]) for line in text.splitlines() if line.startswith(prefix))

    assert (
        value(after, 'aos_chat_messages_total{billing_mode="live",status="ok"}')
        - value(before, 'aos_chat_messages_total{billing_mode="live",status="ok"}')
        == 1
    )
    assert (
        value(after, 'aos_chat_errors_total{code="message_too_long"}')
        - value(before, 'aos_chat_errors_total{code="message_too_long"}')
        == 1
    )
    assert 'route="/chat/conversations/{conversation_id}/messages"' in after  # route templates, not raw IDs
    assert str(conv) not in after

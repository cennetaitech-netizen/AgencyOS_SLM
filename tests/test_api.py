from __future__ import annotations

import json
import uuid

import httpx
import pytest

from chat_service.api.app import create_app
from chat_service.billing.payments import DevPaymentProvider
from chat_service.db.schema import chat_token_packs
from chat_service.llm.fake import FakeModelClient, FakeTurn
from chat_service.tools.product import MockProductAPI


@pytest.fixture
def ids():
    return {"org": uuid.uuid4(), "user": uuid.uuid4(), "staff": uuid.uuid4()}


def headers(user, org, roles="planner"):
    return {"X-Dev-User-Id": str(user), "X-Dev-Org-Id": str(org), "X-Dev-Roles": roles, "X-Dev-Name": "Priya"}


@pytest.fixture
async def client_factory(engine, settings):
    settings.auth_provider = "dev"
    payments = DevPaymentProvider(settings.payment_webhook_secret)

    async def make(turns):
        model = FakeModelClient(turns)
        app = create_app(settings, db=engine, model=model, product_api=MockProductAPI(), payments=payments)
        ctx = app.router.lifespan_context(app)
        await ctx.__aenter__()
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        return client, model, payments, ctx

    return make


def parse_sse(body: str) -> list[tuple[str, dict]]:
    out = []
    for block in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.split("\n"))
        out.append((lines["event"], json.loads(lines["data"])))
    return out


async def enable_org(client, ids, mode="live"):
    r = await client.put(
        f"/chat/admin/orgs/{ids['org']}/settings",
        json={"chat_enabled": True, "billing_mode": mode, "timezone": "Asia/Kolkata", "currency": "INR"},
        headers=headers(ids["staff"], uuid.uuid4(), "agencyos_staff"),
    )
    assert r.status_code == 200, r.text


async def test_full_chat_flow(client_factory, ids):
    client, _, _, ctx = await client_factory([FakeTurn("Hello! Your campaigns are on track.")])
    h = headers(ids["user"], ids["org"])
    await enable_org(client, ids)
    conv = (await client.post("/chat/conversations", headers=h)).json()["id"]

    r = await client.post(f"/chat/conversations/{conv}/messages", json={"text": "status?"}, headers=h)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["x-accel-buffering"] == "no"
    events = parse_sse(r.text)
    assert events[0][0] == "message.start" and events[-1][0] == "message.end"
    charged = events[-1][1]["tokens_charged"]

    wallet = (await client.get("/chat/wallet", headers=h)).json()
    assert wallet["spendable"] == 200_000 - charged and wallet["billing_mode"] == "live"

    msgs = (await client.get(f"/chat/conversations/{conv}/messages", headers=h)).json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assistant_id = msgs[1]["id"]
    r = await client.post(f"/chat/messages/{assistant_id}/feedback", json={"rating": "up"}, headers=h)
    assert r.status_code == 204

    convs = (await client.get("/chat/conversations", headers=h)).json()["conversations"]
    assert convs[0]["title"] == "status?"
    assert (await client.delete(f"/chat/conversations/{conv}", headers=h)).status_code == 204
    assert (await client.get(f"/chat/conversations/{conv}/messages", headers=h)).status_code == 404
    await ctx.__aexit__(None, None, None)


async def test_auth_and_roles(client_factory, ids):
    client, _, _, ctx = await client_factory([])
    assert (await client.get("/chat/wallet")).status_code == 401
    h = headers(ids["user"], ids["org"])
    assert (await client.get("/chat/usage?date_from=2026-11-01&date_to=2026-11-22", headers=h)).status_code == 403
    r = await client.put(f"/chat/admin/orgs/{ids['org']}/settings", json={"chat_enabled": True}, headers=h)
    assert r.status_code == 403
    admin = headers(ids["user"], ids["org"], "org_admin")
    assert (await client.get("/chat/usage?date_from=2026-11-01&date_to=2026-11-22", headers=admin)).status_code == 200
    await ctx.__aexit__(None, None, None)


async def test_feedback_on_someone_elses_message_is_404(client_factory, ids):
    client, _, _, ctx = await client_factory([FakeTurn("Hi.")])
    h = headers(ids["user"], ids["org"])
    await enable_org(client, ids)
    conv = (await client.post("/chat/conversations", headers=h)).json()["id"]
    await client.post(f"/chat/conversations/{conv}/messages", json={"text": "hi"}, headers=h)
    msg_id = (await client.get(f"/chat/conversations/{conv}/messages", headers=h)).json()["messages"][1]["id"]
    other = headers(uuid.uuid4(), ids["org"])
    r = await client.post(f"/chat/messages/{msg_id}/feedback", json={"rating": "down"}, headers=other)
    assert r.status_code == 404
    await ctx.__aexit__(None, None, None)


async def test_buy_pack_via_signed_webhook_and_refund(client_factory, ids, engine):
    client, _, payments, ctx = await client_factory([])
    async with engine.begin() as conn:
        await conn.execute(
            chat_token_packs.insert().values(
                pack_id="p1m", tokens=1_000_000, price_minor=99900, currency="INR", validity_days=365
            )
        )
    member = headers(ids["user"], ids["org"])
    admin = headers(ids["user"], ids["org"], "org_admin")
    assert (await client.post("/chat/billing/checkout", json={"pack_id": "p1m"}, headers=member)).status_code == 403
    co = (await client.post("/chat/billing/checkout", json={"pack_id": "p1m"}, headers=admin)).json()
    pid = co["provider_payment_id"]

    body = json.dumps({"type": "paid", "provider_payment_id": pid}).encode()
    bad = await client.post("/chat/billing/webhook", content=body, headers={"X-Signature": "nope"})
    assert bad.status_code == 400
    for _ in range(2):  # duplicate delivery must not double-credit
        r = await client.post("/chat/billing/webhook", content=body, headers={"X-Signature": payments.sign(body)})
        assert r.status_code == 204
    wallet = (await client.get("/chat/wallet", headers=member)).json()
    assert wallet["tokens_available"] == 1_000_000

    refund = json.dumps({"type": "refunded", "provider_payment_id": pid}).encode()
    r = await client.post("/chat/billing/webhook", content=refund, headers={"X-Signature": payments.sign(refund)})
    assert r.status_code == 204
    assert (await client.get("/chat/wallet", headers=member)).json()["tokens_available"] == 0
    await ctx.__aexit__(None, None, None)

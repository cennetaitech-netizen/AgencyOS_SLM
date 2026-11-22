"""Security test suite (Phase 12): authentication on every route, staff-only routes, and tenant isolation.

Org A owns data; a user and an org admin from org B try to read or change it through every endpoint that
takes an ID. Every attempt must fail without revealing whether the item exists (404, not 403, for IDs).
"""

from __future__ import annotations

import json
import re
import uuid

import httpx
import pytest
from fastapi.routing import APIRoute

from chat_service.api.app import create_app
from chat_service.db.schema import chat_token_packs
from chat_service.llm.fake import FakeModelClient, FakeTurn
from chat_service.tools.product import MockProductAPI

PUBLIC = {("GET", "/healthz"), ("GET", "/readyz"), ("GET", "/metrics"), ("POST", "/chat/billing/webhook")}


def who(org, roles="planner", user=None):
    return {"X-Dev-User-Id": str(user or uuid.uuid4()), "X-Dev-Org-Id": str(org), "X-Dev-Roles": roles}


@pytest.fixture
async def env(engine, settings):
    settings.write_actions_enabled_global = True
    model = FakeModelClient(
        [
            FakeTurn(tool_calls=[("pause_campaign", {"campaign_id": "cmp_101"})]),
            FakeTurn("Please confirm."),
        ]
    )
    app = create_app(settings, db=engine, model=model, product_api=MockProductAPI())
    async with app.router.lifespan_context(app):
        yield app, httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def _routes(app):
    for r in app.routes:
        if isinstance(r, APIRoute):
            for m in r.methods - {"HEAD"}:
                yield m, r.path


def _fill(path):
    return re.sub(r"\{[^}]+\}", str(uuid.uuid4()), path)


async def test_every_route_requires_authentication(env):
    app, c = env
    checked = 0
    for method, path in _routes(app):
        if (method, path) in PUBLIC:
            continue
        r = await c.request(method, _fill(path), json={})
        assert r.status_code == 401, f"{method} {path} returned {r.status_code} without credentials"
        checked += 1
    assert checked >= 40


async def test_staff_routes_reject_org_admins(env):
    app, c = env
    org_admin = who(uuid.uuid4(), "org_admin,planner")
    checked = 0
    for method, path in _routes(app):
        if not path.startswith("/chat/admin"):
            continue
        r = await c.request(method, _fill(path), json={}, headers=org_admin)
        assert r.status_code == 403, f"{method} {path} returned {r.status_code} for an org admin"
        checked += 1
    assert checked >= 15


async def test_org_admin_routes_reject_members(env):
    _, c = env
    member = who(uuid.uuid4())
    for method, path in [
        ("GET", "/chat/usage?date_from=2026-11-01&date_to=2026-11-02"),
        ("PUT", "/chat/action-rules"),
        ("GET", "/chat/billing/subscription"),
        ("POST", "/chat/billing/subscription"),
        ("PUT", "/chat/billing/profile"),
        ("GET", "/chat/billing/invoices"),
        ("GET", "/chat/caps"),
        ("PUT", "/chat/caps"),
        ("POST", "/chat/billing/checkout"),
        ("GET", "/chat/settings"),
        ("PUT", "/chat/settings/training-consent"),
    ]:
        r = await c.request(method, path, json={}, headers=member)
        assert r.status_code == 403, f"{method} {path} returned {r.status_code} for a member"


async def test_tenant_isolation_across_every_id_endpoint(env, engine):
    app, c = env
    staff = who(uuid.uuid4(), "agencyos_staff")
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    user_a = uuid.uuid4()
    a, a_admin = who(org_a, user=user_a), who(org_a, "org_admin,planner")
    b, b_admin = who(org_b), who(org_b, "org_admin,planner")
    for org in (org_a, org_b):
        await c.put(f"/chat/admin/orgs/{org}/settings", json={"chat_enabled": True}, headers=staff)
    await c.put("/chat/action-rules", json={"write_actions_enabled": True}, headers=a_admin)

    # Org A creates data of every kind.
    conv = (await c.post("/chat/conversations", headers=a)).json()["id"]
    stream = (await c.post(f"/chat/conversations/{conv}/messages", json={"text": "pause cmp_101"}, headers=a)).text
    action_id = next(
        json.loads(line[6:])["action_id"]
        for line in stream.splitlines()
        if line.startswith("data: ") and "action_id" in line
    )
    msg_id = (await c.get(f"/chat/conversations/{conv}/messages", headers=a)).json()["messages"][1]["id"]
    cap_id = (
        await c.put(
            "/chat/caps",
            json={"scope_type": "user", "scope_id": str(user_a), "period": "day", "max_tokens": 1000},
            headers=a_admin,
        )
    ).json()["cap_id"]
    await c.put(
        "/chat/billing/profile",
        json={"legal_name": "Acme Foods", "address": {}, "country": "IN", "billing_email": "billing@acme.example"},
        headers=a_admin,
    )
    await c.put(
        "/chat/admin/plans/p",
        json={"name": "P", "monthly_fee_minor": 100, "currency": "INR", "monthly_included_tokens": 10},
        headers=staff,
    )
    invoice_id = (await c.post("/chat/billing/subscription", json={"plan_id": "p"}, headers=a_admin)).json()[
        "invoice_id"
    ]
    await c.post(f"/chat/admin/invoices/{invoice_id}/approve", headers=staff)
    async with engine.begin() as conn:
        await conn.execute(
            chat_token_packs.insert().values(pack_id="pk", tokens=10, price_minor=1, currency="INR", validity_days=1)
        )

    # Org B (a member and an admin) tries to reach all of it.
    attempts = [
        ("GET", f"/chat/conversations/{conv}/messages", None),
        ("PATCH", f"/chat/conversations/{conv}", {"title": "pwned"}),
        ("DELETE", f"/chat/conversations/{conv}", None),
        ("POST", f"/chat/messages/{msg_id}/feedback", {"rating": "down"}),
        ("GET", f"/chat/actions/{action_id}", None),
        ("POST", f"/chat/actions/{action_id}/confirm", None),
        ("POST", f"/chat/actions/{action_id}/cancel", None),
        ("POST", f"/chat/actions/{action_id}/approve", None),
        ("POST", f"/chat/actions/{action_id}/reject", None),
        ("POST", f"/chat/actions/{action_id}/undo", None),
        ("GET", f"/chat/billing/invoices/{invoice_id}", None),
        ("GET", f"/chat/billing/invoices/{invoice_id}/usage.csv", None),
        ("DELETE", f"/chat/caps/{cap_id}", None),
    ]
    for headers in (b, b_admin):
        for method, path, body in attempts:
            r = await c.request(method, path, json=body, headers=headers)
            assert r.status_code in (403, 404), f"{method} {path} → {r.status_code} for another org"
            assert "Acme" not in r.text

    # Sending into A's conversation fails without running the model or touching A's data.
    r = await c.post(f"/chat/conversations/{conv}/messages", json={"text": "hi"}, headers=b)
    assert '"code": "not_found"' in r.text

    # Lists only ever show the caller's own org.
    assert (await c.get("/chat/conversations", headers=b)).json()["conversations"] == []
    assert (await c.get("/chat/actions", headers=b_admin)).json()["actions"] == []
    assert (await c.get("/chat/billing/invoices", headers=b_admin)).json()["invoices"] == []
    assert (await c.get("/chat/caps", headers=b_admin)).json()["caps"] == []
    assert (await c.get("/chat/billing/profile", headers=b_admin)).json()["profile"] is None
    usage = (await c.get("/chat/usage?date_from=2026-01-01&date_to=2026-03-31", headers=b_admin)).json()["rows"]
    assert usage == []

    # A's data is intact, and A can still act on it.
    assert (await c.get(f"/chat/actions/{action_id}", headers=a)).json()["status"] == "proposed"
    assert len((await c.get(f"/chat/conversations/{conv}/messages", headers=a)).json()["messages"]) == 2
    assert (await c.get("/chat/caps", headers=a_admin)).json()["caps"][0]["cap_id"] == cap_id

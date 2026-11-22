from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import update

from chat_service.api.app import create_app
from chat_service.billing.payments import DevPaymentProvider
from chat_service.db.schema import chat_subscriptions
from chat_service.llm.fake import FakeModelClient, FakeTurn
from chat_service.tools.product import MockProductAPI


def hdr(user, org, roles="planner"):
    return {"X-Dev-User-Id": str(user), "X-Dev-Org-Id": str(org), "X-Dev-Roles": roles}


@pytest.fixture
def ids():
    return {"org": uuid.uuid4(), "planner": uuid.uuid4(), "admin": uuid.uuid4(), "staff": uuid.uuid4()}


@pytest.fixture
async def api(engine, settings):
    settings.write_actions_enabled_global = True
    product = MockProductAPI()
    payments = DevPaymentProvider(settings.payment_webhook_secret)
    state = {}

    async def make(turns=()):
        app = create_app(
            settings, db=engine, model=FakeModelClient(list(turns)), product_api=product, payments=payments
        )
        ctx = app.router.lifespan_context(app)
        await ctx.__aenter__()
        state["ctx"] = ctx
        state["app"] = app
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")

    yield make, product, payments, state
    if "ctx" in state:
        await state["ctx"].__aexit__(None, None, None)


def sse(text):
    out = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.split("\n"))
        out.append((lines["event"], json.loads(lines["data"])))
    return out


async def enable(client, ids):
    r = await client.put(
        f"/chat/admin/orgs/{ids['org']}/settings",
        json={"chat_enabled": True, "billing_mode": "live"},
        headers=hdr(ids["staff"], uuid.uuid4(), "agencyos_staff"),
    )
    assert r.status_code == 200


# ---------------------------------------------------------------- Phase 9 over HTTP


async def test_action_flow_over_http(api, ids):
    make, product, _, _ = api
    client = await make(
        [
            FakeTurn(tool_calls=[("update_campaign_budget", {"campaign_id": "cmp_101", "new_budget": 315000})]),
            FakeTurn("Please confirm the card to apply the change."),
        ]
    )
    planner, admin = hdr(ids["planner"], ids["org"]), hdr(ids["admin"], ids["org"], "org_admin")
    await enable(client, ids)
    assert (
        await client.put("/chat/action-rules", json={"write_actions_enabled": True}, headers=planner)
    ).status_code == 403
    r = await client.put(
        "/chat/action-rules", json={"write_actions_enabled": True, "max_budget_change_pct": 10}, headers=admin
    )
    assert r.status_code == 200 and r.json()["write_actions_enabled"] is True
    catalog = (await client.get("/chat/action-catalog", headers=admin)).json()["actions"]
    assert {a["name"] for a in catalog} == {
        "update_campaign_budget",
        "pause_campaign",
        "resume_campaign",
        "rename_campaign",
    }
    assert (await client.get("/chat/action-catalog", headers=planner)).status_code == 403

    conv = (await client.post("/chat/conversations", headers=planner)).json()["id"]
    events = sse(
        (
            await client.post(f"/chat/conversations/{conv}/messages", json={"text": "raise budget 5%"}, headers=planner)
        ).text
    )
    card = next(d for e, d in events if e == "action.proposed")
    assert card["before"]["budget"] == 300000 and card["after"]["budget"] == 315000
    assert product.campaigns[0]["budget"] == 300000
    aid = card["action_id"]

    stranger = hdr(uuid.uuid4(), ids["org"])
    assert (await client.post(f"/chat/actions/{aid}/confirm", headers=stranger)).status_code == 403
    assert (await client.get(f"/chat/actions/{aid}", headers=stranger)).status_code == 404

    done = (await client.post(f"/chat/actions/{aid}/confirm", headers=planner)).json()
    assert done["status"] == "executed" and done["undo_available"] and product.campaigns[0]["budget"] == 315000
    assert (await client.post(f"/chat/actions/{aid}/confirm", headers=planner)).json()["status"] == "executed"

    undo = (await client.post(f"/chat/actions/{aid}/undo", headers=planner)).json()["proposal"]
    assert undo["after"]["budget"] == 300000
    await client.post(f"/chat/actions/{undo['action_id']}/confirm", headers=planner)
    assert product.campaigns[0]["budget"] == 300000
    mine = (await client.get("/chat/actions", headers=planner)).json()["actions"]
    assert {a["status"] for a in mine} == {"undone", "executed"}
    assert (await client.get("/chat/actions", headers=stranger)).json()["actions"] == []


async def test_action_errors_map_to_http(api, ids):
    make, product, _, _ = api
    client = await make()
    admin = hdr(ids["admin"], ids["org"], "org_admin")
    assert (await client.post(f"/chat/actions/{uuid.uuid4()}/confirm", headers=admin)).status_code == 404
    r = await client.put("/chat/action-rules", json={"blocked_tools": ["drop_tables"]}, headers=admin)
    assert r.status_code == 422


# ---------------------------------------------------------------- Phase 10 over HTTP


async def test_subscription_invoice_and_dunning_flow(api, ids, engine):
    make, _, payments, state = api
    client = await make()
    staff = hdr(ids["staff"], uuid.uuid4(), "agencyos_staff")
    admin = hdr(ids["admin"], ids["org"], "org_admin")
    member = hdr(ids["planner"], ids["org"])

    r = await client.put(
        "/chat/admin/plans/pro",
        headers=staff,
        json={
            "name": "Pro",
            "monthly_fee_minor": 1_999_900,
            "currency": "INR",
            "monthly_included_tokens": 10_000_000,
            "overage_allowed": True,
            "price_per_1m_tokens_minor": 100_000,
        },
    )
    assert r.status_code == 200
    assert (
        await client.put(
            "/chat/admin/plans/bad",
            headers=staff,
            json={
                "name": "Bad",
                "monthly_fee_minor": 1,
                "currency": "INR",
                "monthly_included_tokens": 1,
                "overage_allowed": True,
            },
        )
    ).status_code == 422
    assert [p["plan_id"] for p in (await client.get("/chat/plans", headers=member)).json()["plans"]] == ["pro"]

    assert (
        await client.put(
            "/chat/billing/profile",
            headers=admin,
            json={
                "legal_name": "Acme Foods Pvt Ltd",
                "address": {"city": "Mumbai"},
                "tax_id": "27ABCDE1234F1Z5",
                "country": "in",
                "billing_email": "billing@example.com",
            },
        )
    ).status_code == 200
    assert (await client.post("/chat/billing/subscription", headers=member, json={"plan_id": "pro"})).status_code == 403
    r = await client.post(
        "/chat/billing/subscription", headers=admin, json={"plan_id": "pro", "spend_limit_minor": 500_000}
    )
    assert r.status_code == 201
    invoice_id = r.json()["invoice_id"]
    sub = (await client.get("/chat/billing/subscription", headers=admin)).json()
    assert sub["subscription"]["plan_id"] == "pro" and sub["capacity_tokens"] == 10_000_000 + 5_000_000

    assert (await client.get("/chat/billing/invoices", headers=admin)).json()["invoices"] == []  # draft
    assert (await client.get(f"/chat/billing/invoices/{invoice_id}", headers=admin)).status_code == 404
    drafts = (await client.get(f"/chat/admin/orgs/{ids['org']}/invoices", headers=staff)).json()["invoices"]
    assert drafts[0]["status"] == "draft"
    sent = (await client.post(f"/chat/admin/invoices/{invoice_id}/approve", headers=staff)).json()
    assert sent["status"] == "sent" and sent["invoice_number"]
    inv = (await client.get(f"/chat/billing/invoices/{invoice_id}", headers=admin)).json()
    assert inv["total_minor"] == 1_999_900  # no tax rate configured for IN in this test
    csv_resp = await client.get(f"/chat/billing/invoices/{invoice_id}/usage.csv", headers=admin)
    assert csv_resp.status_code == 200 and csv_resp.text.startswith("occurred_at,")

    provider_invoice_id = state["app"].state.services.invoices.sink.sent[0].invoice_id
    pid = f"dev_inv_{provider_invoice_id.hex[:12]}"
    for kind, expected in (("invoice_payment_failed", "past_due"), ("invoice_paid", "active")):
        body = json.dumps({"type": kind, "provider_invoice_id": pid}).encode()
        r = await client.post("/chat/billing/webhook", content=body, headers={"X-Signature": payments.sign(body)})
        assert r.status_code == 204
        assert (await client.get("/chat/billing/subscription", headers=admin)).json()["subscription"][
            "status"
        ] == expected

    r = await client.put("/chat/billing/subscription", headers=admin, json={"remove_spend_limit": True})
    assert r.json()["subscription"]["spend_limit_minor"] is None

    async with engine.begin() as conn:  # pretend the period ended
        await conn.execute(
            update(chat_subscriptions)
            .where(chat_subscriptions.c.org_id == ids["org"])
            .values(current_period_end=datetime.now(UTC) - timedelta(minutes=1))
        )
    created = (await client.post("/chat/admin/billing/close-cycles", headers=staff)).json()["invoices_created"]
    assert len(created) == 1


async def test_caps_crud(api, ids):
    make, *_ = api
    client = await make()
    admin = hdr(ids["admin"], ids["org"], "org_admin")
    r = await client.put(
        "/chat/caps",
        headers=admin,
        json={"scope_type": "user", "scope_id": str(ids["planner"]), "period": "day", "max_tokens": 50_000},
    )
    assert r.status_code == 201
    caps = (await client.get("/chat/caps", headers=admin)).json()["caps"]
    assert caps[0]["max_tokens"] == 50_000
    bad = await client.put(
        "/chat/caps",
        headers=admin,
        json={"scope_type": "org", "scope_id": str(uuid.uuid4()), "period": "month", "max_tokens": 5},
    )
    assert bad.status_code == 422
    assert (await client.delete(f"/chat/caps/{caps[0]['cap_id']}", headers=admin)).status_code == 204
    assert (await client.get("/chat/caps", headers=hdr(ids["planner"], ids["org"]))).status_code == 403

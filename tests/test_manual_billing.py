"""Manual billing (CHAT_PAYMENT_PROVIDER=manual): launch without an online payment provider. Pack orders show payment
instructions, AgencyOS staff confirm them when the money arrives, and staff mark invoices paid or overdue."""

from __future__ import annotations

import uuid

import httpx
import pytest

from chat_service.api.app import create_app
from chat_service.billing.payments import ManualPaymentProvider, WebhookSignatureError, build_payment_provider
from chat_service.config import Settings, production_problems
from chat_service.db.schema import chat_token_packs
from chat_service.llm.fake import FakeModelClient
from chat_service.tools.product import MockProductAPI


def hdr(user, org, roles="planner"):
    return {"X-Dev-User-Id": str(user), "X-Dev-Org-Id": str(org), "X-Dev-Roles": roles}


@pytest.fixture
async def manual_api(engine, settings):
    settings.payment_provider = "manual"
    settings.auth_provider = "dev"
    app = create_app(settings, db=engine, model=FakeModelClient([]), product_api=MockProductAPI())
    ctx = app.router.lifespan_context(app)
    await ctx.__aenter__()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    ids = {"org": uuid.uuid4(), "admin": uuid.uuid4(), "member": uuid.uuid4(), "staff": uuid.uuid4()}
    staff = hdr(ids["staff"], uuid.uuid4(), "agencyos_staff")
    r = await client.put(f"/chat/admin/orgs/{ids['org']}/settings", headers=staff, json={"chat_enabled": True})
    assert r.status_code == 200
    yield client, app, ids, staff
    await ctx.__aexit__(None, None, None)


def test_production_accepts_manual_billing_without_webhook_secret():
    base = dict(env="production", payment_webhook_secret="dev-webhook-secret")
    dev = production_problems(Settings(**base, payment_provider="dev"))
    manual = production_problems(Settings(**base, payment_provider="manual"))
    assert any("CHAT_PAYMENT_PROVIDER=dev" in p for p in dev)
    assert any("WEBHOOK_SECRET" in p for p in dev)
    assert not any("PAYMENT" in p for p in manual)
    assert isinstance(build_payment_provider(Settings(payment_provider="manual")), ManualPaymentProvider)
    with pytest.raises(WebhookSignatureError):
        ManualPaymentProvider("x").parse_webhook({}, b"{}")


async def test_manual_pack_order_is_credited_once_when_staff_confirm(manual_api, engine):
    client, app, ids, staff = manual_api
    admin = hdr(ids["admin"], ids["org"], "org_admin")
    async with engine.begin() as conn:
        await conn.execute(
            chat_token_packs.insert().values(
                pack_id="m1m", tokens=1_000_000, price_minor=99_900, currency="INR", validity_days=365
            )
        )
    before = (await client.get("/chat/wallet", headers=admin)).json()["spendable"]

    order = (await client.post("/chat/billing/checkout", json={"pack_id": "m1m"}, headers=admin)).json()
    assert order["manual"] is True and order["checkout_url"] is None
    assert "bank transfer" in order["instructions"] and order["amount_minor"] == 99_900
    ref = order["provider_payment_id"]
    assert ref.startswith("AOS-")

    orders = (await client.get("/chat/billing/orders", headers=admin)).json()["orders"]
    assert [(o["reference"], o["status"]) for o in orders] == [(ref, "pending")]
    assert (await client.get("/chat/wallet", headers=admin)).json()["spendable"] == before  # not paid yet

    # only staff confirm; org admins can't pay themselves
    assert (await client.post(f"/chat/admin/payments/{ref}/confirm", headers=admin)).status_code == 403
    pending = (await client.get("/chat/admin/payments", headers=staff)).json()["payments"]
    assert ref in [p["reference"] for p in pending]

    for _ in range(2):  # confirming twice credits once
        r = await client.post(f"/chat/admin/payments/{ref}/confirm", headers=staff, json={"note": "NEFT UTR 1234"})
        assert r.status_code == 200 and r.json()["status"] == "paid"
    assert (await client.get("/chat/wallet", headers=admin)).json()["spendable"] == before + 1_000_000
    assert (await client.post("/chat/billing/webhook", content=b"{}")).status_code == 400  # no webhooks

    entries = (await client.get("/chat/admin/audit", headers=staff)).json()["entries"]
    assert any(e["action"] == "manual_payment_confirmed" for e in entries)

    # cancelling: only pending orders, and a cancelled order can't be confirmed later
    other = (await client.post("/chat/billing/checkout", json={"pack_id": "m1m"}, headers=admin)).json()
    ref2 = other["provider_payment_id"]
    assert (await client.post(f"/chat/admin/payments/{ref2}/cancel", headers=staff)).status_code == 204
    assert (await client.post(f"/chat/admin/payments/{ref2}/cancel", headers=staff)).status_code == 409
    assert (await client.post(f"/chat/admin/payments/{ref2}/confirm", headers=staff)).status_code == 409
    assert (await client.post("/chat/admin/payments/AOS-NOPE/confirm", headers=staff)).status_code == 404
    assert (await client.get("/chat/wallet", headers=admin)).json()["spendable"] == before + 1_000_000


async def test_staff_mark_invoices_paid_or_overdue(manual_api):
    client, app, ids, staff = manual_api
    admin = hdr(ids["admin"], ids["org"], "org_admin")
    r = await client.put(
        "/chat/admin/plans/pro",
        headers=staff,
        json={
            "name": "Pro",
            "monthly_fee_minor": 1_999_900,
            "currency": "INR",
            "monthly_included_tokens": 10_000_000,
            "overage_allowed": False,
        },
    )
    assert r.status_code == 200
    invoice_id = (await client.post("/chat/billing/subscription", headers=admin, json={"plan_id": "pro"})).json()[
        "invoice_id"
    ]
    # a draft can't be marked paid; once approved it's "sent" (finance emails it) with a manual reference
    assert (await client.post(f"/chat/admin/invoices/{invoice_id}/mark-paid", headers=staff)).status_code == 409
    sent = (await client.post(f"/chat/admin/invoices/{invoice_id}/approve", headers=staff)).json()
    assert sent["status"] == "sent"

    def status():
        return client.get("/chat/billing/subscription", headers=admin)

    r = await client.post(f"/chat/admin/invoices/{invoice_id}/mark-unpaid", headers=staff)
    assert r.status_code == 200 and (await status()).json()["subscription"]["status"] == "past_due"
    r = await client.post(f"/chat/admin/invoices/{invoice_id}/mark-paid", headers=staff, json={"note": "UTR 99"})
    assert r.status_code == 200 and (await status()).json()["subscription"]["status"] == "active"
    inv = (await client.get(f"/chat/billing/invoices/{invoice_id}", headers=admin)).json()
    assert inv["status"] == "paid"
    assert (await client.post(f"/chat/admin/invoices/{invoice_id}/mark-paid", headers=admin)).status_code == 403
    unknown = uuid.uuid4()
    assert (await client.post(f"/chat/admin/invoices/{unknown}/mark-unpaid", headers=staff)).status_code == 404

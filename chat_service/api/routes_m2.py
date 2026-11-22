"""M2 endpoints: chat actions (Phase 9) and subscription billing, caps and invoices (Phase 10)."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from chat_service.actions.service import ActionError
from chat_service.billing.invoices import InvoiceError
from chat_service.billing.subscriptions import SubscriptionError
from chat_service.db.schema import chat_billing_profiles
from chat_service.principal import Principal

_ACTION_HTTP = {
    "forbidden": 403,
    "not_found": 404,
    "expired": 410,
    "stale": 409,
    "not_allowed": 409,
    "failed": 502,
    "invalid_rules": 422,
}


def _action_error(exc: ActionError) -> HTTPException:
    return HTTPException(_ACTION_HTTP.get(exc.code, 400), {"code": exc.code, "message": exc.message})


class RejectBody(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class ActionRulesBody(BaseModel):
    write_actions_enabled: bool | None = None
    max_budget_change_pct: float | None = Field(default=None, gt=0, le=100)
    max_budget_change_abs_minor: int | None = Field(default=None, ge=0)
    second_approval_above_minor: int | None = Field(default=None, ge=0)
    daily_spend_change_cap_minor: int | None = Field(default=None, ge=0)
    tier1_requires_confirmation: bool | None = None
    roles_by_tier: dict[Literal["1", "2", "3"], list[str]] | None = None
    blocked_tools: list[str] | None = None


class SubscribeBody(BaseModel):
    plan_id: str
    spend_limit_minor: int | None = Field(default=None, ge=0)


class SubscriptionUpdate(BaseModel):
    plan_id: str | None = None
    spend_limit_minor: int | None = Field(default=None, ge=0)
    remove_spend_limit: bool = False


class CapBody(BaseModel):
    scope_type: Literal["org", "workspace", "user"]
    scope_id: uuid.UUID
    period: Literal["day", "month"]
    max_tokens: int = Field(gt=0, le=10_000_000_000)
    hard: bool = True


class BillingProfileBody(BaseModel):
    legal_name: str = Field(min_length=2, max_length=300)
    address: dict[str, Any]
    tax_id: str | None = Field(default=None, max_length=64)
    country: str = Field(min_length=2, max_length=2)
    billing_email: str = Field(min_length=5, max_length=300, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class PlanBody(BaseModel):
    name: str
    monthly_fee_minor: int = Field(ge=0)
    currency: str = Field(min_length=3, max_length=3)
    monthly_included_tokens: int = Field(ge=0)
    overage_allowed: bool = False
    price_per_1m_tokens_minor: int | None = Field(default=None, gt=0)
    max_output_tokens_per_msg: int = Field(default=1200, gt=0)
    requests_per_minute: int = Field(default=20, gt=0)
    active: bool = True


def register_m2_routes(app: FastAPI, services, principal, require_role) -> None:  # noqa: C901 - route table
    org_admin = require_role("org_admin_role")
    staff = require_role("staff_role")

    # ------------------------------------------------------------------ chat actions

    @app.get("/chat/actions")
    async def list_actions(status: str | None = None, p: Principal = Depends(principal), svc=Depends(services)):
        rows = await svc.actions.list(p.org_id, status)
        if not p.has_role(svc.settings.approver_role):
            rows = [r for r in rows if r["requested_by"] == str(p.user_id)]
        return {"actions": rows}

    @app.get("/chat/actions/{action_id}")
    async def get_action(action_id: uuid.UUID, p: Principal = Depends(principal), svc=Depends(services)):
        try:
            view = await svc.actions.get(p.org_id, action_id)
        except ActionError as exc:
            raise _action_error(exc) from exc
        if view["requested_by"] != str(p.user_id) and not p.has_role(svc.settings.approver_role):
            raise HTTPException(404, "action not found")
        return view

    async def _do(fn, *args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except ActionError as exc:
            raise _action_error(exc) from exc

    @app.post("/chat/actions/{action_id}/confirm")
    async def confirm_action(action_id: uuid.UUID, p: Principal = Depends(principal), svc=Depends(services)):
        return await _do(svc.actions.confirm, p, action_id)

    @app.post("/chat/actions/{action_id}/cancel")
    async def cancel_action(action_id: uuid.UUID, p: Principal = Depends(principal), svc=Depends(services)):
        return await _do(svc.actions.cancel, p, action_id)

    @app.post("/chat/actions/{action_id}/approve")
    async def approve_action(action_id: uuid.UUID, p: Principal = Depends(principal), svc=Depends(services)):
        return await _do(svc.actions.approve, p, action_id)

    @app.post("/chat/actions/{action_id}/reject")
    async def reject_action(
        action_id: uuid.UUID, body: RejectBody | None = None, p: Principal = Depends(principal), svc=Depends(services)
    ):
        return await _do(svc.actions.reject, p, action_id, body.reason if body else None)

    @app.post("/chat/actions/{action_id}/undo")
    async def undo_action(action_id: uuid.UUID, p: Principal = Depends(principal), svc=Depends(services)):
        return {"proposal": await _do(svc.actions.undo, p, action_id)}

    @app.get("/chat/action-rules")
    async def get_action_rules(p: Principal = Depends(org_admin), svc=Depends(services)):
        return (await svc.actions.rules(p.org_id)).__dict__

    @app.put("/chat/action-rules")
    async def put_action_rules(body: ActionRulesBody, p: Principal = Depends(org_admin), svc=Depends(services)):
        values = body.model_dump(exclude_unset=True)
        return (await _do(svc.actions.set_rules, p.org_id, **values)).__dict__

    # ------------------------------------------------------------------ plans & subscription

    @app.get("/chat/plans")
    async def list_plans(p: Principal = Depends(principal), svc=Depends(services)):
        return {"plans": await svc.subscriptions.list_plans()}

    @app.get("/chat/billing/subscription")
    async def get_subscription(p: Principal = Depends(org_admin), svc=Depends(services)):
        sub = await svc.subscriptions.get(p.org_id)
        return {"subscription": sub.__dict__ if sub else None, "capacity_tokens": await svc.wallet.capacity(p.org_id)}

    @app.post("/chat/billing/subscription", status_code=201)
    async def subscribe(body: SubscribeBody, p: Principal = Depends(org_admin), svc=Depends(services)):
        try:
            invoice_id = await svc.subscriptions.subscribe(p.org_id, body.plan_id, body.spend_limit_minor)
        except SubscriptionError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"invoice_id": str(invoice_id) if invoice_id else None}

    @app.put("/chat/billing/subscription")
    async def update_subscription(body: SubscriptionUpdate, p: Principal = Depends(org_admin), svc=Depends(services)):
        result: dict[str, Any] = {}
        try:
            if body.plan_id:
                result = await svc.subscriptions.change_plan(p.org_id, body.plan_id)
            if body.remove_spend_limit or body.spend_limit_minor is not None:
                await svc.subscriptions.set_spend_limit(
                    p.org_id, None if body.remove_spend_limit else body.spend_limit_minor
                )
        except SubscriptionError as exc:
            raise HTTPException(409, str(exc)) from exc
        return result | {"subscription": (await svc.subscriptions.get(p.org_id)).__dict__}

    # ------------------------------------------------------------------ billing profile & invoices

    @app.get("/chat/billing/profile")
    async def get_profile(p: Principal = Depends(org_admin), svc=Depends(services)):
        from sqlalchemy import select

        async with svc.db.connect() as conn:
            row = (
                await conn.execute(select(chat_billing_profiles).where(chat_billing_profiles.c.org_id == p.org_id))
            ).first()
        return {"profile": dict(row._mapping) if row else None}

    @app.put("/chat/billing/profile")
    async def put_profile(body: BillingProfileBody, p: Principal = Depends(org_admin), svc=Depends(services)):
        from sqlalchemy import func
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        values = body.model_dump() | {"country": body.country.upper()}
        async with svc.db.begin() as conn:
            await conn.execute(
                pg_insert(chat_billing_profiles)
                .values(org_id=p.org_id, **values)
                .on_conflict_do_update(index_elements=["org_id"], set_=values | {"updated_at": func.now()})
            )
        return {"profile": values}

    @app.get("/chat/billing/invoices")
    async def list_invoices(p: Principal = Depends(org_admin), svc=Depends(services)):
        return {"invoices": await svc.invoices.list(p.org_id)}

    async def _client_invoice(svc, p: Principal, invoice_id: uuid.UUID):
        try:
            inv = await svc.invoices.get(p.org_id, invoice_id)
        except InvoiceError as exc:
            raise HTTPException(404, "invoice not found") from exc
        if inv.status in ("draft", "void"):
            raise HTTPException(404, "invoice not found")
        return inv

    @app.get("/chat/billing/invoices/{invoice_id}")
    async def get_invoice(invoice_id: uuid.UUID, p: Principal = Depends(org_admin), svc=Depends(services)):
        return (await _client_invoice(svc, p, invoice_id)).__dict__

    @app.get("/chat/billing/invoices/{invoice_id}/usage.csv", response_class=PlainTextResponse)
    async def invoice_usage(invoice_id: uuid.UUID, p: Principal = Depends(org_admin), svc=Depends(services)):
        await _client_invoice(svc, p, invoice_id)
        csv_text = await svc.invoices.usage_csv(p.org_id, invoice_id)
        return PlainTextResponse(
            csv_text,
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="usage-{invoice_id}.csv"'},
        )

    # ------------------------------------------------------------------ caps

    @app.get("/chat/caps")
    async def list_caps(p: Principal = Depends(org_admin), svc=Depends(services)):
        return {"caps": await svc.caps.list_caps(p.org_id)}

    @app.put("/chat/caps", status_code=201)
    async def put_cap(body: CapBody, p: Principal = Depends(org_admin), svc=Depends(services)):
        try:
            cap_id = await svc.caps.set_cap(
                p.org_id, body.scope_type, body.scope_id, body.period, body.max_tokens, body.hard
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"cap_id": str(cap_id)}

    @app.delete("/chat/caps/{cap_id}", status_code=204)
    async def delete_cap(cap_id: uuid.UUID, p: Principal = Depends(org_admin), svc=Depends(services)) -> None:
        if not await svc.caps.delete_cap(p.org_id, cap_id):
            raise HTTPException(404, "cap not found")

    # ------------------------------------------------------------------ staff: plans, invoices, cycles

    @app.put("/chat/admin/plans/{plan_id}")
    async def admin_plan(plan_id: str, body: PlanBody, p: Principal = Depends(staff), svc=Depends(services)):
        try:
            await svc.subscriptions.upsert_plan(plan_id=plan_id, **body.model_dump())
        except SubscriptionError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"plan_id": plan_id}

    @app.get("/chat/admin/orgs/{org_id}/invoices")
    async def admin_org_invoices(org_id: uuid.UUID, p: Principal = Depends(staff), svc=Depends(services)):
        return {"invoices": await svc.invoices.list(org_id, include_drafts=True)}

    @app.post("/chat/admin/invoices/{invoice_id}/approve")
    async def admin_approve_invoice(invoice_id: uuid.UUID, p: Principal = Depends(staff), svc=Depends(services)):
        try:
            return (await svc.invoices.approve_and_send(invoice_id, p.user_id)).__dict__
        except InvoiceError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/chat/admin/invoices/{invoice_id}/void", status_code=204)
    async def admin_void_invoice(invoice_id: uuid.UUID, p: Principal = Depends(staff), svc=Depends(services)) -> None:
        try:
            await svc.invoices.void(invoice_id)
        except InvoiceError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/chat/admin/billing/close-cycles")
    async def admin_close_cycles(p: Principal = Depends(staff), svc=Depends(services)):
        created = await svc.subscriptions.close_due_cycles()
        return {"invoices_created": [str(i) for i in created]}

    @app.get("/chat/admin/actions/pending-approval")
    async def admin_pending(org_id: uuid.UUID = Query(...), p: Principal = Depends(staff), svc=Depends(services)):
        return {"actions": await svc.actions.list(org_id, "awaiting_second_approval")}

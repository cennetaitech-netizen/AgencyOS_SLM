"""Operations endpoints: unit economics, quality review, org wallet view (Phase 7), model rollout (Phase 11),
and the org admin's training-data consent (needed before Phase 11 may use an org's conversations)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from chat_service.analytics.economics import unit_economics
from chat_service.analytics.quality import quality_review
from chat_service.llm.router import ModelRouter, RolloutError
from chat_service.principal import Principal


class ConsentBody(BaseModel):
    consent: bool


class CanaryBody(BaseModel):
    model: str = Field(min_length=1, max_length=128)
    percent: int = Field(ge=1, le=100)


class PercentBody(BaseModel):
    percent: int = Field(ge=1, le=100)


class RollbackBody(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


def _range(date_from: date, date_to: date, max_days: int = 92) -> tuple[datetime, datetime]:
    if date_to < date_from or (date_to - date_from).days > max_days:
        raise HTTPException(422, f"date range must be 0–{max_days} days")
    return datetime.combine(date_from, time.min, UTC), datetime.combine(date_to + timedelta(days=1), time.min, UTC)


def register_ops_routes(app: FastAPI, services, principal, require_role, audited) -> None:
    org_admin = require_role("org_admin_role")
    staff = require_role("staff_role")

    # ------------------------------------------------------------------ org admin: settings & consent

    @app.get("/chat/settings")
    async def org_settings(p: Principal = Depends(org_admin), svc=Depends(services)):
        org = await svc.orgs.get(p.org_id)
        return {
            "org_id": str(p.org_id),
            "chat_enabled": org.chat_enabled,
            "billing_mode": org.billing_mode or svc.settings.billing_mode,
            "timezone": org.timezone,
            "currency": org.currency,
            "training_consent": org.training_consent,
        }

    @app.put("/chat/settings/training-consent", dependencies=[audited("training_consent_changed", "self")])
    async def training_consent(body: ConsentBody, p: Principal = Depends(org_admin), svc=Depends(services)):
        await svc.orgs.upsert(
            p.org_id,
            training_consent=body.consent,
            training_consent_at=datetime.now(UTC),
            training_consent_by=p.user_id,
        )
        await svc.analytics.track("training_consent_changed", p.org_id, p.user_id, {"consent": body.consent})
        return {"training_consent": body.consent}

    # ------------------------------------------------------------------ staff: economics, quality, wallets

    @app.get("/chat/admin/economics")
    async def economics(
        date_from: date,
        date_to: date,
        org_id: uuid.UUID | None = None,
        p: Principal = Depends(staff),
        svc=Depends(services),
    ):
        start, end = _range(date_from, date_to)
        return await unit_economics(svc.db, svc.settings, start, end, org_id)

    @app.get("/chat/admin/quality-review")
    async def quality(
        date_from: date,
        date_to: date,
        org_id: uuid.UUID | None = None,
        sample: int = Query(30, ge=0, le=200),
        p: Principal = Depends(staff),
        svc=Depends(services),
    ):
        start, end = _range(date_from, date_to, 31)
        return await quality_review(svc.db, start, end, sample, org_id)

    @app.get("/chat/admin/orgs/{org_id}/wallet")
    async def org_wallet(org_id: uuid.UUID, p: Principal = Depends(staff), svc=Depends(services)):
        bal = await svc.wallet.get_balance(org_id)
        return {
            "tokens_available": bal.tokens_available,
            "tokens_reserved": bal.tokens_reserved,
            "capacity": await svc.wallet.capacity(org_id),
            "has_paid": bal.has_paid,
            "invariant_ok": await svc.wallet.check_invariant(org_id),
            "buckets": [b.__dict__ for b in bal.buckets],
        }

    # ------------------------------------------------------------------ staff: model rollout (Phase 11)

    def _router(svc) -> ModelRouter:
        if not isinstance(svc.model, ModelRouter):
            raise HTTPException(409, "model rollout isn't configured for this deployment")
        return svc.model

    async def _rollout(fn, *args):
        try:
            return (await fn(*args)).__dict__
        except RolloutError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/chat/admin/model-rollout")
    async def rollout_state(p: Principal = Depends(staff), svc=Depends(services)):
        st = await svc.rollout.state()
        models = sorted(svc.model.clients) if isinstance(svc.model, ModelRouter) else [svc.model.name]
        return st.__dict__ | {"available_models": models}

    @app.post("/chat/admin/model-rollout/canary", dependencies=[audited("model_canary_started", "global")])
    async def start_canary(body: CanaryBody, p: Principal = Depends(staff), svc=Depends(services)):
        return await _rollout(_router(svc).store.start_canary, body.model, body.percent)

    @app.post("/chat/admin/model-rollout/percent", dependencies=[audited("model_canary_percent", "global")])
    async def canary_percent(body: PercentBody, p: Principal = Depends(staff), svc=Depends(services)):
        return await _rollout(_router(svc).store.set_percent, body.percent)

    @app.post("/chat/admin/model-rollout/promote", dependencies=[audited("model_promoted", "global")])
    async def promote(p: Principal = Depends(staff), svc=Depends(services)):
        return await _rollout(_router(svc).store.promote)

    @app.post("/chat/admin/model-rollout/rollback", dependencies=[audited("model_rolled_back", "global")])
    async def rollback(body: RollbackBody, p: Principal = Depends(staff), svc=Depends(services)):
        return await _rollout(_router(svc).store.rollback, f"manual: {body.reason}")

    @app.get("/chat/admin/audit")
    async def audit_log(
        org_id: uuid.UUID | None = None,
        limit: int = Query(200, ge=1, le=1000),
        p: Principal = Depends(staff),
        svc=Depends(services),
    ):
        return {"entries": await svc.audit.list(org_id, limit)}

    @app.post("/chat/admin/model-rollout/evaluate")
    async def evaluate(p: Principal = Depends(staff), svc=Depends(services)):
        return await svc.rollout.evaluate_canary(svc.settings)

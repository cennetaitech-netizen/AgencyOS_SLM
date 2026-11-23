"""HTTP API (docs/IMPLEMENTATION_GUIDE.md §7). Run: ``uvicorn chat_service.api.app:create_app --factory``."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import Date, cast, func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from chat_service.actions.service import ActionService
from chat_service.analytics.events import LogAnalyticsSink
from chat_service.api.auth import AuthProvider, DevAuthProvider, InviteAuthProvider, ProductAuthProvider
from chat_service.api.security import SecurityMiddleware
from chat_service.audit import AuditLog
from chat_service.billing.caps import CapsService, LogNotifier
from chat_service.billing.invoices import InvoiceError, InvoiceService, LogInvoiceSink, ManualInvoiceSink
from chat_service.billing.payments import PaymentProvider, WebhookSignatureError, build_payment_provider
from chat_service.billing.subscriptions import SubscriptionService
from chat_service.billing.tokenizer import build_tokenizer
from chat_service.billing.wallet import WalletError, WalletService
from chat_service.config import Settings, get_settings, production_problems
from chat_service.crypto import FieldCipher
from chat_service.db import create_engine
from chat_service.db.schema import chat_token_packs, chat_usage_events
from chat_service.engine.agent import ChatEngine
from chat_service.engine.limits import build_limiters
from chat_service.engine.store import ConversationStore, OrgSettingsStore
from chat_service.llm.client import FallbackModelClient, ModelClient, OpenAICompatibleClient
from chat_service.llm.router import ModelRouter, RolloutStore
from chat_service.metrics import HttpMetricsMiddleware
from chat_service.metrics import render as render_metrics
from chat_service.principal import Principal
from chat_service.tools import build_registry, default_product_api
from chat_service.tools.product import ProductAPI


class NewConversation(BaseModel):
    title: str | None = Field(default=None, max_length=200)


class SendMessage(BaseModel):
    text: str


class Feedback(BaseModel):
    rating: Literal["up", "down"]
    reason: Literal["wrong_numbers", "didnt_understand", "too_long", "other"] | None = None
    comment: str | None = Field(default=None, max_length=2000)


class CheckoutRequest(BaseModel):
    pack_id: str


class OrgSettingsUpdate(BaseModel):
    chat_enabled: bool | None = None
    billing_mode: Literal["shadow", "live"] | None = None
    timezone: str | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    model_pin: str | None = Field(default=None, max_length=128)


class NewInvite(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    org_name: str = Field(min_length=1, max_length=120)
    role: Literal["member", "planner", "org_admin", "staff"] = "org_admin"
    days: int | None = Field(default=None, ge=1, le=365)
    model: str | None = Field(default=None, max_length=128)  # answer this organisation with this model


class PaymentNote(BaseModel):
    note: str = Field(default="", max_length=500)  # e.g. bank transfer reference; kept in the audit log


class Goodwill(BaseModel):
    tokens: int = Field(gt=0, le=10_000_000)
    note: str = Field(min_length=3, max_length=500)


class Services:
    def __init__(
        self,
        settings: Settings,
        db: AsyncEngine,
        model: ModelClient,
        product_api: ProductAPI | None,
        auth: AuthProvider,
        payments: PaymentProvider,
    ) -> None:
        self.settings = settings
        self.db = db
        self.product_api = product_api or default_product_api(settings)
        self.wallet = WalletService(db, settings)
        self.cipher = FieldCipher(settings.data_encryption_keys)
        self.conversations = ConversationStore(db, self.cipher)
        self.orgs = OrgSettingsStore(db)
        self.auth = auth
        self.payments = payments
        self.notifier = LogNotifier()  # TODO(Phase 6): product email / in-app notifications
        sink = ManualInvoiceSink() if settings.payment_provider == "manual" else LogInvoiceSink()
        self.invoices = InvoiceService(db, settings, sink)  # a provider's invoicing replaces the sink (D9)
        self.subscriptions = SubscriptionService(db, settings, self.wallet, self.invoices)
        self.caps = CapsService(db, self.notifier, settings.alert_thresholds)
        self.actions = ActionService(db, settings, self.product_api, orgs=self.orgs, notifier=self.notifier)
        self.analytics = LogAnalyticsSink()  # TODO(Phase 7): product analytics tool
        self.audit = AuditLog(db)
        self.rate_limiter, self.concurrency_limiter = build_limiters(settings)
        self.model = model
        self.rollout = model.store if isinstance(model, ModelRouter) else RolloutStore(db, model.name)
        self.chat = ChatEngine(
            settings,
            self.wallet,
            build_registry(settings, self.product_api),
            model,
            build_tokenizer(settings),
            self.conversations,
            self.orgs,
            actions=self.actions,
            subscriptions=self.subscriptions,
            caps=self.caps,
            analytics=self.analytics,
            rate_limiter=self.rate_limiter,
            concurrency=self.concurrency_limiter,
        )


def _client_for(settings: Settings, model_name: str) -> ModelClient:
    primary = OpenAICompatibleClient(
        settings.model_base_url, settings.model_api_key, model_name, settings.model_timeout_seconds
    )
    fallback = None
    if settings.model_fallback_base_url and settings.model_fallback_name:
        fallback = OpenAICompatibleClient(
            settings.model_fallback_base_url,
            settings.model_fallback_api_key,
            settings.model_fallback_name,
            settings.model_timeout_seconds,
        )
    return FallbackModelClient(primary, fallback)


def model_clients(settings: Settings) -> dict[str, ModelClient]:
    """Every configured model, by name: the main host's models, then Gemini, Ollama and any others."""
    names = [settings.model_name, *[n for n in settings.extra_model_names if n != settings.model_name]]
    clients: dict[str, ModelClient] = {n: _client_for(settings, n) for n in names}
    timeout = settings.model_timeout_seconds
    if settings.gemini_api_key:
        clients[settings.gemini_model] = OpenAICompatibleClient(
            settings.gemini_base_url, settings.gemini_api_key, settings.gemini_model, timeout
        )
    if settings.ollama_base_url:
        for m in settings.ollama_models:
            clients[m] = OpenAICompatibleClient(settings.ollama_base_url, "", m, settings.ollama_timeout_seconds)
    for ep in settings.models:
        clients[ep.name] = OpenAICompatibleClient(ep.base_url, ep.api_key, ep.model or ep.name, timeout, name=ep.name)
    return clients


def default_model_name(settings: Settings, clients: dict[str, ModelClient]) -> str:
    if settings.default_model:
        if settings.default_model not in clients:
            raise RuntimeError(
                f"CHAT_DEFAULT_MODEL={settings.default_model!r} is not a configured model "
                f"(configured: {', '.join(clients)})"
            )
        return settings.default_model
    if settings.gemini_api_key:
        return settings.gemini_model
    if settings.ollama_base_url and settings.ollama_models:
        return settings.ollama_models[0]
    return settings.model_name


def _default_model(settings: Settings, db: AsyncEngine) -> ModelClient:
    """A router over every configured model (main host and its extra versions, Gemini, Ollama, others), so the
    default model, canary rollouts and per-org pinning work without code changes."""
    clients = model_clients(settings)
    return ModelRouter(RolloutStore(db, default_model_name(settings, clients)), clients)


def _check_model(svc: Services, name: str) -> None:
    known = svc.model.clients if isinstance(svc.model, ModelRouter) else {svc.model.name: svc.model}
    if name not in known:
        raise HTTPException(422, f"unknown model {name!r}; configured: {', '.join(sorted(known))}")


def _default_auth(settings: Settings) -> AuthProvider:
    if settings.auth_provider == "dev":
        if settings.env not in ("dev", "test"):
            raise RuntimeError("dev auth is only allowed in dev/test environments")
        return DevAuthProvider()
    if settings.auth_provider == "invite":
        return InviteAuthProvider(settings.invite_secret)
    return ProductAuthProvider(settings.product_api_base_url)


def create_app(
    settings: Settings | None = None,
    db: AsyncEngine | None = None,
    model: ModelClient | None = None,
    product_api: ProductAPI | None = None,
    auth: AuthProvider | None = None,
    payments: PaymentProvider | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    owns_db = db is None
    if settings.env == "production":
        problems = production_problems(settings)
        if problems:
            raise RuntimeError("unsafe production configuration:\n- " + "\n- ".join(problems))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = db or create_engine(settings.database_url)
        app.state.services = Services(
            settings,
            engine,
            model or _default_model(settings, engine),
            product_api,
            auth or _default_auth(settings),
            payments or build_payment_provider(settings),
        )
        yield
        if owns_db:
            await engine.dispose()

    is_prod = settings.env == "production"
    app = FastAPI(
        title="AgencyOS Assistant API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None if is_prod else "/docs",
        redoc_url=None if is_prod else "/redoc",
        openapi_url=None if is_prod else "/openapi.json",
    )
    if settings.cors_allowed_origins:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allowed_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        )
    app.add_middleware(HttpMetricsMiddleware)
    app.add_middleware(
        SecurityMiddleware,
        max_request_bytes=settings.max_request_bytes,
        hsts=is_prod,
        docs_paths=("/docs", "/redoc", "/openapi.json"),
        web=bool(settings.web_dir),
    )

    def services(request: Request) -> Services:
        return request.app.state.services

    async def principal(request: Request, svc: Services = Depends(services)) -> Principal:
        return await svc.auth.authenticate(request)

    def audited(action: str, target: str = "path"):
        """Record a successful change in the audit log. target: 'path' (org_id path param), 'self' (the
        caller's org) or 'global' (platform-wide, e.g. plans and model rollout)."""

        async def dep(request: Request, p: Principal = Depends(principal), svc: Services = Depends(services)):
            yield
            target_org = None
            if target == "path" and "org_id" in request.path_params:
                target_org = uuid.UUID(str(request.path_params["org_id"]))
            elif target == "self":
                target_org = p.org_id
            try:
                body = await request.json() if request.method != "DELETE" else None
            except Exception:  # noqa: BLE001 - no/invalid JSON body
                body = None
            details = {"method": request.method, "path": request.url.path, "body": body}
            await svc.audit.record(p, action, target_org, details)

        return Depends(dep)

    def require_role(role_attr: str):
        async def dep(p: Principal = Depends(principal), svc: Services = Depends(services)) -> Principal:
            if not p.has_role(getattr(svc.settings, role_attr)):
                raise HTTPException(403, "insufficient role")
            return p

        return dep

    # ------------------------------------------------------------------ health

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Liveness: the process is up."""
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(svc: Services = Depends(services)):
        """Readiness: dependencies reachable. Load balancers send traffic only when this is 200."""
        from fastapi.responses import JSONResponse
        from sqlalchemy import text as sql_text

        checks: dict[str, str] = {}
        try:
            async with svc.db.connect() as conn:
                await conn.execute(sql_text("SELECT 1"))
            checks["database"] = "ok"
        except Exception as exc:  # noqa: BLE001
            checks["database"] = f"error: {type(exc).__name__}"
        redis = getattr(svc.rate_limiter, "redis", None)
        if redis is not None:
            try:
                await redis.ping()
                checks["redis"] = "ok"
            except Exception as exc:  # noqa: BLE001 - limits fail open, so this degrades rather than fails
                checks["redis"] = f"degraded: {type(exc).__name__}"
        ok = checks["database"] == "ok"
        return JSONResponse(
            {"status": "ready" if ok else "not_ready", "checks": checks}, status_code=200 if ok else 503
        )

    @app.get("/metrics", include_in_schema=False)
    async def metrics_endpoint(request: Request, svc: Services = Depends(services)):
        from fastapi.responses import Response

        token = svc.settings.metrics_token
        if token and request.headers.get("authorization") != f"Bearer {token}":
            raise HTTPException(401, "metrics token required")
        body, content_type = render_metrics()
        return Response(body, media_type=content_type)

    # ------------------------------------------------------------------ conversations

    @app.post("/chat/conversations", status_code=201)
    async def create_conversation(
        body: NewConversation | None = None, p: Principal = Depends(principal), svc: Services = Depends(services)
    ) -> dict[str, str]:
        cid = await svc.conversations.create(p.org_id, p.user_id, body.title if body else None)
        return {"id": str(cid)}

    @app.get("/chat/conversations")
    async def list_conversations(
        limit: int = Query(50, le=100),
        offset: int = Query(0, ge=0),
        p: Principal = Depends(principal),
        svc: Services = Depends(services),
    ):
        rows = await svc.conversations.list(p.org_id, p.user_id, limit, offset)
        return {
            "conversations": [{"id": str(r["id"]), "title": r["title"], "updated_at": r["updated_at"]} for r in rows]
        }

    @app.get("/chat/conversations/{conversation_id}/messages")
    async def get_messages(
        conversation_id: uuid.UUID, p: Principal = Depends(principal), svc: Services = Depends(services)
    ):
        if await svc.conversations.get(p.org_id, p.user_id, conversation_id) is None:
            raise HTTPException(404, "conversation not found")
        rows = await svc.conversations.messages(conversation_id)
        return {
            "messages": [
                {
                    "id": str(r["id"]),
                    "role": r["role"],
                    "content": r["content"],
                    "tokens_charged": r["tokens_charged"],
                    "created_at": r["created_at"],
                }
                for r in rows
            ]
        }

    @app.patch("/chat/conversations/{conversation_id}")
    async def rename_conversation(
        conversation_id: uuid.UUID,
        body: NewConversation,
        p: Principal = Depends(principal),
        svc: Services = Depends(services),
    ) -> dict[str, str | None]:
        if not body.title or not await svc.conversations.rename(p.org_id, p.user_id, conversation_id, body.title):
            raise HTTPException(404, "conversation not found")
        return {"id": str(conversation_id), "title": body.title}

    @app.delete("/chat/conversations/{conversation_id}", status_code=204)
    async def delete_conversation(
        conversation_id: uuid.UUID, p: Principal = Depends(principal), svc: Services = Depends(services)
    ) -> None:
        if not await svc.conversations.delete(p.org_id, p.user_id, conversation_id):
            raise HTTPException(404, "conversation not found")

    @app.post("/chat/conversations/{conversation_id}/messages")
    async def send_message(
        conversation_id: uuid.UUID,
        body: SendMessage,
        p: Principal = Depends(principal),
        svc: Services = Depends(services),
    ) -> StreamingResponse:
        events = svc.chat.handle_message(p, conversation_id, body.text)
        return StreamingResponse(
            _sse(events, svc.settings.sse_heartbeat_seconds),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
        )

    @app.post("/chat/messages/{message_id}/feedback", status_code=204)
    async def feedback(
        message_id: uuid.UUID, body: Feedback, p: Principal = Depends(principal), svc: Services = Depends(services)
    ) -> None:
        owner = await svc.conversations.message_owner(message_id)
        if owner is None or owner.org_id != p.org_id or owner.user_id != p.user_id or owner.role != "assistant":
            raise HTTPException(404, "message not found")
        await svc.conversations.add_feedback(message_id, p.user_id, body.rating, body.reason, body.comment)
        await svc.analytics.track(
            "chat_answer_rated", p.org_id, p.user_id, {"rating": body.rating, "reason": body.reason}
        )

    # ------------------------------------------------------------------ wallet, usage, packs

    @app.get("/chat/wallet")
    async def wallet(p: Principal = Depends(principal), svc: Services = Depends(services)):
        bal = await svc.wallet.get_balance(p.org_id)
        org = await svc.orgs.get(p.org_id)
        return {
            "tokens_available": bal.tokens_available,
            "tokens_reserved": bal.tokens_reserved,
            "spendable": bal.spendable,
            "low_balance": bal.spendable < svc.settings.low_balance_tokens,
            "billing_mode": org.billing_mode or svc.settings.billing_mode,
            "buckets": [
                {"source": b.source, "remaining": b.remaining, "expires_at": b.expires_at} for b in bal.buckets
            ],
        }

    @app.get("/chat/usage")
    async def usage(
        date_from: date,
        date_to: date,
        p: Principal = Depends(require_role("org_admin_role")),
        svc: Services = Depends(services),
    ):
        if date_to < date_from or (date_to - date_from).days > 92:
            raise HTTPException(422, "date range must be 0–92 days")
        start = datetime.combine(date_from, time.min, UTC)
        end = datetime.combine(date_to + timedelta(days=1), time.min, UTC)
        day = cast(chat_usage_events.c.occurred_at, Date).label("day")
        async with svc.db.connect() as conn:
            rows = (
                await conn.execute(
                    select(
                        day,
                        chat_usage_events.c.user_id,
                        chat_usage_events.c.status,
                        func.sum(chat_usage_events.c.tokens_charged).label("tokens"),
                        func.count().label("messages"),
                    )
                    .where(
                        chat_usage_events.c.org_id == p.org_id,
                        chat_usage_events.c.occurred_at >= start,
                        chat_usage_events.c.occurred_at < end,
                    )
                    .group_by(day, chat_usage_events.c.user_id, chat_usage_events.c.status)
                    .order_by(day)
                )
            ).all()
        return {
            "rows": [
                {
                    "day": r.day,
                    "user_id": str(r.user_id),
                    "status": r.status,
                    "tokens": int(r.tokens),
                    "messages": r.messages,
                }
                for r in rows
            ]
        }

    @app.get("/chat/packs")
    async def packs(p: Principal = Depends(principal), svc: Services = Depends(services)):
        async with svc.db.connect() as conn:
            rows = (
                await conn.execute(
                    select(chat_token_packs)
                    .where(chat_token_packs.c.active.is_(True))
                    .order_by(chat_token_packs.c.tokens)
                )
            ).all()
        return {"packs": [dict(r._mapping) for r in rows]}

    # ------------------------------------------------------------------ buying packs

    @app.post("/chat/billing/checkout", dependencies=[audited("checkout_started", "self")])
    async def checkout(
        body: CheckoutRequest, p: Principal = Depends(require_role("org_admin_role")), svc: Services = Depends(services)
    ):
        async with svc.db.connect() as conn:
            pack = (
                await conn.execute(
                    select(chat_token_packs).where(
                        chat_token_packs.c.pack_id == body.pack_id, chat_token_packs.c.active.is_(True)
                    )
                )
            ).first()
        if pack is None:
            raise HTTPException(404, "pack not found")
        await svc.analytics.track("checkout_started", p.org_id, p.user_id, {"pack_id": pack.pack_id})
        co = await svc.payments.create_checkout(
            p.org_id,
            pack.pack_id,
            pack.price_minor,
            pack.currency,
            f"{svc.settings.app_base_url}/assistant/billing/success",
        )
        await svc.wallet.create_pending_payment(
            p.org_id, pack.pack_id, svc.payments.name, co.provider_payment_id, p.user_id
        )
        return {
            "checkout_url": co.checkout_url,
            "provider_payment_id": co.provider_payment_id,
            "manual": co.checkout_url is None,
            "instructions": co.instructions,
            "amount_minor": pack.price_minor,
            "currency": pack.currency,
        }

    @app.post("/chat/billing/webhook", status_code=204)
    async def webhook(request: Request, svc: Services = Depends(services)) -> None:
        body = await request.body()
        try:
            ev = svc.payments.parse_webhook({k.lower(): v for k, v in request.headers.items()}, body)
        except WebhookSignatureError as exc:
            raise HTTPException(400, "invalid signature") from exc
        try:
            if ev.kind == "paid":
                await svc.wallet.credit_purchase(ev.provider_payment_id)
                await svc.analytics.track("pack_purchased", None, None, {"provider_payment_id": ev.provider_payment_id})
            elif ev.kind in ("refunded", "disputed"):
                await svc.wallet.reverse_purchase(ev.provider_payment_id, ev.kind)
            elif ev.kind == "invoice_paid":
                org_id = await svc.invoices.mark_paid(ev.provider_invoice_id)
                await svc.subscriptions.payment_succeeded(org_id)
            elif ev.kind == "invoice_payment_failed":
                org_id = await svc.invoices.org_for_provider_invoice(ev.provider_invoice_id)
                await svc.subscriptions.payment_failed(org_id)
        except (WalletError, InvoiceError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/chat/billing/orders")
    async def billing_orders(p: Principal = Depends(require_role("org_admin_role")), svc: Services = Depends(services)):
        """This org's token pack orders (pending ones await payment under manual billing)."""
        return {"orders": await svc.wallet.list_payments(p.org_id, limit=20)}

    # ------------------------------------------------------------------ manual billing (AgencyOS staff)

    @app.get("/chat/admin/payments")
    async def admin_payments(
        status: str | None = Query("pending", pattern="^(pending|paid|refunded|disputed|failed)$"),
        p: Principal = Depends(require_role("staff_role")),
        svc: Services = Depends(services),
    ):
        return {"payments": await svc.wallet.list_payments(None, status)}

    @app.post("/chat/admin/payments/{reference}/confirm", dependencies=[audited("manual_payment_confirmed", "global")])
    async def admin_confirm_payment(
        reference: str,
        body: PaymentNote | None = None,
        p: Principal = Depends(require_role("staff_role")),
        svc: Services = Depends(services),
    ):
        """The money for a manual order arrived: credit the pack (once, even if confirmed twice)."""
        provider = await svc.wallet.payment_provider_of(reference)
        if provider is None:
            raise HTTPException(404, "unknown order reference")
        if provider != "manual":
            raise HTTPException(409, "this order is paid through the payment provider, not confirmed by hand")
        try:
            bucket_id = await svc.wallet.credit_purchase(reference)
        except WalletError as exc:
            raise HTTPException(409, str(exc)) from exc
        await svc.analytics.track("pack_purchased", None, p.user_id, {"provider_payment_id": reference})
        return {"reference": reference, "status": "paid", "bucket_id": str(bucket_id)}

    @app.post(
        "/chat/admin/payments/{reference}/cancel",
        status_code=204,
        dependencies=[audited("manual_payment_cancelled", "global")],
    )
    async def admin_cancel_payment(
        reference: str,
        body: PaymentNote | None = None,
        p: Principal = Depends(require_role("staff_role")),
        svc: Services = Depends(services),
    ) -> None:
        try:
            await svc.wallet.cancel_pending_payment(reference)
        except WalletError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/chat/admin/invoices/{invoice_id}/mark-paid", dependencies=[audited("invoice_marked_paid", "global")])
    async def admin_invoice_paid(
        invoice_id: uuid.UUID,
        body: PaymentNote | None = None,
        p: Principal = Depends(require_role("staff_role")),
        svc: Services = Depends(services),
    ):
        """Manual billing: the invoice was paid (e.g. bank transfer). Lifts past-due or suspended status."""
        try:
            org_id = await svc.invoices.mark_paid_by_id(invoice_id)
        except InvoiceError as exc:
            raise HTTPException(409, str(exc)) from exc
        await svc.subscriptions.payment_succeeded(org_id)
        return {"invoice_id": str(invoice_id), "status": "paid"}

    @app.post(
        "/chat/admin/invoices/{invoice_id}/mark-unpaid",
        dependencies=[audited("invoice_marked_unpaid", "global")],
    )
    async def admin_invoice_unpaid(
        invoice_id: uuid.UUID,
        body: PaymentNote | None = None,
        p: Principal = Depends(require_role("staff_role")),
        svc: Services = Depends(services),
    ):
        """Manual billing: the invoice is overdue. The org goes past due; dunning suspends it after the grace
        period (CHAT_DUNNING_GRACE_DAYS) unless it's marked paid first."""
        try:
            org_id = await svc.invoices.org_of(invoice_id)
        except InvoiceError as exc:
            raise HTTPException(404, str(exc)) from exc
        await svc.subscriptions.payment_failed(org_id)
        return {"invoice_id": str(invoice_id), "subscription": "past_due"}

    # ------------------------------------------------------------------ internal admin (AgencyOS staff)

    @app.put("/chat/admin/orgs/{org_id}/settings", dependencies=[audited("org_settings_updated")])
    async def admin_org_settings(
        org_id: uuid.UUID,
        body: OrgSettingsUpdate,
        p: Principal = Depends(require_role("staff_role")),
        svc: Services = Depends(services),
    ):
        values = body.model_dump(exclude_none=True)
        if values.get("model_pin"):
            _check_model(svc, values["model_pin"])
        elif "model_pin" in values:
            values["model_pin"] = None  # "" clears the pin: the organisation gets the default model again
        if "timezone" in values:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

            try:
                ZoneInfo(values["timezone"])
            except ZoneInfoNotFoundError as exc:
                raise HTTPException(422, "unknown timezone") from exc
        org = await svc.orgs.upsert(org_id, **values)
        return org.__dict__ | {"org_id": str(org.org_id)}

    @app.post("/chat/admin/orgs/{org_id}/goodwill", status_code=201, dependencies=[audited("goodwill_granted")])
    async def admin_goodwill(
        org_id: uuid.UUID,
        body: Goodwill,
        p: Principal = Depends(require_role("staff_role")),
        svc: Services = Depends(services),
    ):
        bucket_id = await svc.wallet.grant_goodwill(org_id, body.tokens, p.user_id, body.note)
        return {"bucket_id": str(bucket_id)}

    from chat_service.api.routes_m2 import register_m2_routes

    register_m2_routes(app, services, principal, require_role, audited)
    from chat_service.api.routes_ops import register_ops_routes

    register_ops_routes(app, services, principal, require_role, audited)

    # ------------------------------------------------------------------ test site (invite links)

    @app.get("/chat/me")
    async def me(p: Principal = Depends(principal)):
        return {"user_id": str(p.user_id), "org_id": str(p.org_id), "display_name": p.display_name, "roles": p.roles}

    @app.post("/chat/admin/invites", status_code=201, dependencies=[audited("invite_created", target="global")])
    async def admin_create_invite(
        body: NewInvite, p: Principal = Depends(require_role("staff_role")), svc: Services = Depends(services)
    ):
        """Staff: create a tester's invite link and switch the assistant on for their organisation."""
        from chat_service.invites import ROLE_PRESETS, InviteError, create_invite

        if svc.settings.auth_provider != "invite":
            raise HTTPException(404, "invite links are only used with CHAT_AUTH_PROVIDER=invite")
        try:
            token, invite = create_invite(
                svc.settings.invite_secret,
                body.name,
                body.org_name,
                ROLE_PRESETS[body.role],
                body.days or svc.settings.invite_default_days,
            )
        except InviteError as exc:
            raise HTTPException(422, str(exc)) from exc
        if body.model:
            _check_model(svc, body.model)
            await svc.orgs.upsert(invite.org_id, chat_enabled=True, model_pin=body.model)
        else:
            await svc.orgs.upsert(invite.org_id, chat_enabled=True)
        return {
            "path": f"/?invite={token}",
            "name": invite.name,
            "org_name": invite.org_name,
            "org_id": str(invite.org_id),
            "user_id": str(invite.user_id),
            "role": body.role,
            "expires_at": datetime.fromtimestamp(invite.expires_at, UTC).isoformat(),
        }

    if settings.web_dir:
        from fastapi.staticfiles import StaticFiles

        # Last, so every API route above takes precedence.
        app.mount("/", StaticFiles(directory=settings.web_dir, html=True), name="web")
    return app


async def _sse(events: AsyncIterator[dict[str, Any]], heartbeat_seconds: float) -> AsyncIterator[str]:
    """Format engine events as SSE, with heartbeats while waiting (e.g. during slow tool calls)."""
    it = events.__aiter__()
    pending: asyncio.Task | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(it.__anext__())
            done, _ = await asyncio.wait({pending}, timeout=heartbeat_seconds)
            if not done:
                yield "event: heartbeat\ndata: {}\n\n"
                continue
            try:
                ev = pending.result()
            except StopAsyncIteration:
                return
            finally:
                pending = None
            yield f"event: {ev['event']}\ndata: {json.dumps(ev['data'], default=str)}\n\n"
    finally:
        if pending is not None:
            pending.cancel()
        await events.aclose()

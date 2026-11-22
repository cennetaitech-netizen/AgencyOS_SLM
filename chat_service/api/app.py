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
from chat_service.api.auth import AuthProvider, DevAuthProvider, ProductAuthProvider
from chat_service.billing.caps import CapsService, LogNotifier
from chat_service.billing.invoices import InvoiceError, InvoiceService, LogInvoiceSink
from chat_service.billing.payments import DevPaymentProvider, PaymentProvider, WebhookSignatureError
from chat_service.billing.subscriptions import SubscriptionService
from chat_service.billing.tokenizer import build_tokenizer
from chat_service.billing.wallet import WalletError, WalletService
from chat_service.config import Settings, get_settings
from chat_service.db import create_engine
from chat_service.db.schema import chat_token_packs, chat_usage_events
from chat_service.engine.agent import ChatEngine
from chat_service.engine.store import ConversationStore, OrgSettingsStore
from chat_service.llm.client import FallbackModelClient, ModelClient, OpenAICompatibleClient
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
        self.conversations = ConversationStore(db)
        self.orgs = OrgSettingsStore(db)
        self.auth = auth
        self.payments = payments
        self.notifier = LogNotifier()  # TODO(Phase 6): product email / in-app notifications
        self.invoices = InvoiceService(db, settings, LogInvoiceSink())  # TODO(D9): provider or accounting sink
        self.subscriptions = SubscriptionService(db, settings, self.wallet, self.invoices)
        self.caps = CapsService(db, self.notifier, settings.alert_thresholds)
        self.actions = ActionService(db, settings, self.product_api, orgs=self.orgs, notifier=self.notifier)
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
        )


def _default_model(settings: Settings) -> ModelClient:
    primary = OpenAICompatibleClient(
        settings.model_base_url, settings.model_api_key, settings.model_name, settings.model_timeout_seconds
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


def _default_auth(settings: Settings) -> AuthProvider:
    if settings.auth_provider == "dev":
        if settings.env not in ("dev", "test"):
            raise RuntimeError("dev auth is only allowed in dev/test environments")
        return DevAuthProvider()
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

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = db or create_engine(settings.database_url)
        app.state.services = Services(
            settings,
            engine,
            model or _default_model(settings),
            product_api,
            auth or _default_auth(settings),
            payments or DevPaymentProvider(settings.payment_webhook_secret),
        )
        yield
        if owns_db:
            await engine.dispose()

    app = FastAPI(title="AgencyOS Assistant API", version="0.1.0", lifespan=lifespan)

    def services(request: Request) -> Services:
        return request.app.state.services

    async def principal(request: Request, svc: Services = Depends(services)) -> Principal:
        return await svc.auth.authenticate(request)

    def require_role(role_attr: str):
        async def dep(p: Principal = Depends(principal), svc: Services = Depends(services)) -> Principal:
            if not p.has_role(getattr(svc.settings, role_attr)):
                raise HTTPException(403, "insufficient role")
            return p

        return dep

    # ------------------------------------------------------------------ health

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

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

    @app.post("/chat/billing/checkout")
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
        return {"checkout_url": co.checkout_url, "provider_payment_id": co.provider_payment_id}

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

    # ------------------------------------------------------------------ internal admin (AgencyOS staff)

    @app.put("/chat/admin/orgs/{org_id}/settings")
    async def admin_org_settings(
        org_id: uuid.UUID,
        body: OrgSettingsUpdate,
        p: Principal = Depends(require_role("staff_role")),
        svc: Services = Depends(services),
    ):
        values = body.model_dump(exclude_none=True)
        if "timezone" in values:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

            try:
                ZoneInfo(values["timezone"])
            except ZoneInfoNotFoundError as exc:
                raise HTTPException(422, "unknown timezone") from exc
        org = await svc.orgs.upsert(org_id, **values)
        return org.__dict__ | {"org_id": str(org.org_id)}

    @app.post("/chat/admin/orgs/{org_id}/goodwill", status_code=201)
    async def admin_goodwill(
        org_id: uuid.UUID,
        body: Goodwill,
        p: Principal = Depends(require_role("staff_role")),
        svc: Services = Depends(services),
    ):
        bucket_id = await svc.wallet.grant_goodwill(org_id, body.tokens, p.user_id, body.note)
        return {"bucket_id": str(bucket_id)}

    from chat_service.api.routes_m2 import register_m2_routes

    register_m2_routes(app, services, principal, require_role)
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

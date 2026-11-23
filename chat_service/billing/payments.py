"""Payment provider interface for buying token packs.

- ``ManualPaymentProvider``: no online payments. An order is recorded as pending with a reference, the user sees
  payment instructions (invoice, bank transfer), and AgencyOS staff confirm it once the money arrives
  (``POST /chat/admin/payments/{reference}/confirm``), which credits the pack exactly like a provider's "paid" event.
- ``DevPaymentProvider``: the full online flow (checkout → signed webhook) for development and tests.
A Stripe or Razorpay adapter implements the same protocol when online payments are wanted (decision D9).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class Checkout:
    provider_payment_id: str
    checkout_url: str | None  # None for manual payment: show ``instructions`` instead
    instructions: str | None = None


@dataclass(frozen=True)
class WebhookEvent:
    kind: Literal["paid", "refunded", "disputed", "invoice_paid", "invoice_payment_failed", "ignored"]
    provider_payment_id: str | None
    provider_invoice_id: str | None = None


class WebhookSignatureError(Exception):
    pass


class PaymentProvider(Protocol):
    name: str

    async def create_checkout(
        self, org_id: uuid.UUID, pack_id: str, amount_minor: int, currency: str, success_url: str
    ) -> Checkout: ...

    def parse_webhook(self, headers: dict[str, str], body: bytes) -> WebhookEvent: ...


class ManualPaymentProvider:
    """Orders are paid offline (invoice, bank transfer) and confirmed by AgencyOS staff. No webhooks."""

    name = "manual"

    def __init__(self, instructions: str) -> None:
        self.instructions = instructions

    async def create_checkout(self, org_id, pack_id, amount_minor, currency, success_url) -> Checkout:
        reference = f"AOS-{uuid.uuid4().hex[:10].upper()}"
        return Checkout(reference, None, self.instructions)

    def parse_webhook(self, headers: dict[str, str], body: bytes) -> WebhookEvent:
        raise WebhookSignatureError("manual payments have no webhooks; staff confirm payments instead")


def build_payment_provider(settings) -> PaymentProvider:
    if settings.payment_provider == "manual":
        return ManualPaymentProvider(settings.manual_payment_instructions)
    return DevPaymentProvider(settings.payment_webhook_secret)


class DevPaymentProvider:
    """HMAC-signed fake provider: ``X-Signature: hex(hmac_sha256(secret, body))``."""

    name = "dev"

    def __init__(self, webhook_secret: str) -> None:
        if not webhook_secret:
            raise ValueError("webhook secret required")
        self._secret = webhook_secret.encode()

    async def create_checkout(self, org_id, pack_id, amount_minor, currency, success_url) -> Checkout:
        pid = f"dev_{uuid.uuid4().hex}"
        return Checkout(pid, f"{success_url}?dev_payment={pid}")

    def sign(self, body: bytes) -> str:
        return hmac.new(self._secret, body, hashlib.sha256).hexdigest()

    def parse_webhook(self, headers: dict[str, str], body: bytes) -> WebhookEvent:
        given = headers.get("x-signature", "")
        if not hmac.compare_digest(given, self.sign(body)):
            raise WebhookSignatureError("bad signature")
        data = json.loads(body)
        kind = data.get("type")
        if kind in ("invoice_paid", "invoice_payment_failed"):
            return WebhookEvent(kind, None, data["provider_invoice_id"])
        if kind not in ("paid", "refunded", "disputed"):
            return WebhookEvent("ignored", None)
        return WebhookEvent(kind, data["provider_payment_id"])

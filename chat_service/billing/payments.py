"""Payment provider interface for buying token packs.

``DevPaymentProvider`` implements the full flow (checkout → signed webhook) for development and tests.
A Stripe or Razorpay adapter implements the same protocol once finance picks the provider (decision D9).
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
    checkout_url: str


@dataclass(frozen=True)
class WebhookEvent:
    kind: Literal["paid", "refunded", "disputed", "ignored"]
    provider_payment_id: str | None


class WebhookSignatureError(Exception):
    pass


class PaymentProvider(Protocol):
    name: str

    async def create_checkout(
        self, org_id: uuid.UUID, pack_id: str, amount_minor: int, currency: str, success_url: str
    ) -> Checkout: ...

    def parse_webhook(self, headers: dict[str, str], body: bytes) -> WebhookEvent: ...


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
        if kind not in ("paid", "refunded", "disputed"):
            return WebhookEvent("ignored", None)
        return WebhookEvent(kind, data["provider_payment_id"])

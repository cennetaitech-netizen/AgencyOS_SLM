"""Product analytics events (Phase 7). Swap ``LogAnalyticsSink`` for the product's analytics tool
(Mixpanel, Amplitude, PostHog, GA4, …) by implementing ``AnalyticsSink.track``.

Event names (keep stable, dashboards depend on them):
chat_opened · chat_message_sent · chat_answer_rated · chat_refusal · chat_error · trial_granted ·
low_balance_shown · usage_alert · checkout_started · pack_purchased · action_proposed · action_confirmed ·
subscription_started
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Protocol

log = logging.getLogger("chat_service.analytics")


class AnalyticsSink(Protocol):
    async def track(
        self, event: str, org_id: uuid.UUID | None, user_id: uuid.UUID | None, props: dict[str, Any] | None = None
    ) -> None: ...


class LogAnalyticsSink:
    def __init__(self) -> None:
        self.events: list[tuple[str, uuid.UUID | None, uuid.UUID | None, dict[str, Any]]] = []

    async def track(self, event, org_id, user_id, props=None) -> None:
        props = props or {}
        self.events.append((event, org_id, user_id, props))
        log.info("analytics %s org=%s user=%s %s", event, org_id, user_id, props)

    def names(self) -> list[str]:
        return [e[0] for e in self.events]

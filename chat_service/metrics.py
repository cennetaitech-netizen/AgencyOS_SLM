"""Prometheus metrics (Phase 12). Scraped from ``GET /metrics`` (protect with ``CHAT_METRICS_TOKEN``).

Alert rules for the launch SLOs are in ``ops/prometheus/alerts.yml``.
"""

from __future__ import annotations

import time

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

_LATENCY_BUCKETS = (0.1, 0.25, 0.5, 1, 1.5, 2, 3, 5, 8, 13, 21, 34, 60)

HTTP_REQUESTS = Counter("aos_http_requests_total", "HTTP requests", ["method", "route", "status"])
HTTP_DURATION = Histogram(
    "aos_http_request_duration_seconds",
    "HTTP request duration (non-streaming)",
    ["method", "route"],
    buckets=_LATENCY_BUCKETS,
)
MESSAGES = Counter("aos_chat_messages_total", "Assistant messages by outcome", ["status", "billing_mode"])
ERRORS = Counter("aos_chat_errors_total", "Chat errors shown to users, by code", ["code"])
MESSAGE_LATENCY = Histogram("aos_chat_message_seconds", "Full reply time", buckets=_LATENCY_BUCKETS)
TTFT = Histogram("aos_chat_time_to_first_token_seconds", "Time to first visible token", buckets=_LATENCY_BUCKETS)
BILLABLE_TOKENS = Counter("aos_chat_billable_tokens_total", "Billable tokens (charged or shadow)", ["billing_mode"])
HOST_TOKENS = Counter("aos_chat_host_tokens_total", "Tokens processed by the model host", ["kind", "model"])
HOST_CACHED_TOKENS = Counter(
    "aos_chat_host_cached_prompt_tokens_total",
    "Prompt tokens served from the model host's prefix cache (included in host prompt tokens)",
    ["model"],
)
TOOL_CALLS = Counter("aos_chat_tool_calls_total", "Tool calls", ["tool", "ok"])
ACTIONS = Counter("aos_chat_actions_total", "Chat action outcomes", ["tool", "outcome"])
UNGROUNDED = Counter("aos_chat_ungrounded_answers_total", "Answers flagged by the numeric grounding check")
MODEL_FALLBACK = Counter("aos_model_fallback_total", "Requests served by the fallback model host")
LIMITER_FAIL_OPEN = Counter("aos_limiter_fail_open_total", "Requests allowed because Redis was unreachable", ["kind"])


def render() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST


class HttpMetricsMiddleware:
    """Counts every request by route template (not raw path, to keep label cardinality low)."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        start = time.perf_counter()
        status = {"code": 500, "streaming": False}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                hdrs = dict(message.get("headers") or [])
                status["streaming"] = hdrs.get(b"content-type", b"").startswith(b"text/event-stream")
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            route = getattr(scope.get("route"), "path", None) or "unmatched"
            method = scope.get("method", "GET")
            HTTP_REQUESTS.labels(method, route, str(status["code"])).inc()
            if not status["streaming"]:
                HTTP_DURATION.labels(method, route).observe(time.perf_counter() - start)

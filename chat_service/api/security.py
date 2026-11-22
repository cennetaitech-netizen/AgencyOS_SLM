"""HTTP hardening (Phase 12): request IDs, security headers, request-size limit.

Pure ASGI middleware, so it doesn't buffer streaming (SSE) responses.
"""

from __future__ import annotations

import json
import re
import uuid
from contextvars import ContextVar

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")

_SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"),
    (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
    (b"cache-control", b"no-store"),
]


class SecurityMiddleware:
    def __init__(self, app, max_request_bytes: int, hsts: bool, docs_paths: tuple[str, ...] = ()) -> None:
        self.app = app
        self.max_bytes = max_request_bytes
        self.hsts = hsts
        self.docs_paths = docs_paths

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        incoming = headers.get(b"x-request-id", b"").decode("latin-1")
        rid = incoming if _SAFE_ID.match(incoming) else uuid.uuid4().hex
        token = request_id_var.set(rid)

        declared = headers.get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            await self._too_large(send, rid)
            request_id_var.reset(token)
            return

        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _TooLarge()
            return message

        is_docs = any(scope.get("path", "").startswith(p) for p in self.docs_paths)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                hdrs = list(message.get("headers") or [])
                existing = {k.lower() for k, _ in hdrs}
                for k, v in _SECURITY_HEADERS:
                    if is_docs and k in (b"content-security-policy", b"x-frame-options"):
                        continue  # the interactive API docs need scripts; disabled in production anyway
                    if k not in existing:
                        hdrs.append((k, v))
                if self.hsts:
                    hdrs.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))
                hdrs.append((b"x-request-id", rid.encode()))
                message = {**message, "headers": hdrs}
            await send(message)

        try:
            await self.app(scope, limited_receive, send_with_headers)
        except _TooLarge:
            await self._too_large(send, rid)
        finally:
            request_id_var.reset(token)

    @staticmethod
    async def _too_large(send, rid: str) -> None:
        body = json.dumps({"detail": "request body too large"}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [(b"content-type", b"application/json"), (b"x-request-id", rid.encode())],
            }
        )
        await send({"type": "http.response.body", "body": body})


class _TooLarge(Exception):
    pass

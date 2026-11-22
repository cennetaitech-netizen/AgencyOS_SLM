"""Authentication: turn an incoming request into a ``Principal``.

* ``DevAuthProvider``: header-based identities for local development and tests only.
* ``ProductAuthProvider``: validates the user's AgencyOS token by calling the product.
  ⚠️ The ``/me`` path and response fields are placeholders until Phase 0 documents the product's auth.
"""

from __future__ import annotations

import uuid
from typing import Protocol

import httpx
from fastapi import HTTPException, Request

from chat_service.principal import Principal


class AuthProvider(Protocol):
    async def authenticate(self, request: Request) -> Principal: ...


class DevAuthProvider:
    async def authenticate(self, request: Request) -> Principal:
        try:
            user_id = uuid.UUID(request.headers["X-Dev-User-Id"])
            org_id = uuid.UUID(request.headers["X-Dev-Org-Id"])
        except (KeyError, ValueError) as exc:
            raise HTTPException(401, "missing or invalid dev identity headers") from exc
        roles = tuple(r.strip() for r in request.headers.get("X-Dev-Roles", "member").split(",") if r.strip())
        ws = request.headers.get("X-Dev-Workspace-Id")
        return Principal(
            user_id,
            org_id,
            request.headers.get("X-Dev-Name", "Dev User"),
            roles,
            request.headers.get("X-Dev-Credential"),
            uuid.UUID(ws) if ws else None,
        )


class ProductAuthProvider:
    ME_PATH = "/api/v1/me"  # TODO(Phase 0): real endpoint and field names

    def __init__(self, base_url: str, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(base_url=base_url, timeout=5.0)

    async def authenticate(self, request: Request) -> Principal:
        header = request.headers.get("Authorization", "")
        if not header.lower().startswith("bearer "):
            raise HTTPException(401, "missing bearer token")
        token = header[7:]
        resp = await self._client.get(self.ME_PATH, headers={"Authorization": f"Bearer {token}"})
        if resp.status_code in (401, 403):
            raise HTTPException(401, "invalid or expired token")
        resp.raise_for_status()
        me = resp.json()
        return Principal(
            user_id=uuid.UUID(str(me["user_id"])),
            org_id=uuid.UUID(str(me["org_id"])),
            display_name=me.get("display_name", "User"),
            roles=tuple(me.get("roles", [])),
            credential=token,
        )

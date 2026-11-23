"""Invite links: sign-in for a hosted test site, before the AgencyOS product's own sign-in is connected.

Each link carries a signed token that names one tester: their user id, organisation, display name, roles and expiry.
The service checks the signature with ``CHAT_INVITE_SECRET`` (``CHAT_AUTH_PROVIDER=invite``). Nothing is stored:
- the same organisation name always maps to the same organisation id, so testers from one company share balance,
  settings and approvals;
- to withdraw every link at once, change ``CHAT_INVITE_SECRET``. Links also expire (30 days by default).

Create a link (on the server, e.g. Render's Shell tab):
    python -m chat_service.invites create --name "Priya" --org "Acme Media" --role org_admin
Staff create the others in the test site's Testers tab.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from dataclasses import dataclass

PREFIX = "aos1"
_ORG_NAMESPACE = uuid.UUID("6f1c2b1e-8d43-4f0a-9a52-3c1f0e7d9b21")
ROLE_PRESETS = {
    "member": ("member",),
    "planner": ("planner",),
    "org_admin": ("planner", "org_admin"),
    "staff": ("planner", "org_admin", "agencyos_staff"),
}


class InviteError(ValueError):
    pass


@dataclass(frozen=True)
class Invite:
    user_id: uuid.UUID
    org_id: uuid.UUID
    org_name: str
    name: str
    roles: tuple[str, ...]
    expires_at: int  # unix seconds


def org_id_for(org_name: str) -> uuid.UUID:
    return uuid.uuid5(_ORG_NAMESPACE, " ".join(org_name.lower().split()))


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(secret: str, body: str) -> str:
    return _b64(hmac.new(secret.encode(), f"{PREFIX}.{body}".encode(), hashlib.sha256).digest())


def check_secret(secret: str) -> None:
    if len(secret) < 32:
        raise InviteError("CHAT_INVITE_SECRET must be a random secret of at least 32 characters")


def create_invite(
    secret: str, name: str, org_name: str, roles: tuple[str, ...], days: int, now: float | None = None
) -> tuple[str, Invite]:
    check_secret(secret)
    name, org_name = name.strip(), org_name.strip()
    if not name or not org_name or len(name) > 80 or len(org_name) > 120:
        raise InviteError("name and organisation are required (at most 80 and 120 characters)")
    if not 1 <= days <= 365:
        raise InviteError("days must be between 1 and 365")
    invite = Invite(uuid.uuid4(), org_id_for(org_name), org_name, name, roles, int((now or time.time()) + days * 86400))
    payload = {
        "sub": str(invite.user_id),
        "org": str(invite.org_id),
        "org_name": org_name,
        "name": name,
        "roles": list(roles),
        "exp": invite.expires_at,
    }
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    return f"{PREFIX}.{body}.{_sign(secret, body)}", invite


def read_invite(secret: str, token: str, now: float | None = None) -> Invite:
    try:
        prefix, body, sig = token.split(".")
    except ValueError:
        raise InviteError("malformed invite") from None
    if prefix != PREFIX or not hmac.compare_digest(sig, _sign(secret, body)):
        raise InviteError("invalid invite")
    try:
        p = json.loads(_unb64(body))
        invite = Invite(
            uuid.UUID(p["sub"]), uuid.UUID(p["org"]), str(p["org_name"]), str(p["name"]),
            tuple(str(r) for r in p["roles"]), int(p["exp"]),
        )  # fmt: skip
    except (ValueError, KeyError, TypeError):
        raise InviteError("malformed invite") from None
    if invite.expires_at < (now or time.time()):
        raise InviteError("invite expired")
    return invite


def invite_link(base_url: str, token: str) -> str:
    return f"{base_url.rstrip('/')}/?invite={token}"


async def _enable_org(org_id: uuid.UUID) -> None:
    from chat_service.config import get_settings
    from chat_service.db import create_engine
    from chat_service.engine.store import OrgSettingsStore

    engine = create_engine(get_settings().database_url)
    try:
        await OrgSettingsStore(engine).upsert(org_id, chat_enabled=True)
    finally:
        await engine.dispose()


def main() -> None:
    from chat_service.config import get_settings

    ap = argparse.ArgumentParser(prog="python -m chat_service.invites")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create", help="create an invite link and switch the assistant on for its organisation")
    c.add_argument("--name", required=True, help="the tester's name, shown in the app")
    c.add_argument("--org", required=True, help="organisation name; the same name always means the same organisation")
    c.add_argument("--role", choices=sorted(ROLE_PRESETS), default="org_admin")
    c.add_argument("--days", type=int, default=None)
    c.add_argument("--base-url", default=None, help="the test site's address (default: $RENDER_EXTERNAL_URL)")
    c.add_argument("--no-enable", action="store_true", help="don't switch the assistant on for the organisation")
    args = ap.parse_args()

    settings = get_settings()
    token, invite = create_invite(
        settings.invite_secret, args.name, args.org, ROLE_PRESETS[args.role], args.days or settings.invite_default_days
    )
    if not args.no_enable:
        asyncio.run(_enable_org(invite.org_id))
    base = args.base_url or os.environ.get("RENDER_EXTERNAL_URL") or "http://localhost:8090"
    expires = time.strftime("%Y-%m-%d", time.gmtime(invite.expires_at))
    print(f"{invite.name} ({args.role}, {invite.org_name}), valid until {expires}:")
    print(invite_link(base, token))


if __name__ == "__main__":
    main()

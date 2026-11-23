"""Invite-link sign-in for the hosted test site, the staff endpoint that creates links, and serving the site."""

from __future__ import annotations

import base64
import uuid

import httpx
import pytest

from chat_service.api.app import create_app
from chat_service.api.auth import InviteAuthProvider
from chat_service.config import Settings, production_problems
from chat_service.crypto import FieldCipher
from chat_service.invites import ROLE_PRESETS, InviteError, create_invite, org_id_for, read_invite
from chat_service.llm.fake import FakeModelClient
from chat_service.tools.product import MockProductAPI

SECRET = "x" * 40


def test_invite_round_trip_and_org_ids():
    token, invite = create_invite(SECRET, " Priya ", "Acme  Media", ROLE_PRESETS["org_admin"], 30, now=1_000)
    back = read_invite(SECRET, token, now=2_000)
    assert back == invite
    assert back.name == "Priya" and back.roles == ("planner", "org_admin")
    assert back.org_id == org_id_for("acme media") == org_id_for("ACME MEDIA")  # same org however it's typed
    assert org_id_for("Acme Media") != org_id_for("Acme Media UK")
    _, other = create_invite(SECRET, "Sam", "Acme Media", ROLE_PRESETS["planner"], 30)
    assert other.org_id == invite.org_id and other.user_id != invite.user_id


@pytest.mark.parametrize(
    "mutate, error",
    [
        (lambda t: t[:-2] + ("AA" if not t.endswith("AA") else "BB"), "invalid invite"),
        (
            lambda t: (
                "aos1." + base64.urlsafe_b64encode(b'{"roles":["agencyos_staff"]}').decode() + "." + t.split(".")[2]
            ),
            "invalid",
        ),  # noqa: E501
        (lambda t: "nonsense", "malformed"),
    ],
)
def test_tampered_invites_are_refused(mutate, error):
    token, _ = create_invite(SECRET, "Priya", "Acme", ROLE_PRESETS["member"], 30)
    with pytest.raises(InviteError, match=error):
        read_invite(SECRET, mutate(token))


def test_expired_wrong_secret_and_weak_secret():
    token, _ = create_invite(SECRET, "Priya", "Acme", ROLE_PRESETS["member"], 1, now=1_000)
    with pytest.raises(InviteError, match="expired"):
        read_invite(SECRET, token, now=1_000 + 2 * 86400)
    with pytest.raises(InviteError, match="invalid"):
        read_invite("y" * 40, token, now=1_000)  # changing the secret withdraws every link
    with pytest.raises(InviteError, match="32 characters"):
        create_invite("short", "Priya", "Acme", ("member",), 30)
    with pytest.raises(InviteError, match="32 characters"):
        InviteAuthProvider("short")


def test_settings_for_hosted_platforms():
    s = Settings(database_url="postgres://u:p@db.internal:5432/chat")
    assert s.database_url == "postgresql+asyncpg://u:p@db.internal:5432/chat"
    assert Settings(database_url="postgresql://u:p@h/c").database_url == "postgresql+asyncpg://u:p@h/c"
    # one generated secret (standard base64, as hosting platforms make them) works as the encryption key
    generated = base64.b64encode(bytes(range(32))).decode()
    s = Settings(data_encryption_keys=generated)
    assert s.data_encryption_keys == [generated]
    cipher = FieldCipher(s.data_encryption_keys)
    assert cipher.decrypt_json(cipher.encrypt_json({"a": 1})) == {"a": 1}
    assert Settings(data_encryption_keys='["k1", "k2"]').data_encryption_keys == ["k1", "k2"]
    with pytest.raises(ValueError):
        FieldCipher(["too-short"])
    assert any("invite" in p for p in production_problems(Settings(env="production", auth_provider="invite")))


@pytest.fixture
async def invite_client(engine, settings, tmp_path):
    (tmp_path / "index.html").write_text("<!doctype html><title>t</title>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app-1234.js").write_text("console.log(1)")
    ctxs = []

    async def make(**overrides):
        s = settings.model_copy(
            update={"env": "staging", "auth_provider": "invite", "invite_secret": SECRET, "web_dir": str(tmp_path)}
            | overrides
        )
        app = create_app(s, db=engine, model=FakeModelClient([]), product_api=MockProductAPI())
        ctx = app.router.lifespan_context(app)
        await ctx.__aenter__()
        ctxs.append(ctx)
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")

    yield make
    for ctx in ctxs:
        await ctx.__aexit__(None, None, None)


def _auth(role: str, org: str = "Acme Media") -> dict[str, str]:
    token, _ = create_invite(SECRET, "Tester", org, ROLE_PRESETS[role], 30)
    return {"Authorization": f"Bearer {token}"}


async def test_sign_in_with_an_invite(invite_client):
    c = await invite_client()
    r = await c.get("/chat/me", headers=_auth("org_admin"))
    assert r.status_code == 200
    assert r.json()["roles"] == ["planner", "org_admin"] and r.json()["org_id"] == str(org_id_for("Acme Media"))
    assert (await c.get("/chat/me")).status_code == 401
    assert (await c.get("/chat/me", headers={"Authorization": "Bearer aos1.x.y"})).status_code == 401
    # dev identity headers mean nothing here
    dev = {"X-Dev-User-Id": str(uuid.uuid4()), "X-Dev-Org-Id": str(uuid.uuid4()), "X-Dev-Roles": "agencyos_staff"}
    assert (await c.get("/chat/me", headers=dev)).status_code == 401


async def test_staff_create_invites_that_work_and_switch_the_org_on(invite_client):
    c = await invite_client()
    body = {"name": "Priya", "org_name": "Beta Agency", "role": "org_admin", "days": 7}
    assert (await c.post("/chat/admin/invites", json=body, headers=_auth("org_admin"))).status_code == 403
    r = await c.post("/chat/admin/invites", json=body, headers=_auth("staff", "AgencyOS"))
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["path"].startswith("/?invite=aos1.") and created["org_id"] == str(org_id_for("Beta Agency"))
    tester = {"Authorization": "Bearer " + created["path"].split("=", 1)[1]}
    me = (await c.get("/chat/me", headers=tester)).json()
    assert me["display_name"] == "Priya" and me["org_id"] == created["org_id"]
    # the assistant is on for the new organisation: a conversation can be started and a message answered
    conv = await c.post("/chat/conversations", headers=tester)
    assert conv.status_code == 201
    bad = await c.post("/chat/admin/invites", json=body | {"role": "root"}, headers=_auth("staff", "AgencyOS"))
    assert bad.status_code == 422


async def test_invite_endpoint_only_in_invite_mode(make_dev_client):
    c = await make_dev_client()
    staff = {"X-Dev-User-Id": str(uuid.uuid4()), "X-Dev-Org-Id": str(uuid.uuid4()), "X-Dev-Roles": "agencyos_staff"}
    r = await c.post("/chat/admin/invites", json={"name": "a", "org_name": "b"}, headers=staff)
    assert r.status_code == 404


@pytest.fixture
async def make_dev_client(engine, settings):
    ctxs = []

    async def make():
        app = create_app(settings, db=engine, model=FakeModelClient([]), product_api=MockProductAPI())
        ctx = app.router.lifespan_context(app)
        await ctx.__aenter__()
        ctxs.append(ctx)
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")

    yield make
    for ctx in ctxs:
        await ctx.__aexit__(None, None, None)


async def test_site_pages_get_their_own_security_policy(invite_client):
    c = await invite_client()
    page = await c.get("/")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    csp = page.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "unsafe-eval" not in csp
    asset = await c.get("/assets/app-1234.js")
    assert asset.status_code == 200 and "immutable" in asset.headers["cache-control"]
    api = await c.get("/healthz")
    assert api.headers["content-security-policy"] == "default-src 'none'; frame-ancestors 'none'"
    assert api.json() == {"status": "ok"}  # API routes still win over the site
    assert (await c.get("/chat/me")).status_code == 401


def test_render_blueprint_settings_exist_and_are_safe():
    import re
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[1]
    text = (root / "render.yaml").read_text()
    blueprint = yaml.safe_load(text)
    names = set(re.findall(r"\bCHAT_[A-Z0-9_]+", text)) - {"CHAT_ENV"}
    unknown = sorted(n for n in names if n.removeprefix("CHAT_").lower() not in Settings.model_fields)
    assert unknown == []
    group = {e["key"]: e for e in blueprint["envVarGroups"][0]["envVars"]}
    assert group["CHAT_ENV"]["value"] != "production" and group["CHAT_AUTH_PROVIDER"]["value"] == "invite"
    web = next(s for s in blueprint["services"] if s["type"] == "web")
    web_env = {e.get("key"): e for e in web["envVars"]}
    assert web_env["CHAT_INVITE_SECRET"].get("generateValue") is True
    assert web_env["CHAT_MODEL_API_KEY"].get("sync") is False and "value" not in web_env["CHAT_MODEL_API_KEY"]
    assert web["healthCheckPath"] == "/readyz" and web["preDeployCommand"] == "alembic upgrade head"
    for db in blueprint["databases"]:
        assert db["ipAllowList"] == []  # not reachable from the internet

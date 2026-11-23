"""The product API contract (docs/contracts/product_api.openapi.yaml): the HTTP adapter, sign-in and the
conformance checker work end to end against the mock product served over HTTP, and the adapter's endpoint map
can't drift from the contract."""

from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi import HTTPException
from starlette.requests import Request

from chat_service.api.auth import ProductAuthProvider
from chat_service.tools.mock_product_server import create_mock_product_app
from chat_service.tools.product import ENDPOINTS, HttpProductAPI, MockProductAPI
from chat_service.tools.registry import ToolContext, ToolInputError

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = yaml.safe_load((ROOT / "docs/contracts/product_api.openapi.yaml").read_text())


def checker():
    spec = importlib.util.spec_from_file_location("check_product_api", ROOT / "ops/check_product_api.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_product_api"] = mod  # dataclasses need the module registered
    spec.loader.exec_module(mod)
    return mod


def http_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://product")


def ctx(token: str | None = "test-planner-acme") -> ToolContext:
    return ToolContext(org_id=uuid.uuid4(), user_id=uuid.uuid4(), request_id="r-1", user_credential=token)


def test_adapter_endpoints_are_in_the_contract():
    paths = CONTRACT["paths"]
    for tool, (method, path) in ENDPOINTS.items():
        assert path in paths, f"{tool}: {path} missing from the contract"
        assert method.lower() in paths[path], f"{tool}: {method} {path} missing from the contract"
    assert ProductAuthProvider.ME_PATH in paths


async def test_http_adapter_reads_and_writes_through_the_contract():
    mock = MockProductAPI()
    api = HttpProductAPI("http://product", client=http_for(create_mock_product_app(mock)))
    direct = MockProductAPI()

    for tool, params in [
        ("list_clients", {}),
        ("list_campaigns", {"client_id": "cl_acme"}),
        ("get_campaign_details", {"campaign_id": "cmp_101"}),
        ("get_budget_pacing", {"campaign_id": "cmp_102"}),
        (
            "get_campaign_performance",
            {"client_id": "cl_acme", "date_from": "2026-11-01", "date_to": "2026-11-07", "breakdown": "platform"},
        ),
    ]:
        assert await api.call(ctx(), tool, params) == await direct.call(ctx(), tool, params), tool

    with pytest.raises(ToolInputError):
        await api.call(ctx(), "get_campaign_details", {"campaign_id": "nope"})
    with pytest.raises(PermissionError):
        await api.call(ctx(token=None), "list_clients", {})

    key = "confirm-a1"
    for _ in range(2):  # the assistant retries with the same key: applied once
        result = await api.write(ctx(), "update_campaign_budget", {"campaign_id": "cmp_101", "new_budget": 330000}, key)
    assert result["campaign"]["budget"] == 330000
    await api.write(ctx(), "pause_campaign", {"campaign_id": "cmp_101"}, "confirm-a2")
    await api.write(ctx(), "rename_campaign", {"campaign_id": "cmp_101", "new_name": "Acme Diwali – Brand"}, "a3")
    assert {k: mock.campaigns[0][k] for k in ("budget", "status", "name")} == {
        "budget": 330000,
        "status": "paused",
        "name": "Acme Diwali – Brand",
    }


async def test_product_sign_in():
    auth = ProductAuthProvider("http://product", client=http_for(create_mock_product_app()))

    def request(token: str | None) -> Request:
        headers = [(b"authorization", f"Bearer {token}".encode())] if token else []
        return Request({"type": "http", "headers": headers, "method": "GET", "path": "/"})

    admin = await auth.authenticate(request("test-admin-acme"))
    planner = await auth.authenticate(request("test-planner-acme"))
    assert admin.roles == ("org_admin",) and planner.roles == ("planner",)
    assert admin.org_id == planner.org_id and admin.user_id != planner.user_id
    assert admin.credential == "test-admin-acme"  # forwarded to the product on every tool call
    for bad in (None, "stolen-token"):
        with pytest.raises(HTTPException) as exc:
            await auth.authenticate(request(bad))
        assert exc.value.status_code == 401


async def test_conformance_checker_passes_on_the_reference_implementation():
    async with http_for(create_mock_product_app()) as http:
        report = await checker().run_checks(http, "test-admin-acme", write_campaign="cmp_101")
    assert report.failed == 0, [r for r in report.results if not r[0]]
    assert len(report.results) >= 45


async def test_conformance_checker_reports_a_broken_api():
    app = create_mock_product_app()

    async def drop_currency(scope, receive, send):  # an implementation that forgets "currency" on lists
        async def send_wrapper(message):
            if message["type"] == "http.response.body" and scope["path"] == ENDPOINTS["list_campaigns"][1]:
                data = json.loads(message["body"])
                data.pop("currency", None)
                message = {**message, "body": json.dumps(data).encode()}
            elif message["type"] == "http.response.start" and scope["path"] == ENDPOINTS["list_campaigns"][1]:
                message = {**message, "headers": [h for h in message["headers"] if h[0] != b"content-length"]}
            await send(message)

        await app(scope, receive, send_wrapper)

    async with http_for(drop_currency) as http:
        report = await checker().run_checks(http, "test-admin-acme")
    failed = [name for ok, name, _ in report.results if not ok]
    assert "list_campaigns: matches CampaignList" in failed
    assert all("CampaignList" in name or "list_campaigns" in name for name in failed)


async def test_chat_tool_calls_reach_the_product_over_http_as_the_user(engine, settings):
    from chat_service.api.app import create_app
    from chat_service.llm.fake import FakeModelClient, FakeTurn

    seen: list[str] = []
    product = create_mock_product_app()

    async def record_auth(scope, receive, send):
        if scope["type"] == "http":
            seen.append(dict(scope["headers"]).get(b"authorization", b"").decode())
        await product(scope, receive, send)

    settings.auth_provider = "dev"
    model = FakeModelClient([FakeTurn(tool_calls=[("list_campaigns", {"status": "paused"})]), FakeTurn("One paused.")])
    api = HttpProductAPI("http://product", client=http_for(record_auth))
    app = create_app(settings, db=engine, model=model, product_api=api)
    ctx_mgr = app.router.lifespan_context(app)
    await ctx_mgr.__aenter__()
    try:
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
        org = uuid.uuid4()
        staff = {"X-Dev-User-Id": str(uuid.uuid4()), "X-Dev-Org-Id": str(uuid.uuid4()), "X-Dev-Roles": "agencyos_staff"}
        assert (
            await client.put(f"/chat/admin/orgs/{org}/settings", headers=staff, json={"chat_enabled": True})
        ).is_success
        user = {"X-Dev-User-Id": str(uuid.uuid4()), "X-Dev-Org-Id": str(org), "X-Dev-Credential": "test-planner-acme"}
        conv = (await client.post("/chat/conversations", headers=user, json={})).json()["id"]
        r = await client.post(f"/chat/conversations/{conv}/messages", headers=user, json={"text": "paused campaigns?"})
        assert '"tool": "list_campaigns", "ok": true' in r.text
        tool_result = json.loads(model.requests[1]["messages"][-1]["content"])
        assert [c["name"] for c in tool_result["rows"]] == ["Nova Brand Search"]
        assert seen == ["Bearer test-planner-acme"]  # the user's own token, so the product applies their permissions
    finally:
        await ctx_mgr.__aexit__(None, None, None)

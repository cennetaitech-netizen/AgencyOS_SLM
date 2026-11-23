"""The mock product (``MockProductAPI``) served over HTTP, following docs/contracts/product_api.openapi.yaml.

For development, CI and as a reference for the product team: the real HTTP adapter (``HttpProductAPI``) and sign-in
(``ProductAuthProvider``) talk to it exactly as they will to AgencyOS, and ``ops/check_product_api.py`` passes
against it.

  python -m chat_service.tools.mock_product_server --port 8000

Tokens: any bearer token starting with ``test-`` is accepted. ``test-admin-…`` signs in an org admin, other
``test-…`` tokens a planner. User and org IDs are derived from the token, so different tokens are different users
(``test-admin-acme`` and ``test-planner-acme`` share an org: the part after the role names the org).
"""

from __future__ import annotations

import argparse
import uuid
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from chat_service.tools.product import ENDPOINTS, MockProductAPI, ProductWriteConflict
from chat_service.tools.registry import ToolContext, ToolInputError

ME_PATH = "/api/v1/me"
_NS = uuid.UUID("6f1c3c8e-5b1d-4c7e-9a43-2f0b8d1e7a10")


def _identity(token: str) -> dict[str, Any] | None:
    if not token.startswith("test-"):
        return None
    parts = token.split("-", 2)
    role = parts[1] if len(parts) > 1 else "planner"
    org = parts[2] if len(parts) > 2 else "default"
    return {
        "user_id": str(uuid.uuid5(_NS, token)),
        "org_id": str(uuid.uuid5(_NS, f"org:{org}")),
        "display_name": f"Test {role.title()}",
        "roles": ["org_admin"] if role == "admin" else ["planner"],
    }


def _error(status: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status)


def create_mock_product_app(api: MockProductAPI | None = None) -> Starlette:
    api = api or MockProductAPI()

    def auth(request: Request) -> dict[str, Any] | None:
        header = request.headers.get("authorization", "")
        return _identity(header[7:]) if header.lower().startswith("bearer ") else None

    def ctx(request: Request, who: dict[str, Any]) -> ToolContext:
        return ToolContext(
            org_id=uuid.UUID(who["org_id"]),
            user_id=uuid.UUID(who["user_id"]),
            request_id=request.headers.get("x-request-id", "-"),
        )

    async def me(request: Request):
        who = auth(request)
        return JSONResponse(who) if who else _error(401, "missing or invalid token")

    def read_route(tool: str, path: str) -> Route:
        async def handler(request: Request):
            who = auth(request)
            if who is None:
                return _error(401, "missing or invalid token")
            params: dict[str, Any] = dict(request.query_params) | dict(request.path_params)
            try:
                return JSONResponse(await api.call(ctx(request, who), tool, params))
            except ToolInputError as exc:
                return _error(404 if "not found" in str(exc) else 400, str(exc))
            except (KeyError, ValueError) as exc:
                return _error(400, f"invalid parameters: {exc}")

        return Route(path, handler, methods=["GET"], name=tool)

    async def write(request: Request, tool: str, params: dict[str, Any], key_suffix: str = ""):
        who = auth(request)
        if who is None:
            return _error(401, "missing or invalid token")
        key = request.headers.get("idempotency-key")
        if not key:
            return _error(400, "Idempotency-Key header is required")
        key += key_suffix  # one request that changes two fields is two writes under the same key
        try:
            return JSONResponse(await api.write(ctx(request, who), tool, params, key))
        except ToolInputError as exc:
            return _error(404, str(exc))
        except ProductWriteConflict as exc:
            return _error(409, str(exc))

    async def update_campaign(request: Request):
        body = await request.json()
        cid = request.path_params["campaign_id"]
        if set(body) - {"budget", "name"} or not body:
            return _error(400, "send budget and/or name")
        result = None
        if "budget" in body:
            result = await write(
                request, "update_campaign_budget", {"campaign_id": cid, "new_budget": body["budget"]}, ":budget"
            )
        if "name" in body and (result is None or result.status_code == 200):
            result = await write(request, "rename_campaign", {"campaign_id": cid, "new_name": body["name"]}, ":name")
        return result

    def state_route(tool: str, path: str) -> Route:
        async def handler(request: Request):
            return await write(request, tool, {"campaign_id": request.path_params["campaign_id"]})

        return Route(path, handler, methods=["POST"], name=tool)

    get_details = read_route("get_campaign_details", ENDPOINTS["get_campaign_details"][1])

    async def campaign_handler(request: Request):
        if request.method == "GET":
            return await get_details.endpoint(request)
        return await update_campaign(request)

    routes = [Route(ME_PATH, me, methods=["GET"])]
    for tool in ("list_clients", "list_campaigns", "get_campaign_performance", "get_budget_pacing", "search_help_docs"):
        routes.append(read_route(tool, ENDPOINTS[tool][1]))
    routes.append(Route(ENDPOINTS["get_campaign_details"][1], campaign_handler, methods=["GET", "PATCH"]))
    routes.append(state_route("pause_campaign", ENDPOINTS["pause_campaign"][1]))
    routes.append(state_route("resume_campaign", ENDPOINTS["resume_campaign"][1]))
    return Starlette(routes=routes)


if __name__ == "__main__":
    import uvicorn

    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    uvicorn.run(create_mock_product_app(), host=args.host, port=args.port, log_level="warning")

"""Read tools over the existing AgencyOS product API.

⚠️  PLACEHOLDER MAPPING. The endpoint paths below are *assumptions* until Phase 0 discovery maps each tool
to a real product operation (docs/IMPLEMENTATION_GUIDE.md Phase 0, rule 2: never invent endpoints).
The tool *names, descriptions and schemas* are the contract the model and evals use. Only
``ENDPOINTS`` and the response mapping should change when the real API is connected.

Every call uses the requesting user's own credential, so the product enforces permissions.
"""

from __future__ import annotations

import hashlib
import random
from datetime import date, timedelta
from typing import Any, Protocol

import httpx

from chat_service.tools.registry import Tool, ToolContext, ToolInputError

# Tool name → (HTTP method, path template). TODO(Phase 0): replace with real endpoints.
ENDPOINTS: dict[str, tuple[str, str]] = {
    "list_clients": ("GET", "/api/v1/clients"),
    "list_campaigns": ("GET", "/api/v1/campaigns"),
    "get_campaign_performance": ("GET", "/api/v1/performance"),
    "get_budget_pacing": ("GET", "/api/v1/campaigns/{campaign_id}/pacing"),
    "search_help_docs": ("GET", "/api/v1/help/search"),
    "get_campaign_details": ("GET", "/api/v1/campaigns/{campaign_id}"),
    # Write operations (Phase 9). Only ever called by ActionService after human confirmation.
    "update_campaign_budget": ("PATCH", "/api/v1/campaigns/{campaign_id}"),
    "pause_campaign": ("POST", "/api/v1/campaigns/{campaign_id}/pause"),
    "resume_campaign": ("POST", "/api/v1/campaigns/{campaign_id}/resume"),
    "rename_campaign": ("PATCH", "/api/v1/campaigns/{campaign_id}"),
}

# Write tool → how its arguments map to the request body. TODO(Phase 0): match the real API.
WRITE_BODIES: dict[str, dict[str, str]] = {
    "update_campaign_budget": {"new_budget": "budget"},
    "pause_campaign": {},
    "resume_campaign": {},
    "rename_campaign": {"new_name": "name"},
}


class ProductWriteConflict(Exception):
    """The product refused the change (e.g. state changed, platform rejected it)."""


class ProductAPI(Protocol):
    async def call(self, ctx: ToolContext, tool_name: str, params: dict[str, Any]) -> dict[str, Any]: ...

    async def write(
        self, ctx: ToolContext, tool_name: str, params: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any]: ...


class HttpProductAPI:
    """Calls the real product API with the user's credential. Read-only (GET) in M1."""

    def __init__(self, base_url: str, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(base_url=base_url, timeout=15.0)

    async def call(self, ctx: ToolContext, tool_name: str, params: dict[str, Any]) -> dict[str, Any]:
        method, template = ENDPOINTS[tool_name]
        if method != "GET":
            raise RuntimeError("only read (GET) operations are allowed through HttpProductAPI.call")
        path_params = {k: params[k] for k in _placeholders(template) if k in params}
        query = {k: v for k, v in params.items() if k not in path_params and v is not None}
        headers = {"X-Request-ID": ctx.request_id}
        if ctx.user_credential:
            headers["Authorization"] = f"Bearer {ctx.user_credential}"
        resp = await self._client.request(method, template.format(**path_params), params=query, headers=headers)
        if resp.status_code in (401, 403):
            raise PermissionError(resp.status_code)
        if resp.status_code == 404:
            raise ToolInputError("not found: check the ID (use list_clients / list_campaigns)")
        resp.raise_for_status()
        return resp.json()

    async def write(
        self, ctx: ToolContext, tool_name: str, params: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any]:
        """Execute a confirmed change as the user. The idempotency key makes retries safe."""
        method, template = ENDPOINTS[tool_name]
        if method == "GET":
            raise RuntimeError(f"{tool_name} is not a write operation")
        path_params = {k: params[k] for k in _placeholders(template) if k in params}
        body = {dest: params[src] for src, dest in WRITE_BODIES[tool_name].items() if src in params}
        headers = {"X-Request-ID": ctx.request_id, "Idempotency-Key": idempotency_key}
        if ctx.user_credential:
            headers["Authorization"] = f"Bearer {ctx.user_credential}"
        resp = await self._client.request(method, template.format(**path_params), json=body, headers=headers)
        if resp.status_code in (401, 403):
            raise PermissionError(resp.status_code)
        if resp.status_code in (409, 422):
            raise ProductWriteConflict(resp.text[:300])
        resp.raise_for_status()
        return resp.json() if resp.content else {}


def _placeholders(template: str) -> list[str]:
    import string

    return [f for _, f, _, _ in string.Formatter().parse(template) if f]


# --------------------------------------------------------------------------- mock product (dev & tests)


class MockProductAPI:
    """Deterministic sample data so the chat works end to end before the real API is connected."""

    PLATFORMS = ("google_ads", "meta")

    def __init__(self, today: date | None = None) -> None:
        self.today = today or date(2026, 11, 22)
        self.clients = [
            {"client_id": "cl_acme", "name": "Acme Foods"},
            {"client_id": "cl_nova", "name": "Nova Fitness"},
        ]
        self.campaigns = [
            {
                "campaign_id": "cmp_101",
                "client_id": "cl_acme",
                "name": "Acme Diwali Search",
                "platform": "google_ads",
                "status": "active",
                "budget": 300000,
                "flight_start": "2026-11-01",
                "flight_end": "2026-11-30",
            },
            {
                "campaign_id": "cmp_102",
                "client_id": "cl_acme",
                "name": "Acme Diwali Social",
                "platform": "meta",
                "status": "active",
                "budget": 250000,
                "flight_start": "2026-11-01",
                "flight_end": "2026-11-30",
            },
            {
                "campaign_id": "cmp_201",
                "client_id": "cl_nova",
                "name": "Nova New Year Leads",
                "platform": "meta",
                "status": "active",
                "budget": 120000,
                "flight_start": "2026-11-15",
                "flight_end": "2026-12-31",
            },
            {
                "campaign_id": "cmp_202",
                "client_id": "cl_nova",
                "name": "Nova Brand Search",
                "platform": "google_ads",
                "status": "paused",
                "budget": 60000,
                "flight_start": "2026-10-01",
                "flight_end": "2026-12-31",
            },
        ]
        self.currency = "INR"
        self.write_log: dict[str, dict[str, Any]] = {}  # idempotency key → result
        self.fail_next_write: str | None = None  # tests: simulate a platform rejection

    def _daily(self, campaign_id: str, day: date) -> dict[str, float]:
        seed = int(hashlib.sha256(f"{campaign_id}:{day.isoformat()}".encode()).hexdigest()[:12], 16)
        rnd = random.Random(seed)
        impressions = rnd.randint(20000, 60000)
        clicks = int(impressions * rnd.uniform(0.008, 0.03))
        spend = round(clicks * rnd.uniform(6, 18), 2)
        conversions = int(clicks * rnd.uniform(0.02, 0.08))
        revenue = round(conversions * rnd.uniform(400, 1200), 2)
        return {
            "impressions": impressions,
            "clicks": clicks,
            "spend": spend,
            "conversions": conversions,
            "revenue": revenue,
        }

    def _campaign(self, campaign_id: str) -> dict[str, Any]:
        for c in self.campaigns:
            if c["campaign_id"] == campaign_id:
                return c
        raise ToolInputError(f"campaign {campaign_id!r} not found (use list_campaigns)")

    async def call(self, ctx: ToolContext, tool_name: str, params: dict[str, Any]) -> dict[str, Any]:
        as_of = f"{self.today.isoformat()}T06:00:00Z"
        if tool_name == "list_clients":
            return {"data_as_of": as_of, "rows": self.clients}
        if tool_name == "list_campaigns":
            rows = [
                c
                for c in self.campaigns
                if (not params.get("client_id") or c["client_id"] == params["client_id"])
                and (not params.get("status") or c["status"] == params["status"])
            ]
            return {"data_as_of": as_of, "currency": self.currency, "rows": rows}
        if tool_name == "get_campaign_performance":
            return self._performance(params, as_of)
        if tool_name == "get_budget_pacing":
            c = self._campaign(params["campaign_id"])
            start = date.fromisoformat(c["flight_start"])
            end = date.fromisoformat(c["flight_end"])
            last = min(self.today - timedelta(days=1), end)
            spend = (
                sum(
                    self._daily(c["campaign_id"], start + timedelta(days=i))["spend"]
                    for i in range((last - start).days + 1)
                )
                if last >= start
                else 0.0
            )
            return {
                "data_as_of": as_of,
                "currency": self.currency,
                "campaign_id": c["campaign_id"],
                "name": c["name"],
                "budget": c["budget"],
                "spend_to_date": round(spend, 2),
                "days_elapsed": max(0, (last - start).days + 1),
                "total_days": (end - start).days + 1,
            }
        if tool_name == "get_campaign_details":
            return {"data_as_of": as_of, "currency": self.currency, **self._campaign(params["campaign_id"])}
        if tool_name == "search_help_docs":
            return {
                "rows": [
                    {
                        "title": "Changing a campaign budget",
                        "steps": "Campaigns → select campaign → Budget → Edit → Save",
                    }
                ]
            }
        raise ToolInputError(f"unsupported tool {tool_name}")

    async def write(
        self, ctx: ToolContext, tool_name: str, params: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any]:
        if idempotency_key in self.write_log:
            return self.write_log[idempotency_key]
        if self.fail_next_write:
            msg, self.fail_next_write = self.fail_next_write, None
            raise ProductWriteConflict(msg)
        c = self._campaign(params["campaign_id"])
        if tool_name == "update_campaign_budget":
            c["budget"] = params["new_budget"]
        elif tool_name == "pause_campaign":
            c["status"] = "paused"
        elif tool_name == "resume_campaign":
            c["status"] = "active"
        elif tool_name == "rename_campaign":
            c["name"] = params["new_name"]
        else:
            raise ToolInputError(f"unsupported write {tool_name}")
        result = {"ok": True, "campaign": dict(c)}
        self.write_log[idempotency_key] = result
        return result

    def _performance(self, params: dict[str, Any], as_of: str) -> dict[str, Any]:
        d_from, d_to = date.fromisoformat(params["date_from"]), date.fromisoformat(params["date_to"])
        if d_to < d_from:
            raise ToolInputError("date_to is before date_from")
        if params.get("campaign_id"):
            camps = [self._campaign(params["campaign_id"])]
        elif params.get("client_id"):
            camps = [c for c in self.campaigns if c["client_id"] == params["client_id"]]
        else:
            raise ToolInputError("give campaign_id or client_id")
        breakdown = params.get("breakdown", "none")
        groups: dict[str, dict[str, float]] = {}
        totals = {"impressions": 0, "clicks": 0, "spend": 0.0, "conversions": 0, "revenue": 0.0}
        day = d_from
        while day <= d_to:
            for c in camps:
                m = self._daily(c["campaign_id"], day)
                key = {"platform": c["platform"], "day": day.isoformat(), "campaign": c["name"]}.get(breakdown)
                for k, v in m.items():
                    totals[k] += v
                    if key:
                        groups.setdefault(key, dict.fromkeys(totals, 0))
                        groups[key][k] += v
            day += timedelta(days=1)
        rows = [{breakdown: k, **_derived(v)} for k, v in groups.items()][:20]
        return {
            "data_as_of": as_of,
            "currency": self.currency,
            "date_range": {"from": params["date_from"], "to": params["date_to"]},
            "scope": [c["name"] for c in camps],
            "totals": _derived(totals),
            "rows": rows,
        }


def _derived(m: dict[str, float]) -> dict[str, float]:
    out = {k: round(v, 2) if isinstance(v, float) else v for k, v in m.items()}
    out["ctr_pct"] = round(m["clicks"] / m["impressions"] * 100, 2) if m["impressions"] else None
    out["cpc"] = round(m["spend"] / m["clicks"], 2) if m["clicks"] else None
    out["cpm"] = round(m["spend"] / m["impressions"] * 1000, 2) if m["impressions"] else None
    out["cpa"] = round(m["spend"] / m["conversions"], 2) if m["conversions"] else None
    out["roas"] = round(m["revenue"] / m["spend"], 2) if m["spend"] else None
    return out


# --------------------------------------------------------------------------- tool definitions


def product_tools(api: ProductAPI) -> list[Tool]:
    def handler(name: str):
        async def _h(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
            return await api.call(ctx, name, args)

        return _h

    date_prop = {"type": "string", "format": "date", "pattern": r"^\d{4}-\d{2}-\d{2}$"}
    return [
        Tool(
            name="list_clients",
            label="Looking up clients",
            group="planning",
            description="Clients this user can access, with IDs.",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            handler=handler("list_clients"),
        ),
        Tool(
            name="list_campaigns",
            label="Looking up campaigns",
            group="planning",
            description="Campaigns: ID, name, platform, status, budget, flight dates.",
            parameters={
                "type": "object",
                "properties": {
                    "client_id": {"type": "string"},
                    "status": {"type": "string", "enum": ["active", "paused", "ended"]},
                },
                "additionalProperties": False,
            },
            handler=handler("list_campaigns"),
        ),
        Tool(
            name="get_campaign_performance",
            label="Looking up campaign performance",
            group="reporting",
            description=(
                "Spend, impressions, clicks, conversions, revenue, CTR, CPC, CPM, CPA, ROAS for a campaign "
                "or a client's campaigns over a date range."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "campaign_id": {"type": "string"},
                    "client_id": {"type": "string"},
                    "date_from": date_prop,
                    "date_to": date_prop,
                    "breakdown": {"type": "string", "enum": ["none", "platform", "day", "campaign"]},
                },
                "required": ["date_from", "date_to"],
                "additionalProperties": False,
            },
            handler=handler("get_campaign_performance"),
        ),
        Tool(
            name="get_budget_pacing",
            label="Checking budget pacing",
            group="budgets",
            description="Budget, spend to date, days elapsed, total days (project with media_math pacing_projection).",
            parameters={
                "type": "object",
                "properties": {"campaign_id": {"type": "string"}},
                "required": ["campaign_id"],
                "additionalProperties": False,
            },
            handler=handler("get_budget_pacing"),
        ),
        Tool(
            name="get_campaign_details",
            label="Looking up the campaign",
            group="planning",
            description="Current name, platform, status, budget and flight dates of a campaign.",
            parameters={
                "type": "object",
                "properties": {"campaign_id": {"type": "string"}},
                "required": ["campaign_id"],
                "additionalProperties": False,
            },
            handler=handler("get_campaign_details"),
        ),
        Tool(
            name="search_help_docs",
            label="Searching help articles",
            group="help",
            description="Search AgencyOS help articles (how-to questions).",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string", "minLength": 2}},
                "required": ["query"],
                "additionalProperties": False,
            },
            handler=handler("search_help_docs"),
        ),
    ]

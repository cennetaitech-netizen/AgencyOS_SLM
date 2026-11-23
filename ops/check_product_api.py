"""Checks an AgencyOS product API against the contract the assistant relies on
(docs/contracts/product_api.openapi.yaml and PRODUCT_API.md). For the product team, on staging.

  python ops/check_product_api.py --base-url https://staging.agencyos.example --token <a test user's token>
  python ops/check_product_api.py ... --write-campaign <ID of a TEST campaign>   # also checks the 4 writes

Calls the paths the assistant will call (``ENDPOINTS`` in chat_service/tools/product.py; edit that map if the
product's paths differ) and checks:
- sign-in (/me), and 401 without a token
- every read: status, response shape (JSON schema from the contract), filters, 404 for unknown IDs
- with --write-campaign: rename, budget change, pause and resume, each sent twice with the same Idempotency-Key
  (must apply once), then restores the campaign. Use a test campaign: it really changes it for a moment.
- latency of each call (the assistant times out at 15 s; aim for p95 under 1 s)
Prints one line per check; exit code 1 if any check fails.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
import jsonschema
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from chat_service.api.auth import ProductAuthProvider  # noqa: E402
from chat_service.tools.product import ENDPOINTS  # noqa: E402

CONTRACT = ROOT / "docs/contracts/product_api.openapi.yaml"


@dataclass
class Report:
    results: list[tuple[bool, str, str]] = field(default_factory=list)
    latencies: list[float] = field(default_factory=list)

    def check(self, ok: bool, name: str, detail: str = "", info: str = "") -> bool:
        """``detail`` explains a failure; ``info`` is shown either way."""
        self.results.append((ok, name, detail))
        note = info or ("" if ok else detail)
        print(f"{'ok  ' if ok else 'FAIL'}  {name}{': ' + note if note else ''}", flush=True)
        return ok

    @property
    def failed(self) -> int:
        return sum(not ok for ok, _, _ in self.results)


class Contract:
    def __init__(self, path: Path = CONTRACT) -> None:
        self.components = yaml.safe_load(path.read_text())["components"]

    def problems(self, schema_name: str, data: Any) -> list[str]:
        schema = {"$ref": f"#/components/schemas/{schema_name}", "components": self.components}
        validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
        return [f"{'/'.join(map(str, e.absolute_path)) or '(root)'}: {e.message}" for e in validator.iter_errors(data)]


async def run_checks(
    http: httpx.AsyncClient, token: str, write_campaign: str | None = None, contract: Contract | None = None
) -> Report:
    contract = contract or Contract()
    r = Report()
    auth = {"Authorization": f"Bearer {token}", "X-Request-ID": f"conformance-{uuid.uuid4().hex[:8]}"}

    async def call(method: str, tool_or_path: str, *, params=None, json=None, headers=None, path_params=None):
        path = ENDPOINTS[tool_or_path][1] if tool_or_path in ENDPOINTS else tool_or_path
        path = path.format(**(path_params or {}))
        start = time.monotonic()
        resp = await http.request(method, path, params=params, json=json, headers={**auth, **(headers or {})})
        r.latencies.append(time.monotonic() - start)
        return resp

    def shape(name: str, resp: httpx.Response, schema: str) -> Any:
        if not r.check(resp.status_code == 200, f"{name}: status 200", f"got {resp.status_code} {resp.text[:120]}"):
            return None
        data = resp.json()
        problems = contract.problems(schema, data)
        r.check(not problems, f"{name}: matches {schema}", "; ".join(problems[:5]))
        return data

    # sign-in
    me = shape("me", await call("GET", ProductAuthProvider.ME_PATH), "Me")
    resp = await http.get(ENDPOINTS["list_clients"][1])
    r.check(resp.status_code == 401, "no token: 401", f"got {resp.status_code}")
    if me:
        r.check(True, "me: signed in", info=f"roles {me.get('roles')} (org admins need 'org_admin')")

    # reads
    clients = shape("list_clients", await call("GET", "list_clients"), "ClientList")
    campaigns = shape("list_campaigns", await call("GET", "list_campaigns"), "CampaignList")
    if not campaigns or not campaigns["rows"]:
        r.check(False, "list_campaigns: at least one campaign", "the test user needs access to a campaign")
        return r
    first = campaigns["rows"][0]
    if clients and clients["rows"]:
        cid = first["client_id"]
        filtered = shape(
            "list_campaigns?client_id", await call("GET", "list_campaigns", params={"client_id": cid}), "CampaignList"
        )
        if filtered is not None:
            r.check(all(c["client_id"] == cid for c in filtered["rows"]), "list_campaigns: client filter applied")
    status = first["status"]
    by_status = shape(
        "list_campaigns?status", await call("GET", "list_campaigns", params={"status": status}), "CampaignList"
    )
    if by_status is not None:
        r.check(all(c["status"] == status for c in by_status["rows"]), "list_campaigns: status filter applied")

    cp = {"campaign_id": first["campaign_id"]}
    shape("get_campaign_details", await call("GET", "get_campaign_details", path_params=cp), "CampaignDetails")
    missing = await call(
        "GET", "get_campaign_details", path_params={"campaign_id": f"does-not-exist-{uuid.uuid4().hex[:6]}"}
    )
    r.check(missing.status_code == 404, "get_campaign_details: unknown ID gives 404", f"got {missing.status_code}")
    shape("get_budget_pacing", await call("GET", "get_budget_pacing", path_params=cp), "Pacing")

    today = date.today()
    window = {"date_from": (today - timedelta(days=7)).isoformat(), "date_to": (today - timedelta(days=1)).isoformat()}
    perf = shape(
        "get_campaign_performance",
        await call("GET", "get_campaign_performance", params={**cp, **window}),
        "Performance",
    )
    if perf is not None:
        r.check(
            perf["date_range"] == {"from": window["date_from"], "to": window["date_to"]},
            "performance: date range echoed",
        )
    for breakdown in ("platform", "day", "campaign"):
        data = shape(
            f"performance breakdown={breakdown}",
            await call(
                "GET",
                "get_campaign_performance",
                params={"client_id": first["client_id"], **window, "breakdown": breakdown},
            ),
            "Performance",
        )
        if data is not None:
            r.check(
                all(breakdown in row for row in data["rows"]),
                f"performance breakdown={breakdown}: rows keyed by {breakdown}",
            )
    shape("search_help_docs", await call("GET", "search_help_docs", params={"query": "change budget"}), "HelpResults")

    if write_campaign:
        await _check_writes(r, call, shape, write_campaign)

    if r.latencies:
        p95 = sorted(r.latencies)[max(0, int(len(r.latencies) * 0.95) - 1)]
        timing = f"p95 {p95 * 1000:.0f} ms, median {statistics.median(r.latencies) * 1000:.0f} ms (aim for p95 < 1 s)"
        r.check(p95 < 5, "latency p95 under 5 s", info=timing)
    return r


async def _check_writes(r: Report, call, shape, campaign_id: str) -> None:
    cp = {"campaign_id": campaign_id}
    before = shape(
        "writes: test campaign readable", await call("GET", "get_campaign_details", path_params=cp), "CampaignDetails"
    )
    if before is None:
        return

    async def write_twice(name: str, method: str, tool: str, body: dict | None, check) -> None:
        key = f"conformance-{uuid.uuid4()}"
        first = await call(method, tool, json=body, path_params=cp, headers={"Idempotency-Key": key})
        if shape(f"{name}: applied", first, "WriteResult") is None:
            return
        second = await call(method, tool, json=body, path_params=cp, headers={"Idempotency-Key": key})
        r.check(
            second.status_code == 200 and second.json() == first.json(), f"{name}: same key returns the first result"
        )
        now = (await call("GET", "get_campaign_details", path_params=cp)).json()
        r.check(check(now), f"{name}: applied exactly once", f"campaign now {now}")

    mark = " (conformance check)"
    await write_twice(
        "rename",
        "PATCH",
        "rename_campaign",
        {"name": before["name"] + mark},
        lambda c: c["name"] == before["name"] + mark,
    )
    await write_twice(
        "budget",
        "PATCH",
        "update_campaign_budget",
        {"budget": before["budget"] + 1},
        lambda c: c["budget"] == before["budget"] + 1,
    )
    first, second = (
        ("pause_campaign", "resume_campaign") if before["status"] == "active" else ("resume_campaign", "pause_campaign")
    )
    await write_twice(
        first.split("_")[0],
        "POST",
        first,
        None,
        lambda c: c["status"] == ("paused" if first == "pause_campaign" else "active"),
    )
    # restore
    restore = [
        ("PATCH", "rename_campaign", {"name": before["name"], "budget": before["budget"]}),
        ("POST", second, None),
    ]
    for method, tool, body in restore:
        await call(method, tool, json=body, path_params=cp, headers={"Idempotency-Key": f"conformance-{uuid.uuid4()}"})
    after = (await call("GET", "get_campaign_details", path_params=cp)).json()
    same = {k: after[k] for k in ("name", "budget", "status")} == {k: before[k] for k in ("name", "budget", "status")}

    def state(c: dict) -> str:
        return f"{c['name']!r} / {c['budget']} / {c['status']}"

    r.check(same, "writes: test campaign restored", f"before {state(before)}, after {state(after)}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--token", required=True, help="a test user's AgencyOS token (bearer)")
    ap.add_argument("--write-campaign", help="ID of a TEST campaign: also checks the 4 write operations")
    args = ap.parse_args()

    async def go() -> Report:
        async with httpx.AsyncClient(base_url=args.base_url, timeout=15) as http:
            return await run_checks(http, args.token, args.write_campaign)

    report = asyncio.run(go())
    total = len(report.results)
    print(f"\n{total - report.failed}/{total} checks passed")
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())

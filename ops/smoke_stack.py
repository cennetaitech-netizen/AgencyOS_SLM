"""End-to-end smoke test of a running stack (docker compose --profile monitoring, or staging with dev auth).
Standard library only, so it runs on a bare CI runner.

  python ops/smoke_stack.py --api http://localhost:8090 --prometheus http://localhost:9090 \\
      --grafana http://localhost:3000 --metrics-token dev-metrics-token

Checks, waiting up to --timeout seconds for each service to come up:
1. API ready (database and Redis), security headers present, /metrics refuses requests without the token.
2. A chat message streams end to end through the model host: message.start → tokens → message.end, with tokens
   counted, and the conversation and wallet endpoints answer.
3. /metrics (with the token) shows the message, including host and cached prompt tokens.
4. Prometheus scrapes the API and has loaded the alert rules.
5. Grafana is healthy, its Prometheus data source works, and the AgencyOS Assistant dashboard is provisioned.
Prints one line per check and exits 1 on the first failure.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

STAFF = {"X-Dev-User-Id": str(uuid.uuid4()), "X-Dev-Org-Id": str(uuid.uuid4()), "X-Dev-Roles": "agencyos_staff"}


def request(method: str, url: str, headers: dict | None = None, body: dict | None = None, timeout: float = 60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={**(headers or {})})
    if data is not None:
        req.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def wait_for(name: str, check, timeout: float):
    deadline, last = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except (OSError, ValueError, KeyError) as exc:
            last = exc
        time.sleep(2)
    raise AssertionError(f"{name}: not ready after {timeout:.0f}s ({last})")


def ok(msg: str) -> None:
    print(f"ok    {msg}", flush=True)


def check_api(api: str, timeout: float) -> None:
    def ready():
        status, headers, body = request("GET", f"{api}/readyz", timeout=5)
        return status == 200 and json.loads(body)["status"] == "ready" and headers

    headers = wait_for("API /readyz", ready, timeout)
    lower = {k.lower() for k in headers}
    assert {"x-request-id", "x-content-type-options"} <= lower, f"security headers missing: {sorted(lower)}"
    ok("API ready (database, Redis) with request ID and security headers")


def check_chat(api: str) -> dict:
    org = str(uuid.uuid4())
    status, _, body = request("PUT", f"{api}/chat/admin/orgs/{org}/settings", STAFF, {"chat_enabled": True})
    assert status == 200, f"enable org: {status} {body[:200]!r}"
    user = {"X-Dev-User-Id": str(uuid.uuid4()), "X-Dev-Org-Id": org, "X-Dev-Roles": "planner"}
    status, _, body = request("POST", f"{api}/chat/conversations", user, {})
    assert status == 201, f"create conversation: {status} {body[:200]!r}"
    conv = json.loads(body)["id"]
    status, headers, body = request(
        "POST", f"{api}/chat/conversations/{conv}/messages", user, {"text": "How are my active campaigns doing?"}
    )
    assert status == 200 and headers.get("content-type", "").startswith("text/event-stream"), f"stream: {status}"
    events, event = [], None
    for line in body.decode().splitlines():
        if line.startswith("event: "):
            event = line[7:]
        elif line.startswith("data: ") and event:
            events.append((event, json.loads(line[6:])))
    names = [e for e, _ in events]
    assert names and names[0] == "message.start" and names[-1] == "message.end", f"events: {names}"
    assert "token" in names, "no tokens streamed"
    end = events[-1][1]
    assert end["billable_input_tokens"] > 0 and end["billable_output_tokens"] > 0, end
    ok(f"chat message streamed end to end ({names.count('token')} token events, {end['tokens_charged']} tokens)")
    for path in ("/chat/wallet", "/chat/conversations"):
        status, _, _ = request("GET", f"{api}{path}", user)
        assert status == 200, f"{path}: {status}"
    ok("wallet and conversation list answer")
    return end


def check_metrics(api: str, token: str) -> None:
    status, _, _ = request("GET", f"{api}/metrics")
    assert status == 401, f"/metrics without token should be 401, got {status}"
    status, _, body = request("GET", f"{api}/metrics", {"Authorization": f"Bearer {token}"})
    text = body.decode()
    assert status == 200, f"/metrics with token: {status}"
    for name in ("aos_chat_messages_total", "aos_chat_host_tokens_total", "aos_chat_host_cached_prompt_tokens_total"):
        assert name in text, f"{name} missing from /metrics"
    ok("/metrics refuses requests without the token and shows chat, host and cache counters")


def check_prometheus(prom: str, timeout: float) -> None:
    def scraped():
        q = urllib.parse.quote('sum(aos_chat_messages_total{job="agencyos-assistant"})')
        _, _, body = request("GET", f"{prom}/api/v1/query?query={q}", timeout=5)
        result = json.loads(body)["data"]["result"]
        return result and float(result[0]["value"][1]) >= 1

    wait_for("Prometheus scrape of the API", scraped, 90)
    _, _, body = request("GET", f"{prom}/api/v1/rules")
    rules = [r["name"] for g in json.loads(body)["data"]["groups"] for r in g["rules"]]
    assert "AosAvailabilityBudgetBurn" in rules and "AosPromptCacheHitLow" in rules, rules
    ok(f"Prometheus scrapes the API and loaded {len(rules)} alert rules")


def check_grafana(grafana: str, user: str, password: str, timeout: float) -> None:
    auth = {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}
    wait_for("Grafana health", lambda: request("GET", f"{grafana}/api/health", timeout=5)[0] == 200, timeout)
    status, _, body = request("GET", f"{grafana}/api/datasources/uid/prometheus/health", auth)
    assert status == 200 and json.loads(body).get("status") == "OK", f"data source: {status} {body[:200]!r}"

    def dashboard():
        status, _, body = request("GET", f"{grafana}/api/dashboards/uid/agencyos-assistant", auth, timeout=5)
        return status == 200 and json.loads(body)["dashboard"]["panels"]

    panels = wait_for("Grafana dashboard provisioning", dashboard, 60)
    ok(f"Grafana healthy, Prometheus data source OK, dashboard provisioned ({len(panels)} panels incl. rows)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8090")
    ap.add_argument("--prometheus", help="skip if not given")
    ap.add_argument("--grafana", help="skip if not given")
    ap.add_argument("--grafana-user", default="admin")
    ap.add_argument("--grafana-password", default="admin")
    ap.add_argument("--metrics-token", default="dev-metrics-token")
    ap.add_argument("--timeout", type=float, default=180)
    args = ap.parse_args()
    try:
        check_api(args.api, args.timeout)
        check_chat(args.api)
        check_metrics(args.api, args.metrics_token)
        if args.prometheus:
            check_prometheus(args.prometheus, args.timeout)
        if args.grafana:
            check_grafana(args.grafana, args.grafana_user, args.grafana_password, args.timeout)
    except AssertionError as exc:
        print(f"FAIL  {exc}", flush=True)
        return 1
    print("smoke test passed", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

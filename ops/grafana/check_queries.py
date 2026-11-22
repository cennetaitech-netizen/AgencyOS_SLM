"""Runs every query on the Grafana dashboard against a Prometheus and reports which ones return data.

  python ops/grafana/check_queries.py --prometheus http://localhost:9090 --minutes 30

Use after deploying, or after a load test, to confirm each panel has data. A panel with no data is not
always a fault: fallback, fail-open, 5xx and firing-alert panels stay empty while everything is healthy.
Exit code 1 means a query failed to run (syntax or an unknown function), not that it was empty.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

DASHBOARD = Path(__file__).parent / "dashboards" / "agencyos-assistant.json"


def run(prom: str, expr: str, minutes: int, instant: bool) -> dict:
    expr = expr.replace("$__rate_interval", "1m").replace('instance=~"$instance"', 'instance=~".*"')
    now = time.time()
    if instant:
        params = {"query": expr, "time": now}
        path = "/api/v1/query"
    else:
        params = {"query": expr, "start": now - minutes * 60, "end": now, "step": 15}
        path = "/api/v1/query_range"
    url = f"{prom.rstrip('/')}{path}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read() or b"{}") | {"status": "error"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prometheus", default="http://localhost:9090")
    ap.add_argument("--minutes", type=int, default=30)
    args = ap.parse_args()

    panels = json.loads(DASHBOARD.read_text())["panels"]
    failed = 0
    for panel in panels:
        for t in panel.get("targets", []):
            res = run(args.prometheus, t["expr"], args.minutes, bool(t.get("instant")))
            if res.get("status") != "success":
                failed += 1
                print(f"ERROR  {panel['title']} [{t['refId']}]: {res.get('error')}")
                continue
            series = res["data"]["result"]
            values = [float(v[1]) for s in series for v in (s.get("values") or [s["value"]])]
            finite = [v for v in values if math.isfinite(v)]
            if finite:
                labels = sorted({json.dumps({k: v for k, v in s["metric"].items() if k != "__name__"}) for s in series})
                print(
                    f"ok     {panel['title']} [{t['refId']}]: {len(series)} series, last max "
                    f"{max(finite):.4g}; {', '.join(labels)[:110]}"
                )
            else:
                print(f"empty  {panel['title']} [{t['refId']}]")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

"""Load test (Phase 12): many orgs and users chatting at once against one or more API instances.

  python ops/loadtest.py --api http://localhost:8090 --api http://localhost:8091 \\
      --orgs 25 --users-per-org 3 --duration 60 --staff-header

Each virtual user loops: new conversation → streamed message → (sometimes) wallet and conversation list.
Requests are spread round-robin across the ``--api`` instances (as a load balancer would).
Reports throughput, success rate, error codes, p50/p95/p99 time-to-first-token and full reply time.
Uses dev auth headers: run against a dev/staging deployment only.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import random
import statistics
import time
import uuid
from collections import Counter

import httpx

QUESTIONS = [
    "How are my active campaigns doing?",
    "Which campaigns are active right now?",
    "Give me a quick summary of last week",
    "Any campaigns I should look at today?",
]


def pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int(round(p / 100 * (len(values) - 1))))]


class Stats:
    def __init__(self) -> None:
        self.ttft: list[float] = []
        self.total: list[float] = []
        self.outcomes: Counter[str] = Counter()
        self.other_requests = 0
        self.other_failures = 0


async def user_loop(client: httpx.AsyncClient, apis, headers: dict, stop_at: float, stats: Stats) -> None:
    while time.monotonic() < stop_at:
        base = next(apis)
        try:
            conv = (await client.post(f"{base}/chat/conversations", json={}, headers=headers)).json()["id"]
            start = time.monotonic()
            first = None
            outcome = "no_end"
            async with client.stream(
                "POST",
                f"{base}/chat/conversations/{conv}/messages",
                json={"text": random.choice(QUESTIONS)},
                headers=headers,
            ) as resp:
                if resp.status_code != 200:
                    outcome = f"http_{resp.status_code}"
                else:
                    event = None
                    async for line in resp.aiter_lines():
                        if line.startswith("event: "):
                            event = line[7:]
                            if event == "token" and first is None:
                                first = time.monotonic() - start
                        elif line.startswith("data: ") and event == "message.end":
                            outcome = "ok"
                        elif line.startswith("data: ") and event == "error":
                            outcome = json.loads(line[6:])["code"]
            stats.outcomes[outcome] += 1
            if outcome == "ok":
                stats.total.append(time.monotonic() - start)
                if first is not None:
                    stats.ttft.append(first)
            if random.random() < 0.3:
                for path in ("/chat/wallet", "/chat/conversations"):
                    stats.other_requests += 1
                    r = await client.get(f"{next(apis)}{path}", headers=headers)
                    stats.other_failures += r.status_code >= 400
        except httpx.HTTPError as exc:
            stats.outcomes[f"transport_{type(exc).__name__}"] += 1
        await asyncio.sleep(random.uniform(0.2, 1.0))  # think time


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", action="append", required=True)
    ap.add_argument("--orgs", type=int, default=25)
    ap.add_argument("--users-per-org", type=int, default=3)
    ap.add_argument("--duration", type=int, default=60)
    ap.add_argument("--out", help="write the JSON summary here")
    args = ap.parse_args()

    apis = itertools.cycle(args.api)
    limits = httpx.Limits(max_connections=1000, max_keepalive_connections=200)
    stats = Stats()
    async with httpx.AsyncClient(timeout=60, limits=limits) as client:
        staff = {"X-Dev-User-Id": str(uuid.uuid4()), "X-Dev-Org-Id": str(uuid.uuid4()), "X-Dev-Roles": "agencyos_staff"}
        orgs = [uuid.uuid4() for _ in range(args.orgs)]
        for org in orgs:
            r = await client.put(
                f"{args.api[0]}/chat/admin/orgs/{org}/settings",
                json={"chat_enabled": True, "billing_mode": "live"},
                headers=staff,
            )
            r.raise_for_status()
        users = [
            {"X-Dev-User-Id": str(uuid.uuid4()), "X-Dev-Org-Id": str(org), "X-Dev-Roles": "planner"}
            for org in orgs
            for _ in range(args.users_per_org)
        ]
        started = time.monotonic()
        stop_at = started + args.duration
        await asyncio.gather(*(user_loop(client, apis, u, stop_at, stats) for u in users))
        elapsed = time.monotonic() - started

    total = sum(stats.outcomes.values())
    summary = {
        "virtual_users": len(users),
        "orgs": args.orgs,
        "instances": len(args.api),
        "duration_s": round(elapsed, 1),
        "messages": total,
        "messages_per_s": round(total / elapsed, 2),
        "ok_rate": round(stats.outcomes["ok"] / total, 4) if total else None,
        "outcomes": dict(stats.outcomes),
        "ttft_ms": {p: round(pct(stats.ttft, p) * 1000) for p in (50, 95, 99)} if stats.ttft else None,
        "reply_ms": {p: round(pct(stats.total, p) * 1000) for p in (50, 95, 99)} if stats.total else None,
        "mean_reply_ms": round(statistics.mean(stats.total) * 1000) if stats.total else None,
        "other_requests": stats.other_requests,
        "other_failures": stats.other_failures,
        "orgs_list": [str(o) for o in orgs],
    }
    print(json.dumps({k: v for k, v in summary.items() if k != "orgs_list"}, indent=2))
    if args.out:
        with open(args.out, "w") as f:  # noqa: ASYNC230 - one small write at the end
            json.dump(summary, f, indent=2)


if __name__ == "__main__":
    asyncio.run(main())

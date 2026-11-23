"""Simulates a working day of chat traffic against a model host's prefix cache, to compare prompt layouts and size
the cache. Uses the engine's real prompt builder and tool schemas, and the block cache in ``prefix_cache.py``.

  python ops/prompt_cache_sim.py --users 2000 --orgs 300 --cache-tokens 100000 300000 1000000

Layouts compared:
- ``context-in-system``: the old layout (``system_v1``). Date, user name and role sit in the system prompt, so the
  prefix is shared only by one user's own messages on one day, and only while they stay in the cache.
- ``static-prefix``: the current layout (``system_v3``). System prompt and tool schemas are identical for everyone;
  the context is a line at the start of the latest user message.

It is a model, not a measurement: traffic shape, answer and tool-result sizes are assumptions (see the arguments).
Re-measure with the real model host in staging (``aos_chat_host_cached_prompt_tokens_total`` on the dashboard).
"""

from __future__ import annotations

import argparse
import json
import random
from dataclasses import dataclass, field

from prefix_cache import CHARS_PER_TOKEN, PrefixCache, render

from chat_service.config import Settings
from chat_service.engine.prompting import PromptContext, build_messages, load_prompt
from chat_service.tools import build_registry
from chat_service.tools.product import MockProductAPI

TIMEZONES = [("Asia/Kolkata", "INR"), ("Europe/London", "GBP"), ("America/New_York", "USD"), ("Asia/Dubai", "AED")]
ROLES = ["planner", "analyst", "org_admin", "member"]
QUESTION = "What was the {m} for client {c} over {p}, and which campaign should I look at first?"


@dataclass
class Call:
    at: float
    user: int
    conv: int
    turn: int


@dataclass
class Result:
    messages: int = 0
    billable: int = 0
    prompt: int = 0
    cached: int = 0
    completion: int = 0
    per_layout: dict = field(default_factory=dict)


def schedule(args, rng: random.Random) -> list[Call]:
    """Each user has a few sessions in a 9-hour day; each session is 1-4 messages, 20-120 s apart."""
    calls: list[Call] = []
    conv = 0
    for user in range(args.users):
        for _ in range(rng.choice([1, 2, 3, 3, 4, 5])):
            t = rng.uniform(0, 9 * 3600)
            conv += 1
            for turn in range(rng.randint(1, 4)):
                calls.append(Call(t, user, conv, turn))
                t += rng.uniform(20, 120)
    return sorted(calls, key=lambda c: c.at)


def simulate(layout: str, cache_tokens: int, args) -> dict:
    rng = random.Random(args.seed)  # same traffic for every layout and cache size
    settings = Settings()
    registry = build_registry(settings, MockProductAPI())
    if layout == "context-in-system":
        template = load_prompt("system_v1")
        tools = [
            {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
            for t in registry._tools.values()
            if t.read_only
        ]  # full schemas, as sent before
    else:
        template = load_prompt(settings.prompt_version)
        tools = registry.schemas(include_write=False)
    users = [
        (rng.randrange(args.orgs), f"User {i} {rng.choice(['Sharma', 'Taylor', 'Mehta', 'Chen'])}", rng.choice(ROLES))
        for i in range(args.users)
    ]
    orgs = [rng.choice(TIMEZONES) for _ in range(args.orgs)]
    histories: dict[int, list[dict]] = {}
    cache = PrefixCache(cache_tokens)
    r = Result()
    for call in schedule(args, rng):
        org, name, role = users[call.user]
        tz, cur = orgs[org]
        ctx = PromptContext("2026-11-23", tz, cur, role, name)
        question = QUESTION.format(
            m=rng.choice(["CPA", "ROAS", "CTR"]),
            c=rng.randrange(40),
            p=rng.choice(["last week", "this month", "the last 30 days"]),
        )
        history = histories.setdefault(call.conv, [])
        messages = build_messages(template, ctx, history, question)
        steps = [messages]
        if rng.random() < args.tool_ratio:  # one tool call, then the answer
            result = json.dumps(
                {
                    "rows": [
                        {
                            "campaign_id": f"cmp_{rng.randrange(999)}",
                            "spend": rng.random() * 1e5,
                            "conversions": rng.randrange(900),
                        }
                        for _ in range(args.tool_rows)
                    ]
                }
            )
            steps.append(
                messages
                + [
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "c0",
                                "type": "function",
                                "function": {"name": "get_campaign_performance", "arguments": "{}"},
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": "c0", "content": result},
                ]
            )
        answer = "x" * rng.randint(args.answer_chars // 2, args.answer_chars * 3 // 2)
        for prompt in steps:
            tokens, cached = cache.lookup_and_store(render(prompt, tools))
            r.prompt += tokens
            r.cached += cached
            r.completion += 20 if prompt is not steps[-1] else len(answer) // CHARS_PER_TOKEN
        r.messages += 1
        r.billable += (len(question) + len(answer)) // CHARS_PER_TOKEN
        history += [{"role": "user", "content": question}, {"role": "assistant", "content": answer}]
    host = r.prompt + r.completion
    return {
        "layout": layout,
        "cache_tokens": cache_tokens,
        "messages": r.messages,
        "prompt_tokens_per_message": round(r.prompt / r.messages),
        "cache_hit_pct": round(r.cached / r.prompt * 100, 1),
        "uncached_host_tokens_per_message": round((host - r.cached) / r.messages),
        "billable_tokens_per_message": round(r.billable / r.messages),
        "overhead_ratio": round(host / r.billable, 1),
        "uncached_overhead_ratio": round((host - r.cached) / r.billable, 1),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--users", type=int, default=2000)
    ap.add_argument("--orgs", type=int, default=300)
    ap.add_argument("--cache-tokens", type=int, nargs="+", default=[100_000, 300_000, 1_000_000])
    ap.add_argument("--tool-ratio", type=float, default=0.6, help="share of messages that call one data tool")
    ap.add_argument("--tool-rows", type=int, default=8, help="rows in each tool result")
    ap.add_argument("--answer-chars", type=int, default=320)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    rows = [
        simulate(layout, size, args) for size in args.cache_tokens for layout in ("context-in-system", "static-prefix")
    ]
    print(json.dumps(rows, indent=1))


if __name__ == "__main__":
    main()

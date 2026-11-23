"""Stand-in OpenAI-compatible model host for load tests and game days (never for real traffic).

  python ops/fake_model_host.py --port 8003 --ttft-ms 150 --chunks 20 --chunk-ms 25 --tool-ratio 0.5 --fail-rate 0

Streams a reply with realistic timing, sometimes calls a read tool first, and can inject failures
(``--fail-rate``) or stall (``--stall-ms``) to rehearse incidents.

Token usage is estimated from the prompt as a chat template lays it out (system prompt, then tool schemas, then the
conversation), at ~4 characters per token. Like vLLM's automatic prefix caching, it keeps recently seen prompts as
hashed blocks of 16 tokens (``ops/prefix_cache.py``) and reports the leading blocks it has seen before as
``usage.prompt_tokens_details.cached_tokens``. Turn that off with ``--no-prefix-cache``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random

import uvicorn
from prefix_cache import PrefixCache, render  # ops/prefix_cache.py (this script's folder is on sys.path)
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

ARGS = argparse.Namespace()
CACHE = PrefixCache(capacity_tokens=800_000)  # resized from --cache-tokens at start-up
STATS = {"requests": 0, "prompt_tokens": 0, "cached_tokens": 0, "completion_tokens": 0}


def _chunk(delta: dict | None = None, finish: str | None = None, usage: dict | None = None) -> str:
    body: dict = {"choices": [] if delta is None else [{"delta": delta, "finish_reason": finish}]}
    if usage:
        body["usage"] = usage
    return f"data: {json.dumps(body)}\n\n"


async def completions(request: Request):
    body = await request.json()
    if random.random() < ARGS.fail_rate:
        return JSONResponse({"error": "injected failure"}, status_code=503)
    has_tool_result = any(m["role"] == "tool" for m in body["messages"])
    wants_tool = bool(body.get("tools")) and not has_tool_result and random.random() < ARGS.tool_ratio
    prompt_tokens, cached = CACHE.lookup_and_store(render(body["messages"], body.get("tools")))
    STATS["requests"] += 1
    STATS["prompt_tokens"] += prompt_tokens
    STATS["cached_tokens"] += cached
    details = {"prompt_tokens_details": {"cached_tokens": cached}}

    async def stream():
        await asyncio.sleep((ARGS.ttft_ms + ARGS.stall_ms) / 1000)
        if wants_tool:
            call = {
                "index": 0,
                "id": "call_0",
                "function": {"name": "list_campaigns", "arguments": json.dumps({"status": "active"})},
            }
            yield _chunk({"tool_calls": [call]}, "tool_calls")
            STATS["completion_tokens"] += 20
            yield _chunk(usage={"prompt_tokens": prompt_tokens, "completion_tokens": 20, **details})
        else:
            words = ("Here is the campaign summary you asked for, based on your latest data. " * 3).split()
            per = max(1, len(words) // ARGS.chunks)
            for i in range(0, len(words), per):
                yield _chunk({"content": " ".join(words[i : i + per]) + " "})
                await asyncio.sleep(ARGS.chunk_ms / 1000)
            yield _chunk({}, "stop")
            STATS["completion_tokens"] += len(words)
            yield _chunk(usage={"prompt_tokens": prompt_tokens, "completion_tokens": len(words), **details})
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


async def stats(request: Request):
    """Totals since start (or since the last ``POST /stats/reset``), e.g. to compare prompt layouts."""
    if request.method == "POST":
        STATS.update(dict.fromkeys(STATS, 0))
        CACHE.clear()
    return JSONResponse(STATS)


app = Starlette(
    routes=[
        Route("/v1/chat/completions", completions, methods=["POST"]),
        Route("/stats", stats, methods=["GET"]),
        Route("/stats/reset", stats, methods=["POST"]),
    ]
)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8003)
    ap.add_argument("--ttft-ms", type=int, default=150)
    ap.add_argument("--chunks", type=int, default=20)
    ap.add_argument("--chunk-ms", type=int, default=25)
    ap.add_argument("--tool-ratio", type=float, default=0.5)
    ap.add_argument("--fail-rate", type=float, default=0.0)
    ap.add_argument("--stall-ms", type=int, default=0)
    ap.add_argument("--no-prefix-cache", action="store_true", help="report no cache hits (caching off)")
    ap.add_argument("--cache-tokens", type=int, default=800_000, help="prefix cache size in tokens")
    ARGS = ap.parse_args()
    CACHE = PrefixCache(ARGS.cache_tokens, enabled=not ARGS.no_prefix_cache)
    uvicorn.run(app, host="127.0.0.1", port=ARGS.port, log_level="warning")

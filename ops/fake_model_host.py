"""Stand-in OpenAI-compatible model host for load tests and game days (never for real traffic).

  python ops/fake_model_host.py --port 8003 --ttft-ms 150 --chunks 20 --chunk-ms 25 --tool-ratio 0.5 --fail-rate 0

Streams a reply with realistic timing, sometimes calls a read tool first, and can inject failures
(``--fail-rate``) or stall (``--stall-ms``) to rehearse incidents.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

ARGS = argparse.Namespace()


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
    prompt_tokens = sum(len(str(m.get("content") or "")) for m in body["messages"]) // 4 + 600

    async def stream():
        await asyncio.sleep((ARGS.ttft_ms + ARGS.stall_ms) / 1000)
        if wants_tool:
            call = {
                "index": 0,
                "id": "call_0",
                "function": {"name": "list_campaigns", "arguments": json.dumps({"status": "active"})},
            }
            yield _chunk({"tool_calls": [call]}, "tool_calls")
            yield _chunk(usage={"prompt_tokens": prompt_tokens, "completion_tokens": 20})
        else:
            words = ("Here is the campaign summary you asked for, based on your latest data. " * 3).split()
            per = max(1, len(words) // ARGS.chunks)
            for i in range(0, len(words), per):
                yield _chunk({"content": " ".join(words[i : i + per]) + " "})
                await asyncio.sleep(ARGS.chunk_ms / 1000)
            yield _chunk({}, "stop")
            yield _chunk(usage={"prompt_tokens": prompt_tokens, "completion_tokens": len(words)})
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


app = Starlette(routes=[Route("/v1/chat/completions", completions, methods=["POST"])])

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8003)
    ap.add_argument("--ttft-ms", type=int, default=150)
    ap.add_argument("--chunks", type=int, default=20)
    ap.add_argument("--chunk-ms", type=int, default=25)
    ap.add_argument("--tool-ratio", type=float, default=0.5)
    ap.add_argument("--fail-rate", type=float, default=0.0)
    ap.add_argument("--stall-ms", type=int, default=0)
    ARGS = ap.parse_args()
    uvicorn.run(app, host="127.0.0.1", port=ARGS.port, log_level="warning")

"""A small OpenAI-compatible model server for running the evals against a real (small) open model on a laptop or CI
runner, with no GPU and no model host. Not for production traffic.

  .venv-ml/bin/python ops/local_model_server.py --model Qwen/Qwen2.5-1.5B-Instruct --port 8010

- ``POST /v1/chat/completions`` with ``stream: true``: applies the model's own chat template (with ``tools``), parses
  ``<tool_call>`` blocks into OpenAI ``tool_calls``, and reports usage including
  ``prompt_tokens_details.cached_tokens``.
- Decoding is greedy, so runs are repeatable; ``temperature`` is ignored.
- Like a production host's prefix cache, it keeps the attention cache of the previous prompt and reuses the longest
  shared opening, so the reported cached tokens are real and use the model's real tokenizer.
- Loads weights only (safetensors); never runs code from the model repository.
- One request at a time (a lock), which is enough for the evals. While generating, it sends SSE keep-alive comments
  every 10 seconds so slow CPUs don't trip the client's read timeout.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

TOOL_CALL = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)


def keep_first(cache, tokens: int) -> None:
    """Trim an attention cache to its first ``tokens`` positions (transformers 5 crops by a negative count)."""
    extra = cache.get_seq_length() - tokens
    if extra > 0:
        cache.crop(-extra)


class Model:
    def __init__(self, name: str, max_new_tokens: int) -> None:
        self.tok = AutoTokenizer.from_pretrained(name)
        self.model = AutoModelForCausalLM.from_pretrained(name, dtype=torch.bfloat16)
        self.model.eval()
        self.max_new_tokens = max_new_tokens
        self.lock = threading.Lock()
        self.cached_ids: list[int] = []
        self.cache = None

    @staticmethod
    def _normalise(messages: list[dict]) -> list[dict]:
        out = []
        for m in messages:
            m = dict(m)
            m["content"] = m.get("content") or ""
            if m.get("tool_calls"):
                calls = []
                for c in m["tool_calls"]:
                    args = c["function"].get("arguments") or "{}"
                    calls.append(
                        {
                            **c,
                            "function": {
                                **c["function"],
                                "arguments": json.loads(args) if isinstance(args, str) else args,
                            },
                        }
                    )
                m["tool_calls"] = calls
            out.append(m)
        return out

    def complete(self, messages: list[dict], tools: list[dict] | None, max_tokens: int) -> dict:
        text = self.tok.apply_chat_template(
            self._normalise(messages), tools=tools or None, add_generation_prompt=True, tokenize=False
        )
        ids = self.tok(text, return_tensors="pt")["input_ids"]
        prompt = ids[0].tolist()
        with self.lock, torch.inference_mode():
            shared = 0
            for a, b in zip(self.cached_ids, prompt, strict=False):
                if a != b:
                    break
                shared += 1
            shared = min(shared, len(prompt) - 1)  # at least one token must be computed
            past = None
            if shared and self.cache is not None:
                past = copy.deepcopy(self.cache)
                keep_first(past, shared)
            out = self.model.generate(
                ids,
                attention_mask=torch.ones_like(ids),
                past_key_values=past,
                max_new_tokens=min(max_tokens, self.max_new_tokens),
                do_sample=False,
                return_dict_in_generate=True,
                pad_token_id=self.tok.eos_token_id,
            )
            cache = out.past_key_values
            keep_first(cache, len(prompt))  # keep the prompt part for the next request
            self.cache, self.cached_ids = cache, prompt
        generated = out.sequences[0, len(prompt) :]
        reply = self.tok.decode(generated, skip_special_tokens=True)
        calls = []
        for i, raw in enumerate(TOOL_CALL.findall(reply)):
            try:
                call = json.loads(raw)
                calls.append(
                    {
                        "index": i,
                        "id": f"call_{uuid.uuid4().hex[:8]}",
                        "type": "function",
                        "function": {"name": call["name"], "arguments": json.dumps(call.get("arguments", {}))},
                    }
                )
            except (ValueError, KeyError):
                continue
        content = TOOL_CALL.sub("", reply).strip() if calls else reply.strip()
        return {
            "content": content,
            "tool_calls": calls,
            "usage": {
                "prompt_tokens": len(prompt),
                "completion_tokens": int(generated.shape[0]),
                "prompt_tokens_details": {"cached_tokens": shared},
            },
        }


def handler_for(model: Model, name: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # quiet
            pass

        def do_GET(self):
            body = json.dumps({"data": [{"id": name, "object": "model"}]}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if not self.path.endswith("/chat/completions"):
                self.send_error(404)
                return
            req = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
            started = time.monotonic()
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            # Generate in a thread and send SSE keep-alive comments meanwhile, so a slow CPU doesn't trip the
            # client's read timeout (a real host streams tokens as it goes).
            result: dict = {}
            worker = threading.Thread(
                target=lambda: result.update(
                    res=model.complete(req["messages"], req.get("tools"), int(req.get("max_tokens") or 512))
                )
            )
            worker.start()
            while worker.is_alive():
                worker.join(timeout=10)
                if worker.is_alive():
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
            res = result["res"]

            def send(payload: dict) -> None:
                self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
                self.wfile.flush()

            if res["tool_calls"]:
                if res["content"]:
                    send({"choices": [{"index": 0, "delta": {"content": res["content"]}}]})
                send(
                    {
                        "choices": [
                            {"index": 0, "delta": {"tool_calls": res["tool_calls"]}, "finish_reason": "tool_calls"}
                        ]
                    }
                )
            else:
                words = re.findall(r"\S+\s*", res["content"])
                for i in range(0, len(words), 8):
                    send({"choices": [{"index": 0, "delta": {"content": "".join(words[i : i + 8])}}]})
                send({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            send({"choices": [], "usage": res["usage"]})
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            u = res["usage"]
            print(
                f"{time.monotonic() - started:5.1f}s prompt={u['prompt_tokens']} "
                f"cached={u['prompt_tokens_details']['cached_tokens']} completion={u['completion_tokens']} "
                f"tools={[c['function']['name'] for c in res['tool_calls']]}",
                flush=True,
            )

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="Qwen/Qwen2.5-1.5B-Instruct")
    ap.add_argument("--port", type=int, default=8010)
    ap.add_argument("--max-new-tokens", type=int, default=400)
    args = ap.parse_args()
    model = Model(args.model, args.max_new_tokens)
    print(f"serving {args.model} on http://127.0.0.1:{args.port}/v1", flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(model, args.model)).serve_forever()


if __name__ == "__main__":
    main()

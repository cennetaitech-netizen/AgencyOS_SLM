"""A small model of a model host's automatic prefix cache (as in vLLM), shared by the stand-in model host and the
prompt-cache simulation. Prompts are split into blocks of 16 tokens (~4 characters each) and hashed in a chain, so
a block is reused only when every block before it matched too. Least recently used blocks are evicted first."""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict

CHARS_PER_TOKEN = 4
BLOCK_TOKENS = 16


def render(messages: list[dict], tools: list[dict] | None) -> str:
    """The prompt roughly as a chat template renders it: system message, tool schemas, then the rest."""
    head = messages[:1] if messages and messages[0]["role"] == "system" else []
    parts = [f"<system>{head[0]['content']}" if head else "", json.dumps(tools or [])]
    for m in messages[len(head) :]:
        calls = json.dumps(m["tool_calls"]) if m.get("tool_calls") else ""
        parts.append(f"<{m['role']}>{m.get('content') or ''}{calls}")
    return "".join(parts)


class PrefixCache:
    def __init__(self, capacity_tokens: int, enabled: bool = True) -> None:
        self.capacity_blocks = max(1, capacity_tokens // BLOCK_TOKENS)
        self.enabled = enabled
        self.blocks: OrderedDict[bytes, None] = OrderedDict()

    def lookup_and_store(self, prompt: str) -> tuple[int, int]:
        """Returns (prompt tokens, tokens served from the cache), then caches the prompt's blocks."""
        tokens = max(1, len(prompt) // CHARS_PER_TOKEN)
        size = BLOCK_TOKENS * CHARS_PER_TOKEN
        h, hits, matching = b"", 0, True
        for i in range(0, len(prompt) - size + 1, size):
            h = hashlib.blake2b(h + prompt[i : i + size].encode(), digest_size=16).digest()
            if matching and h in self.blocks:
                hits += 1
                self.blocks.move_to_end(h)
            else:
                matching = False
                self.blocks[h] = None
                if len(self.blocks) > self.capacity_blocks:
                    self.blocks.popitem(last=False)
        if not self.enabled:
            return tokens, 0
        return tokens, min(hits * BLOCK_TOKENS, tokens - 1)  # the last token is always computed

    def clear(self) -> None:
        self.blocks.clear()

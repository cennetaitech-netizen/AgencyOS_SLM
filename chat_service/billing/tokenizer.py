"""Billing tokenizer: a fixed, versioned way to count billable tokens.

Billing never depends on the serving model's own tokenizer (see CHAT_ENGINE_SLM_PLAN.md §3.3).
Production uses a pinned ``tokenizer.json`` file (Hugging Face ``tokenizers`` format) committed to the repo.
The ``dev-regex-v0`` counter is deterministic but only for development and tests. It's refused in production.
"""

from __future__ import annotations

import re
from typing import Protocol

from chat_service.config import Settings


class BillingTokenizer(Protocol):
    version: str

    def count(self, text: str) -> int: ...


class DevRegexTokenizer:
    """Deterministic approximation: words and punctuation, long words split into 4-character pieces."""

    version = "dev-regex-v0"
    _pattern = re.compile(r"\w+|[^\w\s]", re.UNICODE)

    def count(self, text: str) -> int:
        if not text:
            return 0
        total = 0
        for piece in self._pattern.findall(text):
            total += max(1, -(-len(piece) // 4)) if len(piece) > 4 else 1
        return total


class FileTokenizer:
    """Pinned Hugging Face tokenizer file. Deterministic, offline, fast."""

    def __init__(self, path: str, version: str) -> None:
        from tokenizers import Tokenizer  # imported lazily: heavy

        self._tok = Tokenizer.from_file(path)
        self.version = version

    def count(self, text: str) -> int:
        if not text:
            return 0
        return len(self._tok.encode(text, add_special_tokens=False).ids)


def build_tokenizer(settings: Settings) -> BillingTokenizer:
    if settings.billing_tokenizer_path:
        return FileTokenizer(settings.billing_tokenizer_path, settings.billing_tokenizer_version)
    if settings.env == "production":
        raise RuntimeError(
            "CHAT_BILLING_TOKENIZER_PATH must point to the pinned billing tokenizer file in production"
        )
    return DevRegexTokenizer()

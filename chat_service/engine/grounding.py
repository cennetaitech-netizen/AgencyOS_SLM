"""Numeric grounding: every figure in an answer must come from the data the tools returned.

Used two ways:
* at runtime, to flag answers containing unverifiable numbers (stored in ``chat_messages.flags``, shown in the
  weekly quality review, and returned in ``message.end.quality_flags``)
* in the eval harness, where any ungrounded number fails the case (target: 100% grounded)

A number counts as grounded if it equals a number from the sources, or equals one rounded to the answer's
precision (e.g. 12.5 for 12.4987). Dates, small counts (≤ 10) and numbers in the user's own message are ignored.
"""

from __future__ import annotations

import json
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?\b")
_NUM = re.compile(r"(?<![\w.])\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|(?<![\w.])\d+(?:\.\d+)?")


def _numbers(text: str) -> list[str]:
    text = _DATE.sub(" ", text)
    return [m.group(0).replace(",", "") for m in _NUM.finditer(text)]


def _dec(s: str) -> Decimal | None:
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _flatten(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, default=str)


_ISO_PARTS = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})")


def _source_numbers(text: str) -> list[str]:
    """Numbers in a source, plus the year/month/day of any ISO date, so an answer can say "15–21 Nov 2026"
    about a date range the tool returned as 2026-11-15 … 2026-11-21."""
    parts = [str(int(x)) for m in _ISO_PARTS.finditer(text) for x in m.groups()]
    return _numbers(text) + parts


def ungrounded_numbers(answer: str, sources: list[Any], ignore_up_to: int = 10) -> list[str]:
    source_values = {d for s in sources for n in _source_numbers(_flatten(s)) if (d := _dec(n)) is not None}
    missing = []
    for raw in _numbers(answer):
        value = _dec(raw)
        if value is None or (value == value.to_integral_value() and abs(value) <= ignore_up_to):
            continue
        if value in source_values:
            continue
        exp = value.as_tuple().exponent
        places = Decimal(1).scaleb(exp if isinstance(exp, int) else 0)
        if any(src.quantize(places, rounding=ROUND_HALF_UP) == value for src in source_values):
            continue
        missing.append(raw)
    return missing

"""Ad copy validator. Small models can't count characters reliably, so this does it for them."""

from __future__ import annotations

import json
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any

from chat_service.tools.registry import Tool, ToolContext, ToolInputError

_SPECS_PATH = Path(__file__).with_name("ad_specs.json")


@lru_cache
def load_specs() -> dict[str, Any]:
    data = json.loads(_SPECS_PATH.read_text())
    return {k: v for k, v in data.items() if not k.startswith("_")}


def display_length(text: str, platform: str) -> int:
    """Google Ads counts full-width (e.g. CJK) characters as 2. Others count characters."""
    if platform == "google_ads":
        return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)
    return len(text)


def validate(platform: str, ad_format: str, fields: dict[str, list[str]]) -> dict[str, Any]:
    specs = load_specs()
    if platform not in specs:
        raise ToolInputError(f"unknown platform {platform!r}; known: {', '.join(sorted(specs))}")
    if ad_format not in specs[platform]:
        raise ToolInputError(
            f"unknown format {ad_format!r} for {platform}; known: {', '.join(sorted(specs[platform]))}"
        )
    limits = specs[platform][ad_format]
    results, all_ok = [], True
    for field_name, texts in fields.items():
        if field_name not in limits:
            raise ToolInputError(
                f"unknown field {field_name!r} for {platform}/{ad_format}; known: {', '.join(sorted(limits))}"
            )
        spec = limits[field_name]
        for text in texts:
            length = display_length(text, platform)
            ok = length <= spec["max"]
            if not ok and spec["kind"] == "hard":
                all_ok = False
            results.append(
                {
                    "field": field_name,
                    "text": text,
                    "length": length,
                    "max": spec["max"],
                    "limit_kind": spec["kind"],
                    "ok": ok,
                    "over_by": max(0, length - spec["max"]),
                }
            )
    return {"platform": platform, "format": ad_format, "all_hard_limits_ok": all_ok, "rows": results}


async def _handler(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return validate(args["platform"], args["format"], args["fields"])


VALIDATE_AD_COPY = Tool(
    name="validate_ad_copy",
    label="Checking ad copy lengths",
    group="builtin",
    description=(
        "Check ad copy against platform character limits before presenting it. "
        "Pass every headline/description you wrote; rewrite any line that fails a hard limit."
    ),
    parameters={
        "type": "object",
        "properties": {
            "platform": {"type": "string", "enum": ["google_ads", "meta"]},
            "format": {"type": "string", "description": "e.g. responsive_search_ad, single_image_or_video"},
            "fields": {
                "type": "object",
                "description": "Field name → list of texts, e.g. {\"headline\": [\"...\"], \"description\": [\"...\"]}",
                "additionalProperties": {"type": "array", "items": {"type": "string"}},
            },
        },
        "required": ["platform", "format", "fields"],
        "additionalProperties": False,
    },
    handler=_handler,
)

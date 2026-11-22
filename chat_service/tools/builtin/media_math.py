"""Deterministic media calculator. The model must use this instead of doing arithmetic itself."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from chat_service.tools.registry import Tool, ToolContext, ToolInputError

OPERATIONS: dict[str, tuple[list[str], str]] = {
    "sum": (["values"], "sum of values"),
    "percent_split": (["total", "shares"], "total × share% for each item"),
    "percent_change": (["old", "new"], "(new − old) ÷ old × 100"),
    "impressions_from_cpm": (["budget", "cpm"], "budget ÷ CPM × 1000"),
    "cpm": (["spend", "impressions"], "spend ÷ impressions × 1000"),
    "clicks_from_cpc": (["budget", "cpc"], "budget ÷ CPC"),
    "cpc": (["spend", "clicks"], "spend ÷ clicks"),
    "ctr": (["clicks", "impressions"], "clicks ÷ impressions × 100"),
    "cpa": (["spend", "conversions"], "spend ÷ conversions"),
    "conversions_from_cpa": (["budget", "cpa"], "budget ÷ CPA"),
    "roas": (["revenue", "spend"], "revenue ÷ spend"),
    "pacing_projection": (
        ["spend_to_date", "days_elapsed", "total_days", "budget"],
        "projected = spend_to_date ÷ days_elapsed × total_days; pacing% = projected ÷ budget × 100",
    ),
}

_Q2 = Decimal("0.01")


def _d(value: Any, name: str) -> Decimal:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ToolInputError(f"{name} must be a number") from exc
    if not d.is_finite():
        raise ToolInputError(f"{name} must be a finite number")
    return d


def _nonzero(d: Decimal, name: str) -> Decimal:
    if d == 0:
        raise ToolInputError(f"{name} can't be zero")
    return d


def _r(d: Decimal, places: Decimal = _Q2) -> float:
    return float(d.quantize(places, rounding=ROUND_HALF_UP))


def calculate(operation: str, values: dict[str, Any]) -> dict[str, Any]:
    if operation not in OPERATIONS:
        raise ToolInputError(f"unknown operation {operation!r}")
    required, formula = OPERATIONS[operation]
    missing = [k for k in required if k not in values]
    if missing:
        raise ToolInputError(f"missing inputs for {operation}: {', '.join(missing)}")

    if operation == "sum":
        items = values["values"]
        if not isinstance(items, list) or not items:
            raise ToolInputError("values must be a non-empty list of numbers")
        result: Any = _r(sum((_d(v, "value") for v in items), Decimal(0)))
    elif operation == "percent_split":
        total = _d(values["total"], "total")
        shares = values["shares"]
        if not isinstance(shares, dict) or not shares:
            raise ToolInputError("shares must be an object of name → percent")
        pct = {k: _d(v, f"share {k}") for k, v in shares.items()}
        pct_total = sum(pct.values(), Decimal(0))
        if pct_total != 100:
            raise ToolInputError(f"shares must add up to 100 (they add up to {pct_total})")
        amounts = {k: (total * p / 100).quantize(_Q2, rounding=ROUND_HALF_UP) for k, p in pct.items()}
        # Put any rounding remainder on the largest share so the split adds up exactly.
        diff = total.quantize(_Q2) - sum(amounts.values(), Decimal(0))
        if diff:
            biggest = max(pct, key=lambda k: pct[k])
            amounts[biggest] += diff
        result = {k: float(v) for k, v in amounts.items()}
    elif operation == "percent_change":
        old, new = _nonzero(_d(values["old"], "old"), "old"), _d(values["new"], "new")
        result = _r((new - old) / old * 100)
    elif operation == "impressions_from_cpm":
        result = int(
            (_d(values["budget"], "budget") / _nonzero(_d(values["cpm"], "cpm"), "cpm") * 1000).to_integral_value(
                rounding=ROUND_HALF_UP
            )
        )
    elif operation == "cpm":
        result = _r(
            _d(values["spend"], "spend") / _nonzero(_d(values["impressions"], "impressions"), "impressions") * 1000
        )
    elif operation == "clicks_from_cpc":
        result = int(
            (_d(values["budget"], "budget") / _nonzero(_d(values["cpc"], "cpc"), "cpc")).to_integral_value(
                rounding=ROUND_HALF_UP
            )
        )
    elif operation == "cpc":
        result = _r(_d(values["spend"], "spend") / _nonzero(_d(values["clicks"], "clicks"), "clicks"))
    elif operation == "ctr":
        result = _r(
            _d(values["clicks"], "clicks") / _nonzero(_d(values["impressions"], "impressions"), "impressions") * 100
        )
    elif operation == "cpa":
        result = _r(_d(values["spend"], "spend") / _nonzero(_d(values["conversions"], "conversions"), "conversions"))
    elif operation == "conversions_from_cpa":
        result = _r(_d(values["budget"], "budget") / _nonzero(_d(values["cpa"], "cpa"), "cpa"))
    elif operation == "roas":
        result = _r(_d(values["revenue"], "revenue") / _nonzero(_d(values["spend"], "spend"), "spend"))
    else:  # pacing_projection
        spend = _d(values["spend_to_date"], "spend_to_date")
        elapsed = _nonzero(_d(values["days_elapsed"], "days_elapsed"), "days_elapsed")
        total_days = _d(values["total_days"], "total_days")
        budget = _nonzero(_d(values["budget"], "budget"), "budget")
        projected = spend / elapsed * total_days
        result = {"projected_spend": _r(projected), "pacing_percent": _r(projected / budget * 100)}

    return {"operation": operation, "inputs": values, "formula": formula, "result": result}


async def _handler(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return calculate(args["operation"], args.get("values", {}))


MEDIA_MATH = Tool(
    name="media_math",
    label="Calculating",
    group="builtin",
    description=(
        "Exact calculator for media math. Use it for EVERY calculation: sums, budget splits by percent, "
        "percent change, CPM/CPC/CPA/CTR/ROAS, impressions or clicks from a budget, and pacing projections."
    ),
    parameters={
        "type": "object",
        "properties": {
            "operation": {"type": "string", "enum": sorted(OPERATIONS)},
            "values": {
                "type": "object",
                "description": (
                    "Inputs by name. sum: values[]; percent_split: total, shares{name: percent}; "
                    "percent_change: old, new; impressions_from_cpm: budget, cpm; cpm: spend, impressions; "
                    "clicks_from_cpc: budget, cpc; cpc: spend, clicks; ctr: clicks, impressions; "
                    "cpa: spend, conversions; conversions_from_cpa: budget, cpa; roas: revenue, spend; "
                    "pacing_projection: spend_to_date, days_elapsed, total_days, budget"
                ),
            },
        },
        "required": ["operation", "values"],
        "additionalProperties": False,
    },
    handler=_handler,
)

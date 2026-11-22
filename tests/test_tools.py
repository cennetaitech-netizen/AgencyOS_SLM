from __future__ import annotations

import uuid

import pytest

from chat_service.config import Settings
from chat_service.tools import build_registry
from chat_service.tools.builtin.ad_copy import validate
from chat_service.tools.builtin.media_math import calculate
from chat_service.tools.product import MockProductAPI
from chat_service.tools.registry import Tool, ToolContext, ToolInputError, ToolRegistry, ToolRegistryError


def ctx() -> ToolContext:
    return ToolContext(org_id=uuid.uuid4(), user_id=uuid.uuid4(), request_id="t")


# ---------------------------------------------------------------- media_math


@pytest.mark.parametrize(
    ("op", "values", "expected"),
    [
        ("sum", {"values": [0.1, 0.2]}, 0.3),
        ("percent_change", {"old": 200, "new": 250}, 25.0),
        ("impressions_from_cpm", {"budget": 50000, "cpm": 125}, 400000),
        ("cpm", {"spend": 1500, "impressions": 120000}, 12.5),
        ("clicks_from_cpc", {"budget": 10000, "cpc": 12.5}, 800),
        ("cpc", {"spend": 1000, "clicks": 3}, 333.33),
        ("ctr", {"clicks": 25, "impressions": 1000}, 2.5),
        ("cpa", {"spend": 9000, "conversions": 45}, 200.0),
        ("roas", {"revenue": 45000, "spend": 9000}, 5.0),
        ("conversions_from_cpa", {"budget": 10000, "cpa": 300}, 33.33),
    ],
)
def test_media_math_known_answers(op, values, expected):
    assert calculate(op, values)["result"] == expected


def test_percent_split_adds_up_exactly():
    r = calculate("percent_split", {"total": 100000, "shares": {"search": 33.33, "social": 33.33, "video": 33.34}})
    assert round(sum(r["result"].values()), 2) == 100000


def test_percent_split_rejects_shares_not_100():
    with pytest.raises(ToolInputError):
        calculate("percent_split", {"total": 100, "shares": {"a": 50, "b": 40}})


def test_pacing_projection():
    r = calculate("pacing_projection", {"spend_to_date": 4000, "days_elapsed": 10, "total_days": 30, "budget": 10000})
    assert r["result"] == {"projected_spend": 12000.0, "pacing_percent": 120.0}


@pytest.mark.parametrize(
    ("op", "values"), [("cpa", {"spend": 10, "conversions": 0}), ("cpm", {"spend": "abc", "impressions": 5})]
)
def test_media_math_bad_inputs(op, values):
    with pytest.raises(ToolInputError):
        calculate(op, values)


# ---------------------------------------------------------------- ad copy


def test_ad_copy_hard_limit_fails():
    r = validate(
        "google_ads",
        "responsive_search_ad",
        {"headline": ["Fresh Diwali Sweets Delivered", "Order Festive Sweet Boxes Online Today"]},
    )
    assert [row["ok"] for row in r["rows"]] == [True, False]
    assert r["rows"][1]["over_by"] == len("Order Festive Sweet Boxes Online Today") - 30
    assert r["all_hard_limits_ok"] is False


def test_ad_copy_recommended_limit_does_not_fail_overall():
    r = validate("meta", "single_image_or_video", {"headline": ["x" * 41]})
    assert r["rows"][0]["ok"] is False and r["all_hard_limits_ok"] is True


def test_google_counts_wide_characters_double():
    r = validate("google_ads", "responsive_search_ad", {"headline": ["日" * 16]})
    assert r["rows"][0]["length"] == 32 and not r["rows"][0]["ok"]


def test_ad_copy_unknown_field():
    with pytest.raises(ToolInputError):
        validate("google_ads", "responsive_search_ad", {"body": ["x"]})


# ---------------------------------------------------------------- registry


async def _noop(ctx, args):
    return {"ok": True}


def test_registry_rejects_write_tools_in_read_only_mode():
    reg = ToolRegistry(allow_write=False)
    with pytest.raises(ToolRegistryError):
        reg.register(
            Tool("update_budget", "x", {"type": "object"}, _noop, read_only=False, risk_tier=3, preview_tool="p")
        )


def test_registry_write_tools_need_tier_and_preview():
    reg = ToolRegistry(allow_write=True)
    with pytest.raises(ToolRegistryError):
        reg.register(Tool("update_budget", "x", {"type": "object"}, _noop, read_only=False))


async def test_registry_never_executes_write_tools():
    reg = ToolRegistry(allow_write=True)
    reg.register(Tool("update_budget", "x", {"type": "object"}, _noop, read_only=False, risk_tier=3, preview_tool="p"))
    r = await reg.execute(ctx(), "update_budget", "{}", 5, 1000)
    assert not r.ok and "confirmation flow" in r.error


async def test_registry_truncates_large_results():
    async def big(ctx, args):
        return {"rows": [{"i": i, "pad": "x" * 50} for i in range(200)]}

    reg = ToolRegistry()
    reg.register(Tool("big", "x", {"type": "object"}, big))
    r = await reg.execute(ctx(), "big", "{}", 5, 2000)
    assert r.ok and r.truncated and r.data["truncated"] and r.result_chars <= 2000


async def test_registry_unknown_tool_and_bad_json():
    reg = build_registry(Settings(env="test"), MockProductAPI())
    assert not (await reg.execute(ctx(), "nope", "{}", 5, 1000)).ok
    assert "invalid arguments" in (await reg.execute(ctx(), "list_campaigns", "{bad json", 5, 1000)).error


async def test_mock_product_performance_has_data_as_of_and_currency():
    reg = build_registry(Settings(env="test"), MockProductAPI())
    r = await reg.execute(
        ctx(),
        "get_campaign_performance",
        {"client_id": "cl_acme", "date_from": "2026-11-01", "date_to": "2026-11-07", "breakdown": "platform"},
        5,
        8000,
    )
    assert r.ok and r.data["currency"] == "INR" and r.data["data_as_of"]
    assert {row["platform"] for row in r.data["rows"]} == {"google_ads", "meta"}

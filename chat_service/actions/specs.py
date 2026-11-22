"""Write actions the chat can *propose*. Each one declares its risk tier, preview, before → after, rules and undo.

⚠️ The set of actions and their tiers must be approved by the product owner (Phase 9 mini-checkpoint)
once the real write inventory is known. These four are the launch candidates from the combined plan.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

CAMPAIGN_STATE = ("name", "status", "budget")


@dataclass(frozen=True)
class ActionSpec:
    name: str
    label: str
    description: str
    parameters: dict[str, Any]
    risk_tier: int
    reversible: bool
    after: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]
    validate: Callable[[dict[str, Any], dict[str, Any]], list[str]]
    summary: Callable[[dict[str, Any], dict[str, Any]], str]
    undo: Callable[[dict[str, Any], dict[str, Any]], tuple[str, dict[str, Any]]] | None = None
    preview_tool: str = "get_campaign_details"
    state_fields: tuple[str, ...] = CAMPAIGN_STATE
    changes_spend: bool = False

    def target_id(self, args: dict[str, Any]) -> str:
        return str(args["campaign_id"])

    def preview_args(self, args: dict[str, Any]) -> dict[str, Any]:
        return {"campaign_id": args["campaign_id"]}

    def state(self, record: dict[str, Any]) -> dict[str, Any]:
        return {k: record.get(k) for k in self.state_fields}

    def spend_change_minor(self, before: dict[str, Any], after: dict[str, Any]) -> int:
        """Budget delta in minor units (assumes a 2-decimal currency; adjust per currency in Phase 0)."""
        if not self.changes_spend:
            return 0
        return round((float(after["budget"]) - float(before["budget"])) * 100)


def _fmt(n: Any) -> str:
    return f"{float(n):,.2f}".rstrip("0").rstrip(".")


def _ended(before: dict[str, Any]) -> list[str]:
    return ["campaign has ended"] if before.get("status") == "ended" else []


_CID = {"campaign_id": {"type": "string", "description": "Campaign ID from list_campaigns"}}

UPDATE_BUDGET = ActionSpec(
    name="update_campaign_budget",
    label="Change campaign budget",
    description=(
        "PROPOSE a new total budget for a campaign. Nothing changes until the user confirms the card. "
        "Get the current budget with get_campaign_details first; use media_math for any calculation."
    ),
    parameters={
        "type": "object",
        "properties": {**_CID, "new_budget": {"type": "number", "exclusiveMinimum": 0}},
        "required": ["campaign_id", "new_budget"],
        "additionalProperties": False,
    },
    risk_tier=3,
    reversible=True,
    changes_spend=True,
    after=lambda before, args: {**{k: before.get(k) for k in CAMPAIGN_STATE}, "budget": args["new_budget"]},
    validate=lambda before, args: (
        _ended(before)
        + (
            ["new budget is the same as the current budget"]
            if float(args["new_budget"]) == float(before["budget"])
            else []
        )
    ),
    summary=lambda b, a: f"Budget for {b['name']} changed from {_fmt(b['budget'])} to {_fmt(a['budget'])}",
    undo=lambda before, args: (
        "update_campaign_budget",
        {"campaign_id": args["campaign_id"], "new_budget": before["budget"]},
    ),
)

PAUSE = ActionSpec(
    name="pause_campaign",
    label="Pause campaign",
    description="PROPOSE pausing an active campaign. Nothing changes until the user confirms the card.",
    parameters={"type": "object", "properties": _CID, "required": ["campaign_id"], "additionalProperties": False},
    risk_tier=2,
    reversible=True,
    after=lambda before, args: {**{k: before.get(k) for k in CAMPAIGN_STATE}, "status": "paused"},
    validate=lambda before, args: (
        _ended(before) + (["campaign is already paused"] if before.get("status") == "paused" else [])
    ),
    summary=lambda b, a: f"{b['name']} paused",
    undo=lambda before, args: ("resume_campaign", {"campaign_id": args["campaign_id"]}),
)

RESUME = ActionSpec(
    name="resume_campaign",
    label="Resume campaign",
    description="PROPOSE resuming a paused campaign. Nothing changes until the user confirms the card.",
    parameters={"type": "object", "properties": _CID, "required": ["campaign_id"], "additionalProperties": False},
    risk_tier=2,
    reversible=True,
    after=lambda before, args: {**{k: before.get(k) for k in CAMPAIGN_STATE}, "status": "active"},
    validate=lambda before, args: (
        _ended(before) + (["campaign is already active"] if before.get("status") == "active" else [])
    ),
    summary=lambda b, a: f"{b['name']} resumed",
    undo=lambda before, args: ("pause_campaign", {"campaign_id": args["campaign_id"]}),
)

RENAME = ActionSpec(
    name="rename_campaign",
    label="Rename campaign",
    description="PROPOSE a new name for a campaign. Nothing changes until the user confirms the card.",
    parameters={
        "type": "object",
        "properties": {**_CID, "new_name": {"type": "string", "minLength": 3, "maxLength": 120}},
        "required": ["campaign_id", "new_name"],
        "additionalProperties": False,
    },
    risk_tier=1,
    reversible=True,
    after=lambda before, args: {**{k: before.get(k) for k in CAMPAIGN_STATE}, "name": args["new_name"]},
    validate=lambda before, args: ["name is unchanged"] if args["new_name"] == before.get("name") else [],
    summary=lambda b, a: f"Campaign renamed from “{b['name']}” to “{a['name']}”",
    undo=lambda before, args: ("rename_campaign", {"campaign_id": args["campaign_id"], "new_name": before["name"]}),
)

ACTION_SPECS: dict[str, ActionSpec] = {s.name: s for s in (UPDATE_BUDGET, PAUSE, RESUME, RENAME)}

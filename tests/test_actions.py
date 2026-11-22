from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import update

from chat_service.actions.service import ActionError, ActionService
from chat_service.billing.caps import LogNotifier
from chat_service.db.schema import chat_actions, chat_plans, chat_subscriptions
from chat_service.engine.store import OrgSettingsStore
from chat_service.principal import Principal
from chat_service.tools.product import MockProductAPI


@pytest.fixture
def org():
    return uuid.uuid4()


@pytest.fixture
def planner(org):
    return Principal(uuid.uuid4(), org, "Priya", ("planner",), "tok-priya")


@pytest.fixture
def admin(org):
    return Principal(uuid.uuid4(), org, "Asha", ("org_admin",), "tok-asha")


@pytest.fixture
def product():
    return MockProductAPI()


@pytest.fixture
async def actions(engine, settings, clock, product, org):
    settings.write_actions_enabled_global = True
    notifier = LogNotifier()
    svc = ActionService(engine, settings, product, orgs=OrgSettingsStore(engine), notifier=notifier, clock=clock)
    await svc.set_rules(org, write_actions_enabled=True)
    svc._test_notifier = notifier
    return svc


async def propose(actions, who, tool, args):
    ctx = await actions.ctx_for(who, "req")
    return await actions.propose(who, ctx, uuid.uuid4(), uuid.uuid4(), tool, args)


def budget(product, cid="cmp_101"):
    return next(c for c in product.campaigns if c["campaign_id"] == cid)["budget"]


# ---------------------------------------------------------------- propose → confirm


async def test_proposal_changes_nothing_until_confirmed(actions, planner, product):
    r = await propose(actions, planner, "update_campaign_budget", {"campaign_id": "cmp_101", "new_budget": 330000})
    card = r["card"]
    assert r["for_model"]["status"] == "awaiting_user_confirmation"
    assert card["before"]["budget"] == 300000 and card["after"]["budget"] == 330000
    assert card["risk_tier"] == 3 and "large change (10.0%)" in card["warnings"]
    assert budget(product) == 300000  # nothing changed yet

    view = await actions.confirm(planner, uuid.UUID(card["action_id"]))
    assert view["status"] == "executed" and budget(product) == 330000
    assert view["summary"] == "Done: Budget for Acme Diwali Search changed from 300,000 to 330,000"
    assert view["undo_available"] is True


async def test_double_confirm_executes_once(actions, planner, product):
    card = (await propose(actions, planner, "pause_campaign", {"campaign_id": "cmp_101"}))["card"]
    aid = uuid.UUID(card["action_id"])
    results = await asyncio.gather(*(actions.confirm(planner, aid) for _ in range(5)), return_exceptions=True)
    assert all(not isinstance(r, Exception) for r in results)
    assert len(product.write_log) == 1
    assert (await actions.get(planner.org_id, aid))["status"] == "executed"


async def test_only_requester_or_approver_can_confirm(actions, planner, product, org):
    card = (await propose(actions, planner, "pause_campaign", {"campaign_id": "cmp_101"}))["card"]
    stranger = Principal(uuid.uuid4(), org, "X", ("planner",))
    with pytest.raises(ActionError) as exc:
        await actions.confirm(stranger, uuid.UUID(card["action_id"]))
    assert exc.value.code == "forbidden"
    other_org = Principal(uuid.uuid4(), uuid.uuid4(), "Y", ("org_admin",))
    with pytest.raises(ActionError):
        await actions.confirm(other_org, uuid.UUID(card["action_id"]))


# ---------------------------------------------------------------- safety checks


async def test_change_above_max_percent_is_rejected(actions, planner, product):
    r = await propose(actions, planner, "update_campaign_budget", {"campaign_id": "cmp_101", "new_budget": 400000})
    assert r["card"] is None and r["for_model"]["status"] == "rejected"
    assert "above your organisation's limit of 20%" in r["for_model"]["reason"]


async def test_invalid_args_and_unknown_campaign_rejected(actions, planner):
    r1 = await propose(actions, planner, "update_campaign_budget", {"campaign_id": "cmp_101", "new_budget": -5})
    r2 = await propose(actions, planner, "pause_campaign", {"campaign_id": "nope"})
    r3 = await propose(actions, planner, "resume_campaign", {"campaign_id": "cmp_101"})  # already active
    assert "invalid arguments" in r1["for_model"]["reason"]
    assert "not found" in r2["for_model"]["reason"]
    assert "already active" in r3["for_model"]["reason"]


async def test_stale_preview_is_rejected_at_confirm(actions, planner, product):
    card = (
        await propose(actions, planner, "update_campaign_budget", {"campaign_id": "cmp_101", "new_budget": 320000})
    )["card"]
    product.campaigns[0]["budget"] = 310000  # someone changed it in the product meanwhile
    with pytest.raises(ActionError) as exc:
        await actions.confirm(planner, uuid.UUID(card["action_id"]))
    assert exc.value.code == "stale" and budget(product) == 310000
    assert (await actions.get(planner.org_id, uuid.UUID(card["action_id"])))["status"] == "cancelled"


async def test_expired_proposal_cannot_be_confirmed(actions, planner, clock, product):
    card = (await propose(actions, planner, "pause_campaign", {"campaign_id": "cmp_101"}))["card"]
    clock.advance(minutes=11)
    with pytest.raises(ActionError) as exc:
        await actions.confirm(planner, uuid.UUID(card["action_id"]))
    assert exc.value.code == "expired" and product.write_log == {}


async def test_second_approval_flow(actions, planner, admin, org, product):
    await actions.set_rules(org, second_approval_above_minor=1_000_000)  # 10,000.00
    card = (
        await propose(actions, planner, "update_campaign_budget", {"campaign_id": "cmp_101", "new_budget": 330000})
    )["card"]
    aid = uuid.UUID(card["action_id"])
    assert card["requires_second_approval"]
    view = await actions.confirm(planner, aid)
    assert view["status"] == "awaiting_second_approval" and budget(product) == 300000
    assert actions._test_notifier.sent[-1][1]["type"] == "approval_needed"
    with pytest.raises(ActionError):
        await actions.approve(planner, aid)  # not an approver
    planner_admin = Principal(planner.user_id, org, "Priya", ("org_admin",))
    with pytest.raises(ActionError):
        await actions.approve(planner_admin, aid)  # same person can't be the second approver
    view = await actions.approve(admin, aid)
    assert view["status"] == "executed" and view["second_approved_by"] == str(admin.user_id)
    assert budget(product) == 330000


async def test_approver_can_reject(actions, planner, admin, org, product):
    await actions.set_rules(org, second_approval_above_minor=0)
    card = (
        await propose(actions, planner, "update_campaign_budget", {"campaign_id": "cmp_101", "new_budget": 310000})
    )["card"]
    aid = uuid.UUID(card["action_id"])
    await actions.confirm(planner, aid)
    assert (await actions.reject(admin, aid, "not this week"))["status"] == "rejected"
    assert budget(product) == 300000


async def test_daily_spend_change_cap(actions, planner, org, product):
    await actions.set_rules(org, daily_spend_change_cap_minor=5_000_000)  # 50,000.00 per day
    c1 = (await propose(actions, planner, "update_campaign_budget", {"campaign_id": "cmp_101", "new_budget": 330000}))[
        "card"
    ]
    await actions.confirm(planner, uuid.UUID(c1["action_id"]))
    r = await propose(actions, planner, "update_campaign_budget", {"campaign_id": "cmp_102", "new_budget": 275000})
    assert r["card"] is None and "today's limit" in r["for_model"]["reason"]


async def test_role_restriction_and_blocked_tools(actions, planner, admin, org):
    await actions.set_rules(org, roles_by_tier={"3": ["org_admin"]}, blocked_tools=["rename_campaign"])
    r = await propose(actions, planner, "update_campaign_budget", {"campaign_id": "cmp_101", "new_budget": 310000})
    assert "role" in r["for_model"]["reason"]
    assert (await propose(actions, admin, "update_campaign_budget", {"campaign_id": "cmp_101", "new_budget": 310000}))[
        "card"
    ] is not None
    r = await propose(actions, admin, "rename_campaign", {"campaign_id": "cmp_101", "new_name": "New name"})
    assert "blocked" in r["for_model"]["reason"]


async def test_kill_switches_and_past_due_make_chat_read_only(actions, planner, org, settings, engine):
    settings.write_actions_enabled_global = False
    assert (
        "aren't enabled"
        in (await propose(actions, planner, "pause_campaign", {"campaign_id": "cmp_101"}))["for_model"]["reason"]
    )
    settings.write_actions_enabled_global = True
    await actions.set_rules(org, write_actions_enabled=False)
    assert not await actions.writes_enabled(org)
    await actions.set_rules(org, write_actions_enabled=True)
    async with engine.begin() as conn:
        await conn.execute(chat_plans.insert().values(plan_id="p", name="P", currency="INR"))
        await conn.execute(
            chat_subscriptions.insert().values(
                org_id=org,
                plan_id="p",
                current_period_start=actions.clock(),
                current_period_end=actions.clock(),
                status="past_due",
            )
        )
    assert not await actions.writes_enabled(org)


async def test_writes_turned_off_between_propose_and_confirm(actions, planner, org, product):
    card = (await propose(actions, planner, "pause_campaign", {"campaign_id": "cmp_101"}))["card"]
    await actions.set_rules(org, write_actions_enabled=False)
    with pytest.raises(ActionError):
        await actions.confirm(planner, uuid.UUID(card["action_id"]))
    assert product.write_log == {}


async def test_platform_failure_marks_failed_without_retry(actions, planner, product):
    card = (await propose(actions, planner, "pause_campaign", {"campaign_id": "cmp_101"}))["card"]
    product.fail_next_write = "platform rejected: campaign locked"
    with pytest.raises(ActionError) as exc:
        await actions.confirm(planner, uuid.UUID(card["action_id"]))
    assert exc.value.code == "failed"
    view = await actions.get(planner.org_id, uuid.UUID(card["action_id"]))
    assert view["status"] == "failed" and "campaign locked" in view["error"]
    assert (await actions.confirm(planner, uuid.UUID(card["action_id"])))["status"] == "failed"  # no retry


# ---------------------------------------------------------------- undo & tier 1 & expiry


async def test_undo_goes_through_confirmation_and_restores(actions, planner, product):
    c = (await propose(actions, planner, "update_campaign_budget", {"campaign_id": "cmp_101", "new_budget": 330000}))[
        "card"
    ]
    aid = uuid.UUID(c["action_id"])
    await actions.confirm(planner, aid)
    undo_card = await actions.undo(planner, aid)
    assert undo_card["after"]["budget"] == 300000 and undo_card["undo_of"] == str(aid)
    assert budget(product) == 330000
    await actions.confirm(planner, uuid.UUID(undo_card["action_id"]))
    assert budget(product) == 300000
    assert (await actions.get(planner.org_id, aid))["status"] == "undone"


async def test_undo_refused_when_item_changed_or_window_passed(actions, planner, product, clock):
    c = (await propose(actions, planner, "rename_campaign", {"campaign_id": "cmp_101", "new_name": "Search A"}))["card"]
    aid = uuid.UUID(c["action_id"])
    await actions.confirm(planner, aid)
    product.campaigns[0]["name"] = "Changed elsewhere"
    with pytest.raises(ActionError):
        await actions.undo(planner, aid)
    product.campaigns[0]["name"] = "Search A"
    clock.advance(hours=25)
    with pytest.raises(ActionError):
        await actions.undo(planner, aid)


async def test_tier1_can_skip_confirmation_when_org_allows(actions, planner, org, product):
    await actions.set_rules(org, tier1_requires_confirmation=False)
    r = await propose(actions, planner, "rename_campaign", {"campaign_id": "cmp_101", "new_name": "Diwali Search"})
    assert r["card"] is None and r["for_model"]["status"] == "executed"
    assert product.campaigns[0]["name"] == "Diwali Search"


async def test_expire_job(actions, planner, clock, engine):
    await propose(actions, planner, "pause_campaign", {"campaign_id": "cmp_101"})
    clock.advance(minutes=11)
    assert await actions.expire() == 1


async def test_rules_validation(actions, org):
    with pytest.raises(ActionError):
        await actions.set_rules(org, max_budget_change_pct=0)
    with pytest.raises(ActionError):
        await actions.set_rules(org, blocked_tools=["delete_everything"])


async def test_rejected_proposals_are_kept_for_audit(actions, planner, engine):
    await propose(actions, planner, "update_campaign_budget", {"campaign_id": "cmp_101", "new_budget": 900000})
    rows = await actions.list(planner.org_id, status="rejected")
    assert len(rows) == 1 and rows[0]["warnings"]
    async with engine.begin() as conn:  # status values are constrained by the DB
        with pytest.raises(Exception):  # noqa: B017
            await conn.execute(update(chat_actions).values(status="bogus"))

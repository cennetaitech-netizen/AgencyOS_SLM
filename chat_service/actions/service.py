"""Chat actions: the model *proposes*, a permitted human *confirms*, and this service *executes*.

The model can never execute a change. ``propose`` only stores a proposal and returns a confirmation card.
Execution happens in ``confirm`` / ``approve``, which an authenticated user calls through the API.

Statuses: proposed → confirmed (claimed) → [awaiting_second_approval → confirmed] → executing →
executed | failed. Also cancelled (by the requester, or because the state went stale), rejected (by
rules or an approver), expired, and undone (an executed action that was reversed).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import jsonschema
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from chat_service.actions.specs import ACTION_SPECS, ActionSpec
from chat_service.billing.caps import LogNotifier, Notifier
from chat_service.config import Settings
from chat_service.db.schema import chat_action_rules, chat_actions, chat_subscriptions
from chat_service.engine.store import OrgSettingsStore
from chat_service.metrics import ACTIONS
from chat_service.principal import Principal
from chat_service.tools.product import ProductAPI, ProductWriteConflict
from chat_service.tools.registry import Tool, ToolContext, ToolInputError


class ActionError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class ActionRules:
    write_actions_enabled: bool = False
    max_budget_change_pct: float = 20.0
    max_budget_change_abs_minor: int | None = None
    second_approval_above_minor: int | None = None
    daily_spend_change_cap_minor: int | None = None
    tier1_requires_confirmation: bool = True
    roles_by_tier: dict[str, list[str]] = field(default_factory=dict)
    blocked_tools: list[str] = field(default_factory=list)


def action_tools(specs: dict[str, ActionSpec]) -> list[Tool]:
    """Registry entries for write actions (schemas only: the registry never executes them)."""

    async def _never(ctx, args):  # pragma: no cover - the registry refuses to execute write tools
        raise RuntimeError("write tools are executed only by ActionService after confirmation")

    return [
        Tool(
            name=s.name,
            description=s.description,
            parameters=s.parameters,
            handler=_never,
            label=s.label,
            group="actions",
            read_only=False,
            risk_tier=s.risk_tier,
            reversible=s.reversible,
            preview_tool=s.preview_tool,
        )
        for s in specs.values()
    ]


class ActionService:
    def __init__(
        self,
        engine: AsyncEngine,
        settings: Settings,
        product: ProductAPI,
        specs: dict[str, ActionSpec] | None = None,
        orgs: OrgSettingsStore | None = None,
        notifier: Notifier | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.engine = engine
        self.settings = settings
        self.product = product
        self.specs = specs or ACTION_SPECS
        self.orgs = orgs or OrgSettingsStore(engine)
        self.notifier = notifier or LogNotifier()
        self.clock = clock

    # ------------------------------------------------------------------ rules & enablement

    async def rules(self, org_id: uuid.UUID) -> ActionRules:
        async with self.engine.connect() as conn:
            row = (await conn.execute(select(chat_action_rules).where(chat_action_rules.c.org_id == org_id))).first()
        if row is None:
            return ActionRules()
        return ActionRules(
            row.write_actions_enabled,
            float(row.max_budget_change_pct),
            row.max_budget_change_abs_minor,
            row.second_approval_above_minor,
            row.daily_spend_change_cap_minor,
            row.tier1_requires_confirmation,
            dict(row.roles_by_tier or {}),
            list(row.blocked_tools or []),
        )

    async def set_rules(self, org_id: uuid.UUID, **values: Any) -> ActionRules:
        allowed = set(ActionRules.__dataclass_fields__)
        values = {k: v for k, v in values.items() if k in allowed}
        if "max_budget_change_pct" in values and not 0 < float(values["max_budget_change_pct"]) <= 100:
            raise ActionError("invalid_rules", "max_budget_change_pct must be between 0 and 100")
        unknown = set(values.get("blocked_tools", [])) - set(self.specs)
        if unknown:
            raise ActionError("invalid_rules", f"unknown actions: {', '.join(sorted(unknown))}")
        async with self.engine.begin() as conn:
            stmt = pg_insert(chat_action_rules).values(org_id=org_id, **values)
            stmt = (
                stmt.on_conflict_do_update(index_elements=["org_id"], set_=values)
                if values
                else stmt.on_conflict_do_nothing(index_elements=["org_id"])
            )
            await conn.execute(stmt)
        return await self.rules(org_id)

    async def writes_enabled(self, org_id: uuid.UUID) -> bool:
        if not self.settings.write_actions_enabled_global:
            return False
        if not (await self.rules(org_id)).write_actions_enabled:
            return False
        async with self.engine.connect() as conn:
            status = (
                await conn.execute(select(chat_subscriptions.c.status).where(chat_subscriptions.c.org_id == org_id))
            ).scalar_one_or_none()
        return status in (None, "active")  # past due / suspended → read-only

    # ------------------------------------------------------------------ helpers

    async def ctx_for(self, principal: Principal, request_id: str) -> ToolContext:
        org = await self.orgs.get(principal.org_id)
        return ToolContext(
            org_id=principal.org_id,
            user_id=principal.user_id,
            request_id=request_id,
            roles=principal.roles,
            org_timezone=org.timezone,
            org_currency=org.currency,
            user_credential=principal.credential,
        )

    def _role_ok(self, rules: ActionRules, spec: ActionSpec, principal: Principal) -> bool:
        allowed = rules.roles_by_tier.get(str(spec.risk_tier))
        return not allowed or bool(set(allowed) & set(principal.roles))

    async def _spend_changed_today(self, org_id: uuid.UUID, exclude: uuid.UUID | None = None) -> int:
        tz = (await self.orgs.get(org_id)).timezone
        start = self.clock().astimezone(ZoneInfo(tz)).replace(hour=0, minute=0, second=0, microsecond=0)
        q = select(func.coalesce(func.sum(func.abs(chat_actions.c.spend_change_minor)), 0)).where(
            chat_actions.c.org_id == org_id,
            chat_actions.c.status == "executed",
            chat_actions.c.executed_at >= start.astimezone(UTC),
        )
        if exclude:
            q = q.where(chat_actions.c.action_id != exclude)
        async with self.engine.connect() as conn:
            return int((await conn.execute(q)).scalar_one())

    async def _check_rules(
        self,
        org_id: uuid.UUID,
        spec: ActionSpec,
        before: dict,
        after: dict,
        rules: ActionRules,
        exclude: uuid.UUID | None = None,
    ) -> tuple[list[str], list[str], int]:
        """Returns (blocking errors, warnings, spend change in minor units)."""
        errors: list[str] = []
        warnings: list[str] = []
        spend = spec.spend_change_minor(before, after)
        if spec.changes_spend and spend:
            old = float(before["budget"])
            pct = abs(spend) / (old * 100) * 100 if old else 100.0
            if pct > rules.max_budget_change_pct:
                errors.append(
                    f"a {pct:.1f}% change is above your organisation's limit of {rules.max_budget_change_pct:g}% "
                    "per action; propose a smaller step"
                )
            elif pct >= 10:
                warnings.append(f"large change ({pct:.1f}%)")
            if rules.max_budget_change_abs_minor is not None and abs(spend) > rules.max_budget_change_abs_minor:
                errors.append("the change is above your organisation's maximum amount per action")
            if rules.daily_spend_change_cap_minor is not None:
                today = await self._spend_changed_today(org_id, exclude)
                if today + abs(spend) > rules.daily_spend_change_cap_minor:
                    errors.append("this would exceed today's limit for budget changes made through chat")
        return errors, warnings, spend

    def _card(self, row: Any, spec: ActionSpec) -> dict[str, Any]:
        return {
            "action_id": str(row["action_id"]),
            "tool": spec.name,
            "label": spec.label,
            "risk_tier": spec.risk_tier,
            "target_id": row["target_id"],
            "before": row["before_state"],
            "after": row["proposed_after"],
            "warnings": row["rule_warnings"] or [],
            "requires_second_approval": row["requires_second_approval"],
            "expires_at": row["expires_at"].isoformat(),
            "undo_of": str(row["undo_of"]) if row["undo_of"] else None,
        }

    async def _get_row(self, org_id: uuid.UUID, action_id: uuid.UUID):
        async with self.engine.connect() as conn:
            row = (
                await conn.execute(
                    select(chat_actions).where(chat_actions.c.action_id == action_id, chat_actions.c.org_id == org_id)
                )
            ).first()
        if row is None:
            raise ActionError("not_found", "action not found")
        return row

    async def _set(self, action_id: uuid.UUID, **values: Any) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(update(chat_actions).where(chat_actions.c.action_id == action_id).values(**values))

    def view(self, row: Any) -> dict[str, Any]:
        spec = self.specs[row.tool_name]
        summary = None
        if row.status in ("executed", "undone") and row.before_state and row.proposed_after:
            summary = "Done: " + spec.summary(row.before_state, row.proposed_after)
        return {
            "action_id": str(row.action_id),
            "tool": row.tool_name,
            "label": spec.label,
            "status": row.status,
            "risk_tier": row.risk_tier,
            "target_id": row.target_id,
            "before": row.before_state,
            "after": row.proposed_after,
            "warnings": row.rule_warnings or [],
            "requires_second_approval": row.requires_second_approval,
            "requested_by": str(row.requested_by),
            "confirmed_by": str(row.confirmed_by) if row.confirmed_by else None,
            "second_approved_by": str(row.second_approved_by) if row.second_approved_by else None,
            "executed_at": row.executed_at,
            "error": row.error,
            "summary": summary,
            "undo_of": str(row.undo_of) if row.undo_of else None,
            "undo_available": bool(
                row.status == "executed"
                and spec.reversible
                and row.executed_at
                and self.clock() - row.executed_at < timedelta(hours=self.settings.undo_window_hours)
            ),
        }

    # ------------------------------------------------------------------ propose

    async def propose(
        self,
        principal: Principal,
        ctx: ToolContext,
        conversation_id: uuid.UUID,
        message_id: uuid.UUID,
        tool_name: str,
        raw_args: str | dict[str, Any] | None,
        undo_of: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        """Validate and store a proposal. Returns ``{"for_model": ..., "card": ... | None}``."""

        def rejected(reason: str) -> dict[str, Any]:
            ACTIONS.labels(tool_name if tool_name in self.specs else "unknown", "rejected").inc()
            return {"for_model": {"status": "rejected", "reason": reason}, "card": None}

        spec = self.specs.get(tool_name)
        if spec is None:
            return rejected(f"unknown action {tool_name!r}")
        if not await self.writes_enabled(principal.org_id):
            return rejected("chat actions aren't enabled for this organisation; tell the user where to make the change")
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args or {})
            jsonschema.validate(args, spec.parameters)
        except (ValueError, TypeError, jsonschema.ValidationError) as exc:
            msg = exc.message if isinstance(exc, jsonschema.ValidationError) else str(exc)
            return rejected(f"invalid arguments: {msg}")
        rules = await self.rules(principal.org_id)
        if tool_name in rules.blocked_tools:
            return rejected("your organisation has blocked this action in chat")
        if not self._role_ok(rules, spec, principal):
            return rejected("the user's role isn't allowed to make this kind of change")
        try:
            before_full = await self.product.call(ctx, spec.preview_tool, spec.preview_args(args))
        except PermissionError:
            return rejected("the user doesn't have permission to view or change this item")
        except ToolInputError as exc:
            return rejected(str(exc))
        before = spec.state(before_full)
        problems = spec.validate(before, args)
        if problems:
            return rejected("; ".join(problems))
        after = spec.after(before, args)
        errors, warnings, spend = await self._check_rules(principal.org_id, spec, before, after, rules)
        requires_second = (
            rules.second_approval_above_minor is not None and abs(spend) > rules.second_approval_above_minor
        )
        if requires_second:
            warnings.append("needs a second approver")
        now = self.clock()
        auto = spec.risk_tier == 1 and not rules.tier1_requires_confirmation and not errors
        row = {
            "action_id": uuid.uuid4(),
            "org_id": principal.org_id,
            "conversation_id": conversation_id,
            "message_id": message_id,
            "requested_by": principal.user_id,
            "tool_name": tool_name,
            "risk_tier": spec.risk_tier,
            "args": args,
            "before_state": before,
            "proposed_after": after,
            "rule_warnings": errors + warnings,
            "status": "rejected" if errors else "proposed",
            "target_id": spec.target_id(args),
            "spend_change_minor": spend,
            "requires_second_approval": requires_second,
            "undo_of": undo_of,
            "expires_at": now + timedelta(minutes=self.settings.action_expiry_minutes),
            "created_at": now,
        }
        async with self.engine.begin() as conn:
            await conn.execute(chat_actions.insert().values(**row))
        if errors:
            return rejected("; ".join(errors))
        ACTIONS.labels(tool_name, "proposed").inc()
        if auto:
            view = await self.confirm(principal, row["action_id"])
            return {"for_model": {"status": view["status"], "summary": view["summary"]}, "card": None, "executed": view}
        return {
            "for_model": {
                "status": "awaiting_user_confirmation",
                "action_id": str(row["action_id"]),
                "proposed_change": {"before": before, "after": after},
                "requires_second_approval": requires_second,
                "note": "Tell the user to review and confirm the card. Don't say the change is done.",
            },
            "card": self._card(row, spec),
        }

    # ------------------------------------------------------------------ confirm / approve / execute

    async def confirm(self, principal: Principal, action_id: uuid.UUID) -> dict[str, Any]:
        row = await self._get_row(principal.org_id, action_id)
        spec = self.specs[row.tool_name]
        if row.status != "proposed":
            return self.view(row)  # idempotent: double-click, or already handled
        if principal.user_id != row.requested_by and not principal.has_role(self.settings.approver_role):
            raise ActionError("forbidden", "only the person who asked, or an approver, can confirm this")
        now = self.clock()
        async with self.engine.begin() as conn:
            claimed = (
                await conn.execute(
                    update(chat_actions)
                    .where(
                        chat_actions.c.action_id == action_id,
                        chat_actions.c.status == "proposed",
                        chat_actions.c.expires_at > now,
                    )
                    .values(status="confirmed", confirmed_by=principal.user_id)
                    .returning(chat_actions.c.action_id)
                )
            ).first()
        if claimed is None:
            row = await self._get_row(principal.org_id, action_id)
            if row.status == "proposed":  # it expired
                await self._set(action_id, status="expired")
                raise ActionError("expired", "this proposal expired; ask again to get a fresh one")
            return self.view(row)

        ctx = await self.ctx_for(principal, str(action_id))
        problem = await self._revalidate(principal, ctx, row, spec)
        if problem:
            await self._set(action_id, status="cancelled", error=problem)
            raise ActionError("stale" if "changed" in problem else "not_allowed", problem)
        if row.requires_second_approval:
            await self._set(
                action_id,
                status="awaiting_second_approval",
                expires_at=now + timedelta(hours=self.settings.undo_window_hours),
            )
            await self.notifier.notify(
                principal.org_id,
                {
                    "type": "approval_needed",
                    "action_id": str(action_id),
                    "summary": spec.summary(row.before_state, row.proposed_after),
                },
            )
            return self.view(await self._get_row(principal.org_id, action_id))
        return await self._execute(principal, ctx, action_id)

    async def approve(self, principal: Principal, action_id: uuid.UUID) -> dict[str, Any]:
        row = await self._get_row(principal.org_id, action_id)
        spec = self.specs[row.tool_name]
        if row.status != "awaiting_second_approval":
            return self.view(row)
        if not principal.has_role(self.settings.approver_role):
            raise ActionError("forbidden", "only an approver can approve this change")
        if principal.user_id in (row.requested_by, row.confirmed_by):
            raise ActionError("forbidden", "the second approver must be a different person")
        now = self.clock()
        async with self.engine.begin() as conn:
            claimed = (
                await conn.execute(
                    update(chat_actions)
                    .where(
                        chat_actions.c.action_id == action_id,
                        chat_actions.c.status == "awaiting_second_approval",
                        chat_actions.c.expires_at > now,
                    )
                    .values(status="confirmed", second_approved_by=principal.user_id)
                    .returning(chat_actions.c.action_id)
                )
            ).first()
        if claimed is None:
            row = await self._get_row(principal.org_id, action_id)
            if row.status == "awaiting_second_approval":
                await self._set(action_id, status="expired")
                raise ActionError("expired", "this approval request expired")
            return self.view(row)
        ctx = await self.ctx_for(principal, str(action_id))
        problem = await self._revalidate(principal, ctx, row, spec, check_role=False)
        if problem:
            await self._set(action_id, status="cancelled", error=problem)
            raise ActionError("stale" if "changed" in problem else "not_allowed", problem)
        return await self._execute(principal, ctx, action_id)

    async def _revalidate(
        self, principal: Principal, ctx: ToolContext, row: Any, spec: ActionSpec, check_role: bool = True
    ) -> str | None:
        """Everything is checked again at confirmation time: enablement, role, current state, rules."""
        if not await self.writes_enabled(principal.org_id):
            return "chat actions have been turned off"
        rules = await self.rules(principal.org_id)
        if row.tool_name in rules.blocked_tools:
            return "this action has been blocked by your organisation"
        if check_role and not self._role_ok(rules, spec, principal):
            return "your role isn't allowed to make this kind of change"
        try:
            current = spec.state(await self.product.call(ctx, spec.preview_tool, spec.preview_args(row.args)))
        except PermissionError:
            return "you no longer have permission to change this item"
        except ToolInputError as exc:
            return str(exc)
        if current != row.before_state:
            return "the item changed since this was proposed; please review it again"
        errors, _, _ = await self._check_rules(
            principal.org_id, spec, row.before_state, row.proposed_after, rules, exclude=row.action_id
        )
        return "; ".join(errors) or None

    async def _execute(self, principal: Principal, ctx: ToolContext, action_id: uuid.UUID) -> dict[str, Any]:
        async with self.engine.begin() as conn:
            row = (
                await conn.execute(
                    update(chat_actions)
                    .where(chat_actions.c.action_id == action_id, chat_actions.c.status == "confirmed")
                    .values(status="executing")
                    .returning(chat_actions)
                )
            ).first()
        if row is None:
            return self.view(await self._get_row(principal.org_id, action_id))
        try:
            response = await self.product.write(ctx, row.tool_name, row.args, idempotency_key=str(action_id))
        except PermissionError:
            ACTIONS.labels(row.tool_name, "failed").inc()
            await self._set(action_id, status="failed", error="permission denied by the product")
            raise ActionError("failed", "the product refused the change: permission denied") from None
        except (ProductWriteConflict, ToolInputError) as exc:
            ACTIONS.labels(row.tool_name, "failed").inc()
            await self._set(action_id, status="failed", error=str(exc)[:500])
            raise ActionError("failed", f"the change wasn't made: {exc}") from None
        except Exception as exc:  # noqa: BLE001 - unknown outcome: never retried automatically (tier 2–3)
            await self._set(action_id, status="failed", error=f"{type(exc).__name__}: outcome unknown, check the item")
            raise ActionError("failed", "the change may not have been made; please check the item") from None
        await self._set(action_id, status="executed", executed_at=self.clock(), platform_response=response)
        ACTIONS.labels(row.tool_name, "executed").inc()
        if row.undo_of:
            await self._set(row.undo_of, status="undone")
        return self.view(await self._get_row(principal.org_id, action_id))

    # ------------------------------------------------------------------ cancel / reject / undo / expire

    async def cancel(self, principal: Principal, action_id: uuid.UUID) -> dict[str, Any]:
        row = await self._get_row(principal.org_id, action_id)
        if principal.user_id != row.requested_by:
            raise ActionError("forbidden", "only the person who asked can cancel this")
        async with self.engine.begin() as conn:
            await conn.execute(
                update(chat_actions)
                .where(
                    chat_actions.c.action_id == action_id,
                    chat_actions.c.status.in_(("proposed", "awaiting_second_approval")),
                )
                .values(status="cancelled")
            )
        return self.view(await self._get_row(principal.org_id, action_id))

    async def reject(self, principal: Principal, action_id: uuid.UUID, reason: str | None = None) -> dict[str, Any]:
        if not principal.has_role(self.settings.approver_role):
            raise ActionError("forbidden", "only an approver can reject this")
        async with self.engine.begin() as conn:
            await conn.execute(
                update(chat_actions)
                .where(
                    chat_actions.c.action_id == action_id,
                    chat_actions.c.org_id == principal.org_id,
                    chat_actions.c.status == "awaiting_second_approval",
                )
                .values(status="rejected", error=reason or "rejected by approver")
            )
        return self.view(await self._get_row(principal.org_id, action_id))

    async def undo(self, principal: Principal, action_id: uuid.UUID) -> dict[str, Any]:
        """Propose the reverse change. It goes through confirmation like any other action."""
        row = await self._get_row(principal.org_id, action_id)
        spec = self.specs[row.tool_name]
        if not self.view(row)["undo_available"]:
            raise ActionError("not_allowed", "undo isn't available for this action")
        if principal.user_id != row.requested_by and not principal.has_role(self.settings.approver_role):
            raise ActionError("forbidden", "only the person who asked, or an approver, can undo this")
        ctx = await self.ctx_for(principal, f"undo-{action_id}")
        current = spec.state(await self.product.call(ctx, spec.preview_tool, spec.preview_args(row.args)))
        if current != row.proposed_after:
            raise ActionError("stale", "the item changed after this action, so it can't be undone automatically")
        undo_tool, undo_args = spec.undo(row.before_state, row.args)
        result = await self.propose(
            principal, ctx, row.conversation_id, row.message_id, undo_tool, undo_args, undo_of=row.action_id
        )
        if result["card"] is None:
            raise ActionError("not_allowed", result["for_model"].get("reason", "undo isn't possible"))
        return result["card"]

    async def expire(self) -> int:
        async with self.engine.begin() as conn:
            result = await conn.execute(
                update(chat_actions)
                .where(
                    chat_actions.c.status.in_(("proposed", "awaiting_second_approval")),
                    chat_actions.c.expires_at <= self.clock(),
                )
                .values(status="expired")
            )
        return result.rowcount

    async def list(self, org_id: uuid.UUID, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        q = select(chat_actions).where(chat_actions.c.org_id == org_id)
        if status:
            q = q.where(chat_actions.c.status == status)
        async with self.engine.connect() as conn:
            rows = (await conn.execute(q.order_by(chat_actions.c.created_at.desc()).limit(limit))).all()
        return [self.view(r) for r in rows]

    async def get(self, org_id: uuid.UUID, action_id: uuid.UUID) -> dict[str, Any]:
        return self.view(await self._get_row(org_id, action_id))

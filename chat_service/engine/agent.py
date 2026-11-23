"""The chat engine: gates → reserve → agent loop (model ⇄ tools) → settle, streamed as events.

Implements docs/IMPLEMENTATION_GUIDE.md §5.4 with the gap-review additions: context block, daily
ceilings, shadow billing, refusal handling, client-disconnect settlement, version tracking.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from chat_service import metrics
from chat_service.billing.tokenizer import BillingTokenizer
from chat_service.billing.wallet import Reservation, UsageRecord, WalletService
from chat_service.config import Settings
from chat_service.engine.grounding import ungrounded_numbers
from chat_service.engine.limits import ConcurrencyLimiter, MemoryConcurrencyLimiter, MemoryRateLimiter, RateLimiter
from chat_service.engine.prompting import PromptContext, build_messages, load_prompt
from chat_service.engine.store import ConversationStore, OrgSettingsStore
from chat_service.llm.client import ModelClient, ModelError, StreamEnd, TextDelta
from chat_service.principal import Principal
from chat_service.tools.registry import ToolContext, ToolRegistry

if TYPE_CHECKING:
    from chat_service.actions.service import ActionService
    from chat_service.analytics.events import AnalyticsSink
    from chat_service.billing.caps import CapsService
    from chat_service.billing.subscriptions import SubscriptionService

REFUSAL_SENTENCE = "I can only help with media campaigns and AgencyOS."
log = logging.getLogger("chat_service.engine")


def event(name: str, **data: Any) -> dict[str, Any]:
    return {"event": name, "data": data}


def error_event(code: str, message: str) -> dict[str, Any]:
    metrics.ERRORS.labels(code).inc()
    return event("error", code=code, message=message, charged=False)


@dataclass
class _Turn:
    """Mutable state of one assistant message."""

    message_id: uuid.UUID
    billable_input: int
    reservation: Reservation | None = None
    visible: str = ""
    steps: int = 0
    host_prompt: int = 0
    host_completion: int = 0
    host_cached: int = 0
    finished: bool = False
    prompt_version: str = "system_v1"
    writes_enabled: bool = False
    client_id: str | None = None
    workspace_id: uuid.UUID | None = None
    model: Any = None  # the ModelClient serving this message
    started: float = 0.0
    ttft_ms: int | None = None
    sources: list[Any] | None = None  # data the answer may draw numbers from (grounding check)
    tools_used: list[str] | None = None


class ChatEngine:
    def __init__(
        self,
        settings: Settings,
        wallet: WalletService,
        registry: ToolRegistry,
        model: ModelClient,
        tokenizer: BillingTokenizer,
        conversations: ConversationStore,
        orgs: OrgSettingsStore,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        actions: ActionService | None = None,
        subscriptions: SubscriptionService | None = None,
        caps: CapsService | None = None,
        analytics: AnalyticsSink | None = None,
        rate_limiter: RateLimiter | None = None,
        concurrency: ConcurrencyLimiter | None = None,
    ) -> None:
        self.s = settings
        self.wallet = wallet
        self.registry = registry
        self.model = model
        self.tokenizer = tokenizer
        self.conversations = conversations
        self.orgs = orgs
        self.clock = clock
        self.rate_limiter = rate_limiter or MemoryRateLimiter(settings.rate_limit_per_user_per_min)
        self.concurrency = concurrency or MemoryConcurrencyLimiter(settings.max_concurrent_streams_per_org)
        self.actions = actions
        self.subscriptions = subscriptions
        self.caps = caps
        self.analytics = analytics
        self._templates: dict[str, str] = {}

    # ------------------------------------------------------------------ public

    async def handle_message(
        self, principal: Principal, conversation_id: uuid.UUID, text: str
    ) -> AsyncIterator[dict[str, Any]]:
        s = self.s
        org_id, user_id = principal.org_id, principal.user_id

        if not s.enabled_global:
            yield error_event("chat_disabled", "The assistant is temporarily unavailable.")
            return
        org = await self.orgs.get(org_id)
        if not org.chat_enabled:
            yield error_event("chat_disabled", "The assistant isn't enabled for your organisation yet.")
            return
        conv = await self.conversations.get(org_id, user_id, conversation_id)
        if conv is None:
            yield error_event("not_found", "Conversation not found.")
            return
        text = (text or "").strip()
        if not text:
            yield error_event("empty_message", "Please type a message.")
            return
        if len(text) > s.max_message_chars:
            yield error_event("message_too_long", f"Messages can be up to {s.max_message_chars} characters.")
            return
        if await self.conversations.count_user_turns(conversation_id) >= s.max_turns_per_conversation:
            yield error_event("conversation_too_long", "This chat is very long. Please start a new chat.")
            return
        day_start_utc = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        if await self.wallet.global_host_tokens_since(day_start_utc) >= s.global_daily_host_token_ceiling:
            yield error_event("chat_disabled", "The assistant is at capacity right now. Please try again later.")
            return
        if not await self.rate_limiter.allow(user_id):
            log.warning("refused: per-user rate limit org=%s user=%s", org_id, user_id)
            yield error_event("rate_limited", "You're sending messages too quickly. Please wait a moment.")
            return
        slot = await self.concurrency.try_acquire(org_id)
        if slot is None:
            log.warning("refused: org concurrency limit org=%s", org_id)
            yield error_event("rate_limited", "Too many replies are in progress for your organisation.")
            return
        try:
            # aclosing: if the client disconnects, close the inner generator now (so the
            # disconnect settlement runs immediately) instead of whenever it's garbage collected.
            async with aclosing(self._run(principal, org, conversation_id, text)) as inner:
                async for ev in inner:
                    yield ev
        finally:
            await self.concurrency.release(org_id, slot)

    # ------------------------------------------------------------------ internals

    async def _track(self, event_name: str, principal: Principal, **props: Any) -> None:
        if self.analytics is not None:
            try:
                await self.analytics.track(event_name, principal.org_id, principal.user_id, props)
            except Exception:  # noqa: BLE001 - analytics must never break chat
                log.exception("analytics sink failed")

    async def _select_model(self, principal: Principal, org) -> Any:
        if hasattr(self.model, "select"):  # ModelRouter: canary / pinning (Phase 11)
            return await self.model.select(principal.org_id, getattr(org, "model_pin", None))
        return self.model

    def _mode(self, org) -> str:
        return org.billing_mode or self.s.billing_mode

    def _day_start(self, tz_name: str) -> datetime:
        tz = ZoneInfo(tz_name)
        local = self.clock().astimezone(tz)
        return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)

    def _messages(self, principal: Principal, org, version: str, history, text: str) -> list[dict[str, Any]]:
        if version not in self._templates:
            self._templates[version] = load_prompt(version)
        ctx = PromptContext.at(self.clock(), org.timezone, org.currency, principal.primary_role, principal.display_name)
        return build_messages(self._templates[version], ctx, history, text)

    def _usage(self, turn: _Turn) -> UsageRecord:
        return UsageRecord(
            billable_input_tokens=turn.billable_input,
            billable_output_tokens=self.tokenizer.count(turn.visible),
            model_name=turn.model.name,
            prompt_version=turn.prompt_version,
            billing_tokenizer_version=self.tokenizer.version,
            host_prompt_tokens=turn.host_prompt,
            host_completion_tokens=turn.host_completion,
            host_cached_prompt_tokens=turn.host_cached,
            serving_host=getattr(turn.model, "last_serving_host", "primary"),
            steps=turn.steps,
            workspace_id=turn.workspace_id,
            client_id=turn.client_id,
        )

    async def _finalise(
        self, principal: Principal, org, conversation_id: uuid.UUID, turn: _Turn, charge: bool, reason: str | None
    ) -> int:
        """Settle or release (live) / record (shadow). Returns tokens charged (or would-be charged)."""
        usage = self._usage(turn)
        if self._mode(org) == "live":
            assert turn.reservation is not None
            if charge and usage.billable_total > 0:
                return await self.wallet.settle(turn.reservation.reservation_id, usage)
            await self.wallet.release(turn.reservation.reservation_id, usage, reason or "not_charged")
            return 0
        return await self.wallet.record_shadow(
            principal.org_id, principal.user_id, conversation_id, turn.message_id, usage, charge, reason
        )

    async def _run(self, principal: Principal, org, conversation_id: uuid.UUID, text: str):
        s = self.s
        mode = self._mode(org)
        if self.subscriptions is not None:
            sub = await self.subscriptions.get(principal.org_id)
            if sub is not None and sub.status == "suspended":
                yield error_event("billing_suspended", "The assistant is paused because of an unpaid invoice.")
                return
        if await self.wallet.ensure_free_trial(principal.org_id):
            await self._track("trial_granted", principal, tokens=s.free_trial_tokens)
        balance = await self.wallet.get_balance(principal.org_id)
        used_today = await self.wallet.org_tokens_since(principal.org_id, self._day_start(org.timezone))
        daily_cap = s.free_trial_daily_max_tokens if balance.only_free_trial else s.org_daily_max_tokens

        turn = _Turn(
            message_id=uuid.uuid4(),
            billable_input=self.tokenizer.count(text),
            workspace_id=principal.workspace_id,
            started=time.monotonic(),
            sources=[text, self.clock().astimezone(ZoneInfo(org.timezone)).date().isoformat()],
            tools_used=[],
        )
        turn.model = await self._select_model(principal, org)
        turn.writes_enabled = self.actions is not None and await self.actions.writes_enabled(principal.org_id)
        turn.prompt_version = s.prompt_version_actions if turn.writes_enabled else s.prompt_version

        # Every limit that applies; the tightest one sets the reply budget (and the error if too small).
        limits = [
            (daily_cap - used_today, "daily_limit_reached", "Your organisation has reached today's assistant limit.")
        ]
        if mode == "live":
            capacity = await self.wallet.capacity(principal.org_id)
            limits.append((capacity, "insufficient_tokens", "You've used all your tokens."))
        if self.caps is not None:
            cap = await self.caps.check(principal.org_id, principal.user_id, principal.workspace_id, org.timezone)
            if cap.remaining is not None:
                limits.append(
                    (cap.remaining, "usage_cap_reached", "You've reached the assistant limit set by your admin.")
                )
        headroom, code, msg = min(limits, key=lambda x: x[0])
        output_budget = min(s.max_output_tokens, headroom - turn.billable_input)
        if output_budget < s.min_useful_output:
            yield error_event(code, msg)
            return
        if mode == "live":
            turn.reservation = await self.wallet.reserve(
                principal.org_id,
                principal.user_id,
                conversation_id,
                turn.message_id,
                turn.billable_input + output_budget,
            )
            if turn.reservation is None:
                yield error_event("insufficient_tokens", "You've used all your tokens.")
                return

        ctx = ToolContext(
            org_id=principal.org_id,
            user_id=principal.user_id,
            request_id=str(turn.message_id),
            roles=principal.roles,
            org_timezone=org.timezone,
            org_currency=org.currency,
            user_credential=principal.credential,
        )
        tool_records: list[dict[str, Any]] = []
        yield event("message.start", message_id=str(turn.message_id), conversation_id=str(conversation_id))

        try:
            history = await self.conversations.history(conversation_id, s.history_turns)
            await self.conversations.add_message(conversation_id, "user", text)
            messages = self._messages(principal, org, turn.prompt_version, history, text)
            for step in range(1, s.max_steps + 1):
                turn.steps = step
                is_last = step == s.max_steps
                remaining = output_budget - self.tokenizer.count(turn.visible)
                if remaining <= 0:
                    break
                step_text = ""
                end: StreamEnd | None = None
                async for ev in turn.model.stream(
                    messages,
                    None if is_last else self.registry.schemas(include_write=turn.writes_enabled),
                    min(s.max_output_tokens, max(remaining, 256)),
                    s.model_temperature,
                ):
                    if isinstance(ev, TextDelta):
                        if turn.ttft_ms is None:
                            turn.ttft_ms = int((time.monotonic() - turn.started) * 1000)
                        step_text += ev.text
                        turn.visible += ev.text
                        yield event("token", text=ev.text)
                    else:
                        end = ev
                if end is None:
                    raise ModelError("model stream ended without a final event", before_first_token=False)
                turn.host_prompt += end.prompt_tokens or 0
                turn.host_completion += end.completion_tokens or 0
                turn.host_cached += end.cached_prompt_tokens or 0
                if not end.tool_calls or is_last:
                    break
                messages.append(
                    {
                        "role": "assistant",
                        "content": step_text or None,
                        "tool_calls": [
                            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                            for c in end.tool_calls
                        ],
                    }
                )
                for call in end.tool_calls:
                    tool = self.registry.get(call.name)
                    args = _safe_args(call.arguments)
                    turn.tools_used.append(call.name)
                    turn.client_id = turn.client_id or _client_of(args)
                    yield event("tool.start", tool=call.name, label=tool.label if tool else "Working on it")
                    if tool is not None and not tool.read_only and turn.writes_enabled:
                        # Write action: store a proposal for the user to confirm. Never executed here.
                        proposal = await self.actions.propose(
                            principal, ctx, conversation_id, turn.message_id, call.name, call.arguments
                        )
                        ok = proposal["card"] is not None or "executed" in proposal
                        yield event("tool.end", tool=call.name, ok=ok)
                        turn.sources.append(proposal["for_model"])
                        if proposal["card"] is not None:
                            await self._track("action_proposed", principal, tool=call.name)
                            yield event("action.proposed", **proposal["card"])
                        if "executed" in proposal:
                            yield event("action.executed", **proposal["executed"])
                        content = json.dumps(proposal["for_model"], default=str)
                        tool_records.append(
                            {
                                "step": step,
                                "tool_name": call.name,
                                "args": args,
                                "ok": ok,
                                "result": proposal["for_model"],
                                "result_chars": len(content),
                                "truncated": False,
                                "duration_ms": None,
                                "error": None if ok else proposal["for_model"].get("reason"),
                            }
                        )
                        messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
                        continue
                    result = await self.registry.execute(
                        ctx, call.name, call.arguments, s.tool_timeout_seconds, s.tool_result_max_chars
                    )
                    yield event("tool.end", tool=call.name, ok=result.ok)
                    metrics.TOOL_CALLS.labels(call.name if tool else "unknown", str(result.ok).lower()).inc()
                    if result.ok:
                        turn.client_id = turn.client_id or _client_of(result.data)
                        turn.sources.append(result.data)
                    tool_records.append(
                        {
                            "step": step,
                            "tool_name": call.name,
                            "args": args,
                            "ok": result.ok,
                            "result": result.data if result.ok else None,
                            "result_chars": result.result_chars,
                            "truncated": result.truncated,
                            "duration_ms": result.duration_ms,
                            "error": result.error,
                        }
                    )
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": result.for_model()})

            refusal = turn.visible.strip().startswith(REFUSAL_SENTENCE)
            charge = not (refusal and not s.bill_refusals)
            charged = await self._finalise(
                principal, org, conversation_id, turn, charge, "refusal" if refusal else None
            )
            turn.finished = True
            ungrounded = [] if refusal else ungrounded_numbers(turn.visible, turn.sources)
            flags = {"ungrounded_numbers": ungrounded} if ungrounded else None
            await self._save_assistant(conversation_id, turn, charged, tool_records, flags)
            balance = await self.wallet.get_balance(principal.org_id)
            usage = self._usage(turn)
            alerts = (
                await self.caps.evaluate_alerts(
                    principal.org_id, principal.user_id, principal.workspace_id, org.timezone
                )
                if self.caps is not None
                else []
            )
            yield event(
                "message.end",
                message_id=str(turn.message_id),
                billable_input_tokens=usage.billable_input_tokens,
                billable_output_tokens=usage.billable_output_tokens,
                tokens_charged=charged,
                billing_mode=mode,
                balance_after=balance.spendable,
                low_balance=balance.spendable < s.low_balance_tokens,
                alerts=alerts,
                quality_flags=flags or {},
            )
            await self._after_message(
                principal,
                conversation_id,
                turn,
                mode,
                charged,
                "ok",
                refusal,
                flags,
                alerts,
                balance.spendable < s.low_balance_tokens,
            )
        except (asyncio.CancelledError, GeneratorExit):
            # Client disconnected mid-stream: charge only the visible text already sent.
            if not turn.finished:
                turn.finished = True
                await asyncio.shield(self._on_disconnect(principal, org, conversation_id, turn, tool_records))
            raise
        except Exception as exc:  # noqa: BLE001 - never charge for our failures
            if not turn.finished:
                turn.finished = True
                reason = "model_error" if isinstance(exc, ModelError) else "internal_error"
                log.exception("chat message failed: %s", reason)
                await self._finalise(principal, org, conversation_id, turn, False, reason)
                await self._save_assistant(conversation_id, turn, 0, tool_records)
                await self._after_message(principal, conversation_id, turn, mode, 0, reason, False, None, [], False)
            yield error_event("internal_error", "Something went wrong. You were not charged for this message.")

    async def _on_disconnect(self, principal, org, conversation_id, turn: _Turn, tool_records) -> None:
        charged = await self._finalise(principal, org, conversation_id, turn, bool(turn.visible), "client_disconnect")
        await self._save_assistant(conversation_id, turn, charged, tool_records)

    async def _after_message(
        self, principal, conversation_id, turn: _Turn, mode, charged, status, refusal, flags, alerts, low_balance
    ) -> None:
        """Structured log line per message (no secrets, no message text) + analytics events."""
        record = {
            "org_id": str(principal.org_id),
            "user_id": str(principal.user_id),
            "conversation_id": str(conversation_id),
            "message_id": str(turn.message_id),
            "model": turn.model.name if turn.model else None,
            "serving_host": getattr(turn.model, "last_serving_host", "primary"),
            "prompt_version": turn.prompt_version,
            "steps": turn.steps,
            "tools": turn.tools_used,
            "billable_input": turn.billable_input,
            "billable_output": self.tokenizer.count(turn.visible),
            "tokens_charged": charged,
            "host_prompt_tokens": turn.host_prompt,
            "host_completion_tokens": turn.host_completion,
            "host_cached_prompt_tokens": turn.host_cached,
            "ttft_ms": turn.ttft_ms,
            "latency_ms": int((time.monotonic() - turn.started) * 1000),
            "billing_mode": mode,
            "status": status,
            "refusal": refusal,
            "ungrounded_numbers": len((flags or {}).get("ungrounded_numbers", [])),
        }
        log.info("chat_message %s", json.dumps(record))
        model_name = record["model"] or "unknown"
        metrics.MESSAGES.labels("refusal" if refusal else status, mode).inc()
        metrics.MESSAGE_LATENCY.observe(record["latency_ms"] / 1000)
        if turn.ttft_ms is not None:
            metrics.TTFT.observe(turn.ttft_ms / 1000)
        metrics.BILLABLE_TOKENS.labels(mode).inc(charged)
        metrics.HOST_TOKENS.labels("prompt", model_name).inc(turn.host_prompt)
        metrics.HOST_TOKENS.labels("completion", model_name).inc(turn.host_completion)
        metrics.HOST_CACHED_TOKENS.labels(model_name).inc(turn.host_cached)
        if record["ungrounded_numbers"]:
            metrics.UNGROUNDED.inc()
        if status != "ok":
            await self._track("chat_error", principal, reason=status)
            return
        await self._track(
            "chat_message_sent",
            principal,
            tokens=charged,
            steps=turn.steps,
            tools=turn.tools_used,
            latency_ms=record["latency_ms"],
            mode=mode,
        )
        if refusal:
            await self._track("chat_refusal", principal)
        if low_balance:
            await self._track("low_balance_shown", principal)
        for alert in alerts:
            await self._track("usage_alert", principal, **alert)

    async def _save_assistant(self, conversation_id, turn: _Turn, charged: int, tool_records, flags=None) -> None:
        await self.conversations.add_message(
            conversation_id,
            "assistant",
            turn.visible,
            message_id=turn.message_id,
            tokens_charged=charged,
            prompt_version=turn.prompt_version,
            model_name=turn.model.name if turn.model else None,
            flags=flags,
        )
        await self.conversations.add_tool_calls(turn.message_id, tool_records)


def _client_of(data: Any) -> str | None:
    """Best-effort brand/client attribution for per-brand usage breakdowns on invoices."""
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("client_id"), str):
        return data["client_id"]
    rows = data.get("rows")
    if isinstance(rows, list):
        ids = {r.get("client_id") for r in rows if isinstance(r, dict) and r.get("client_id")}
        if len(ids) == 1:
            return ids.pop()
    return None


def _safe_args(raw: str) -> Any:
    try:
        value = json.loads(raw) if raw else {}
        return value if isinstance(value, dict) else {"_raw": raw[:2000]}
    except ValueError:
        return {"_raw": raw[:2000]}

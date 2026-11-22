"""The chat engine: gates → reserve → agent loop (model ⇄ tools) → settle, streamed as events.

Implements docs/IMPLEMENTATION_GUIDE.md §5.4 with the gap-review additions: context block, daily
ceilings, shadow billing, refusal handling, client-disconnect settlement, version tracking.
"""

from __future__ import annotations

import asyncio
from contextlib import aclosing
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from chat_service.billing.tokenizer import BillingTokenizer
from chat_service.billing.wallet import Reservation, UsageRecord, WalletService
from chat_service.config import Settings
from chat_service.engine.limits import ConcurrencyLimiter, RateLimiter
from chat_service.engine.store import ConversationStore, OrgSettingsStore
from chat_service.llm.client import ModelClient, ModelError, StreamEnd, TextDelta
from chat_service.principal import Principal
from chat_service.tools.registry import ToolContext, ToolRegistry

REFUSAL_SENTENCE = "I can only help with media campaigns and AgencyOS."
_PROMPTS = Path(__file__).with_name("prompts")


def load_prompt(version: str) -> str:
    return (_PROMPTS / f"{version}.txt").read_text()


def event(name: str, **data: Any) -> dict[str, Any]:
    return {"event": name, "data": data}


def error_event(code: str, message: str) -> dict[str, Any]:
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
    finished: bool = False


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
    ) -> None:
        self.s = settings
        self.wallet = wallet
        self.registry = registry
        self.model = model
        self.tokenizer = tokenizer
        self.conversations = conversations
        self.orgs = orgs
        self.clock = clock
        self.rate_limiter = RateLimiter(settings.rate_limit_per_user_per_min)
        self.concurrency = ConcurrencyLimiter(settings.max_concurrent_streams_per_org)
        self.system_template = load_prompt(settings.prompt_version)

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
        if not self.rate_limiter.allow(user_id):
            yield error_event("rate_limited", "You're sending messages too quickly. Please wait a moment.")
            return
        if not self.concurrency.try_acquire(org_id):
            yield error_event("rate_limited", "Too many replies are in progress for your organisation.")
            return
        try:
            # aclosing: if the client disconnects, close the inner generator now (so the
            # disconnect settlement runs immediately) instead of whenever it's garbage collected.
            async with aclosing(self._run(principal, org, conversation_id, text)) as inner:
                async for ev in inner:
                    yield ev
        finally:
            self.concurrency.release(org_id)

    # ------------------------------------------------------------------ internals

    def _mode(self, org) -> str:
        return org.billing_mode or self.s.billing_mode

    def _day_start(self, tz_name: str) -> datetime:
        tz = ZoneInfo(tz_name)
        local = self.clock().astimezone(tz)
        return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)

    def _system_prompt(self, principal: Principal, org) -> str:
        today = self.clock().astimezone(ZoneInfo(org.timezone)).date().isoformat()
        return self.system_template.format(
            today_date=today,
            org_timezone=org.timezone,
            org_currency=org.currency,
            user_display_name=principal.display_name,
            user_role=principal.primary_role,
        )

    def _usage(self, turn: _Turn) -> UsageRecord:
        return UsageRecord(
            billable_input_tokens=turn.billable_input,
            billable_output_tokens=self.tokenizer.count(turn.visible),
            model_name=self.model.name,
            prompt_version=self.s.prompt_version,
            billing_tokenizer_version=self.tokenizer.version,
            host_prompt_tokens=turn.host_prompt,
            host_completion_tokens=turn.host_completion,
            serving_host=getattr(self.model, "last_serving_host", "primary"),
            steps=turn.steps,
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
        await self.wallet.ensure_free_trial(principal.org_id)
        balance = await self.wallet.get_balance(principal.org_id)
        used_today = await self.wallet.org_tokens_since(principal.org_id, self._day_start(org.timezone))
        daily_cap = s.free_trial_daily_max_tokens if balance.only_free_trial else s.org_daily_max_tokens
        if used_today >= daily_cap:
            yield error_event("daily_limit_reached", "Your organisation has reached today's assistant limit.")
            return

        turn = _Turn(message_id=uuid.uuid4(), billable_input=self.tokenizer.count(text))
        output_budget = min(s.max_output_tokens, daily_cap - used_today - turn.billable_input)
        if mode == "live":
            output_budget = min(output_budget, balance.spendable - turn.billable_input)
        if output_budget < s.min_useful_output:
            code = "insufficient_tokens" if mode == "live" and balance.spendable < daily_cap - used_today \
                else "daily_limit_reached"
            msg = "You've used all your tokens." if code == "insufficient_tokens" else \
                "Your organisation has reached today's assistant limit."
            yield error_event(code, msg)
            return
        if mode == "live":
            turn.reservation = await self.wallet.reserve(
                principal.org_id, principal.user_id, conversation_id, turn.message_id,
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
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": self._system_prompt(principal, org)},
                *history,
                {"role": "user", "content": text},
            ]
            for step in range(1, s.max_steps + 1):
                turn.steps = step
                is_last = step == s.max_steps
                remaining = output_budget - self.tokenizer.count(turn.visible)
                if remaining <= 0:
                    break
                step_text = ""
                end: StreamEnd | None = None
                async for ev in self.model.stream(
                    messages,
                    None if is_last else self.registry.schemas(),
                    min(s.max_output_tokens, max(remaining, 256)),
                    s.model_temperature,
                ):
                    if isinstance(ev, TextDelta):
                        step_text += ev.text
                        turn.visible += ev.text
                        yield event("token", text=ev.text)
                    else:
                        end = ev
                if end is None:
                    raise ModelError("model stream ended without a final event", before_first_token=False)
                turn.host_prompt += end.prompt_tokens or 0
                turn.host_completion += end.completion_tokens or 0
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
                    yield event("tool.start", tool=call.name, label=tool.label if tool else "Working on it")
                    result = await self.registry.execute(
                        ctx, call.name, call.arguments, s.tool_timeout_seconds, s.tool_result_max_chars
                    )
                    yield event("tool.end", tool=call.name, ok=result.ok)
                    tool_records.append(
                        {
                            "step": step,
                            "tool_name": call.name,
                            "args": _safe_args(call.arguments),
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
            charged = await self._finalise(principal, org, conversation_id, turn, charge,
                                           "refusal" if refusal else None)
            turn.finished = True
            await self._save_assistant(conversation_id, turn, charged, tool_records)
            balance = await self.wallet.get_balance(principal.org_id)
            usage = self._usage(turn)
            yield event(
                "message.end",
                message_id=str(turn.message_id),
                billable_input_tokens=usage.billable_input_tokens,
                billable_output_tokens=usage.billable_output_tokens,
                tokens_charged=charged,
                billing_mode=mode,
                balance_after=balance.spendable,
                low_balance=balance.spendable < s.low_balance_tokens,
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
                await self._finalise(principal, org, conversation_id, turn, False, reason)
                await self._save_assistant(conversation_id, turn, 0, tool_records)
            yield error_event("internal_error", "Something went wrong. You were not charged for this message.")

    async def _on_disconnect(self, principal, org, conversation_id, turn: _Turn, tool_records) -> None:
        charged = await self._finalise(
            principal, org, conversation_id, turn, bool(turn.visible), "client_disconnect"
        )
        await self._save_assistant(conversation_id, turn, charged, tool_records)

    async def _save_assistant(self, conversation_id, turn: _Turn, charged: int, tool_records) -> None:
        await self.conversations.add_message(
            conversation_id,
            "assistant",
            turn.visible,
            message_id=turn.message_id,
            tokens_charged=charged,
            prompt_version=self.s.prompt_version,
            model_name=self.model.name,
        )
        await self.conversations.add_tool_calls(turn.message_id, tool_records)


def _safe_args(raw: str) -> Any:
    import json

    try:
        value = json.loads(raw) if raw else {}
        return value if isinstance(value, dict) else {"_raw": raw[:2000]}
    except ValueError:
        return {"_raw": raw[:2000]}

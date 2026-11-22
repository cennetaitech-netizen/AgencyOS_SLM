# Shared Contracts (Gate G1)

These are the interfaces every workstream builds against (COMBINED_RELEASE_PLAN.md §1). **The code is the source of truth.** This page says where each contract lives and what's still open. Any change to a contract needs tech-lead approval and a note in the change log below.

| # | Contract | Source of truth | Status |
|---|---|---|---|
| 1 | **Database schema** (M1 + M2 tables) | `chat_service/db/schema.py` → migration `migrations/versions/0001_initial_schema.py` | ✅ Draft v1 |
| 2 | **Chat API + SSE events** | `chat_service/api/app.py` (OpenAPI at `/docs` and `/openapi.json`); events from `chat_service/engine/agent.py` | ✅ Draft v1 (M1 endpoints); Phase 9 adds the action endpoints and `action.proposed` |
| 3 | **Tool registry** (read + write) | `chat_service/tools/registry.py` (`Tool`, `ToolContext`, `ToolResult`) | ✅ Draft v1; write tools declared but only executable via the Phase 9 confirmation flow |
| 4 | **Wallet service** | `chat_service/billing/wallet.py` (`reserve`, `settle`, `release`, `record_shadow`, purchases, refunds, expiry) | ✅ Draft v1; Phase 10 adds overage and caps |
| 5 | **Model client** (OpenAI-compatible) | `chat_service/llm/client.py` (`ModelClient` protocol, `FallbackModelClient`) | ✅ Draft v1 |
| 6 | **Product API adapter** | `chat_service/tools/product.py` (`ProductAPI` protocol, `ENDPOINTS` map) | ⚠️ **Placeholder paths**: replace in Phase 0 |
| 7 | **Auth** | `chat_service/api/auth.py` (`AuthProvider` protocol) | ⚠️ `ProductAuthProvider` `/me` path and fields are placeholders: confirm in Phase 0 |
| 8 | **Payment provider** | `chat_service/billing/payments.py` (`PaymentProvider` protocol) | ⚠️ Dev provider only: add Stripe/Razorpay adapter after decision D9 |

## SSE event contract (v1)

```
message.start  {message_id, conversation_id}
tool.start     {tool, label}
tool.end       {tool, ok}
token          {text}
message.end    {message_id, billable_input_tokens, billable_output_tokens, tokens_charged,
                billing_mode, balance_after, low_balance}
heartbeat      {}
error          {code, message, charged:false}
  codes: chat_disabled | not_found | empty_message | message_too_long | conversation_too_long |
         rate_limited | daily_limit_reached | insufficient_tokens | internal_error
```
Phase 9 adds: `action.proposed {action_id, tool, risk_tier, before, after, warnings, expires_at}`.

## Open items (must close before the gates)
| Item | Owner | Gate |
|---|---|---|
| Map the 5 placeholder product tools to real endpoints; add the rest of the M1 read inventory | WS1 + product team | G1 |
| Confirm the auth mechanism and `/me` equivalent, role names (`org_admin`, staff) | WS1 + product team | G1 |
| Move rate/concurrency limits to Redis for multi-instance deployments | WS5 | G3 |
| Pin the billing tokenizer file (`CHAT_BILLING_TOKENIZER_PATH`) after the model choice | WS4 | G2 |
| Encryption at rest for `chat_tool_calls.result` (storage-level or column-level) | WS5 | G3 |
| Real payment-provider adapter + tax-invoice integration | WS2 + finance | G3 |

## Change log
| Date | Change | Approved by |
|---|---|---|
| 2026-11-22 | v1 drafts created | TODO (tech lead) |

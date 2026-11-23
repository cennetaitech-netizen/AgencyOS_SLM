# Shared Contracts (Gate G1)

These are the interfaces every workstream builds against (COMBINED_RELEASE_PLAN.md §1). **The code is the source of truth.** This page says where each contract lives and what's still open. Any change to a contract needs tech-lead approval and a note in the change log below.

| # | Contract | Source of truth | Status |
|---|---|---|---|
| 1 | **Database schema** (M1 + M2 tables) | `chat_service/db/schema.py` → migrations `0001_initial_schema`, `0002_actions_and_subscription_billing` | ✅ v2 |
| 2 | **Chat API + SSE events** | `chat_service/api/app.py` + `chat_service/api/routes_m2.py` (OpenAPI at `/docs`); events from `chat_service/engine/agent.py` | ✅ v2: M1 + actions (Phase 9) + billing (Phase 10) |
| 3 | **Tool registry** (read + write) | `chat_service/tools/registry.py` (`Tool`, `ToolContext`, `ToolResult`) | ✅ Draft v1; write tools declared but only executable via the Phase 9 confirmation flow |
| 4 | **Wallet service** | `chat_service/billing/wallet.py` (`reserve` incl. overage, `settle`, `release`, `capacity`, `record_shadow`, purchases, refunds, expiry) | ✅ v2 with overage |
| 4b | **Subscriptions, invoices, caps** | `chat_service/billing/subscriptions.py`, `invoices.py`, `caps.py` | ✅ v1 |
| 4c | **Chat actions** | `chat_service/actions/specs.py` (`ActionSpec`), `actions/service.py` (`ActionService`) | ✅ v1: 4 launch actions, pending product-owner approval of the write inventory |
| 5 | **Model client** (OpenAI-compatible) | `chat_service/llm/client.py` (`ModelClient` protocol, `FallbackModelClient`) | ✅ Draft v1 |
| 6 | **Product API adapter** | `chat_service/tools/product.py` (`ProductAPI` protocol, `ENDPOINTS` map); contract [`product_api.openapi.yaml`](product_api.openapi.yaml) and [`PRODUCT_API.md`](PRODUCT_API.md); reference server `tools/mock_product_server.py`; checker `ops/check_product_api.py` | ✅ Contract defined and tested end to end against the reference server. ⚠️ The real product must implement it, or send its own spec to map |
| 7 | **Auth** | `chat_service/api/auth.py` (`AuthProvider` protocol) | ✅ `/me` contract defined (`PRODUCT_API.md` §2) and tested against the reference server. ⚠️ Confirm with the product team |
| 8 | **Payment provider** | `chat_service/billing/payments.py` (`PaymentProvider` protocol) | ⚠️ Dev provider only: add Stripe/Razorpay adapter after decision D9 |

## SSE event contract (v2)

```
message.start  {message_id, conversation_id}
tool.start     {tool, label}
tool.end       {tool, ok}
token          {text}
message.end    {message_id, billable_input_tokens, billable_output_tokens, tokens_charged,
                billing_mode, balance_after, low_balance, alerts: [{scope, period, threshold, used, limit}]}
action.proposed {action_id, tool, label, risk_tier, target_id, before, after, warnings,
                 requires_second_approval, expires_at, undo_of}
action.executed {…action view}            (tier-1 actions only, when the org lets them skip confirmation)
heartbeat      {}
error          {code, message, charged:false}
  codes: chat_disabled | not_found | empty_message | message_too_long | conversation_too_long |
         rate_limited | daily_limit_reached | insufficient_tokens | usage_cap_reached |
         billing_suspended | internal_error
```

## Action endpoints (Phase 9)
`POST /chat/actions/{id}/confirm | cancel | approve | reject | undo` · `GET /chat/actions[?status=]` · `GET /chat/actions/{id}` · `GET/PUT /chat/action-rules` (org admin).
HTTP errors: 403 forbidden · 404 not found · 409 stale / not allowed · 410 expired · 502 the product refused the change.

## Billing endpoints (Phase 10)
`GET /chat/plans` · `GET/POST/PUT /chat/billing/subscription` · `GET/PUT /chat/billing/profile` · `GET /chat/billing/invoices[/{id}[/usage.csv]]` · `GET/PUT /chat/caps`, `DELETE /chat/caps/{id}`.
Staff: `PUT /chat/admin/plans/{id}` · `GET /chat/admin/orgs/{id}/invoices` · `POST /chat/admin/invoices/{id}/approve | void` · `POST /chat/admin/billing/close-cycles` · `GET /chat/admin/actions/pending-approval`.
Webhooks: `paid`, `refunded`, `disputed` (packs); `invoice_paid`, `invoice_payment_failed` (subscriptions).

## Open items (must close before the gates)
| Item | Owner | Gate |
|---|---|---|
| Map the 6 placeholder read tools and 4 write operations to real endpoints; add the rest of the M1 read inventory | WS1 + product team | G1 |
| Confirm the auth mechanism and `/me` equivalent, role names (`org_admin`, staff) | WS1 + product team | G1 |
| Move rate/concurrency limits to Redis for multi-instance deployments | WS5 | G3 |
| Pin the billing tokenizer file (`CHAT_BILLING_TOKENIZER_PATH`) after the model choice | WS4 | G2 |
| Encryption at rest for `chat_tool_calls.result` (storage-level or column-level) | WS5 | G3 |
| Real payment-provider adapter + tax-invoice integration (`InvoiceSink`) | WS2 + finance | G3 |
| Approve the write-action inventory, risk tiers and per-currency minor units | Product owner | G1 |
| Replace `LogNotifier` with product email / in-app notifications (approvals, alerts) | WS1 | G3 |

## Change log
| Date | Change | Approved by |
|---|---|---|
| 2026-11-22 | v1 drafts created | TODO (tech lead) |
| 2026-11-22 | v2: actions (Phase 9), overage/subscriptions/caps/invoices (Phase 10), migration 0002 | TODO (tech lead) |

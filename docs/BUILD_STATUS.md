# Build Status (Combined Release)

Last updated: 2026-11-22 · Service code: `chat_service/` · Tests: **117 passing** (Postgres 16) · Migrations: 0001, 0002

## Done
| Area | Phase | What exists | Tests |
|---|---|---|---|
| Shared contracts | G1 | Schema for all M1 + M2 tables, 2 Alembic migrations, interfaces (`docs/contracts/CONTRACTS.md`) | migration up/down + `alembic check` |
| Wallet & metering | 1 | Free trial, atomic reserve/settle/release, shadow mode, bucket order, expiry, packs, refunds/chargebacks, goodwill, invariant check | 20 (incl. concurrency) |
| Model client | 2 | OpenAI-compatible streaming, tool-call assembly, usage capture, fallback host | 4 |
| Bake-off tool | 2 / G2 | 20 cases, scoring, overhead ratio, markdown report | 3 |
| Tools | 3 | Registry (write tools never executed by it), `media_math`, `validate_ad_copy`, 6 product read tools (placeholder endpoints) + mock product | 20 |
| Chat engine | 4 | Gates, context block, agent loop, refusals, fallback, disconnect settlement, version tracking | 17 |
| **Chat actions** | **9** | 4 actions (budget change, pause, resume, rename) with risk tiers; propose → confirm → execute; re-validation at confirm (permissions, current state, rules); max % / max amount per change; daily spend-change cap; second approver; role restrictions; blocked actions; undo within 24h (via confirmation); tier-1 auto option; global and per-org kill switches; read-only when past due; failed writes never retried; full audit rows; v2 prompt | 20 service + 3 engine + 2 API |
| **Subscription billing** | **10** | Plans; subscribe (allowance + in-advance fee invoice); prorated upgrades; downgrades at renewal; overage reserved atomically up to the spend limit; cycle close with overage by brand; catch-up of missed cycles; dunning; org/workspace/user caps; 50/80/90/100% alerts; invoices with tax, reconciliation, numbering at approval, approval gate, usage CSV, void | 15 service + 3 engine + 2 API |
| API | 4, 6, 9, 10 | All M1, action and billing endpoints; signed webhooks for packs and invoices | 8 |
| Jobs | 1, 4, 9, 10 | Expiry, reversals, retention, invariant alerts, action expiry, cycle close, dunning | 1 |
| CI | 7 | Lint, format, migration check, tests on Postgres | — |

## Blocked on inputs
| Needed | Unblocks |
|---|---|
| **Product repo or API spec** + auth details | Real read/write endpoints (replace placeholders in `tools/product.py`), `ProductAuthProvider`, full write inventory |
| **Product-owner approval** of actions, risk tiers, thresholds | Turning on chat actions for beta orgs |
| **Model host shortlist + keys** | Running the G2 bake-off; pinning the billing tokenizer |
| **Payment provider + invoicing route (D9)**, tax rates per country | Real checkout/webhooks, `InvoiceSink` to the provider or accounting system |
| **Plan catalogue and prices (D1/D3)** | Loading plans via `PUT /chat/admin/plans/{id}` |
| Notification channel (email / in-app) | Replacing `LogNotifier` for approvals and usage alerts |

## Next to build (no external input needed)
1. **Phase 7:** eval harness over the full engine (30+ read and action cases), unit-economics view (overhead ratio, margin), admin usage dashboards.
2. **Phase 5:** chat widget (React): streaming, confirmation cards, undo, usage, caps, billing pages.
3. **Phase 11:** dataset builder and LoRA training scripts (base model from G2).
4. Move rate/concurrency limits to Redis for multi-instance deployments.

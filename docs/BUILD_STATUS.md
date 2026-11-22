# Build Status (Combined Release)

Last updated: 2026-11-22 · Service code: `chat_service/` · Tests: 70 passing (Postgres 16)

## Done (foundation, built without the product code)
| Area | Implementation-guide phase | What exists | Tests |
|---|---|---|---|
| Shared contracts | G1 | Schema for all M1 + M2 tables, Alembic migration, interfaces (see `docs/contracts/CONTRACTS.md`) | migration up/down + `alembic check` |
| Wallet & metering | 1 | Free trial (one per org), atomic reserve/settle/release, shadow mode, daily ceilings data, bucket order, expiry, packs, refunds/chargebacks, goodwill, invariant check | 17 (incl. concurrency) |
| Billing tokenizer | 1 | Pinned-file tokenizer + dev counter (refused in production) | via engine tests |
| Model client | 2 | OpenAI-compatible streaming, tool-call assembly, usage capture, fallback host | 4 |
| Bake-off tool | 2 / G2 | `python -m chat_service.evals.bakeoff`: 20 cases, scores, overhead ratio, markdown report | 3 |
| Tools | 3 | Registry (read-only enforced, write tools never executed directly), `media_math`, `validate_ad_copy`, 5 product read tools (placeholder endpoints) + mock product data | 20 |
| Chat engine | 4 | Gates (kill switch, org flag, length, turns, global ceiling, rate, concurrency), context block, agent loop, refusal handling, fallback, disconnect settlement, version tracking, tool-result storage | 17 |
| API | 4, 6 | Conversations, SSE messages (heartbeats, no buffering), feedback, wallet, usage, packs, checkout, signed webhook, staff admin (org settings, goodwill) | 4 (full flows) |
| Jobs | 1, 4 | Reservation/bucket expiry, pending reversals, retention, invariant alerts | via wallet tests |
| CI | 7 | Lint, migration check, tests on Postgres (`.github/workflows/ci.yml`) | — |

## Blocked on inputs (from Section 1 of the implementation guide)
| Needed | Unblocks |
|---|---|
| **Product repo or API spec** + auth details | Phase 0 discovery, real product tools (replace placeholders), `ProductAuthProvider`, Phase 9 write inventory |
| **Model host shortlist + API keys** | Running the G2 bake-off (`evals/models.example.json`), billing tokenizer pin |
| **Payment provider choice (D9)** + test keys | Real checkout/webhook adapter, tax invoices |
| Staging environment + test ad accounts | Integration tests, Phase 9 action testing |

## Next to build (no external input needed)
1. **Phase 9 core:** proposal → confirm/cancel → execute flow, rules engine, second approval, undo, kill switch (against the mock product's write endpoints).
2. **Phase 10 core:** plans/subscriptions, monthly allowance buckets, overage up to the spend limit, org/workspace/user caps, cycle-close invoice generation, reconciliation.
3. **Phase 7:** eval harness over the full engine (30+ cases), unit-economics query, admin usage views.
4. **Phase 5:** chat widget (React) against the SSE contract.
5. **Phase 11:** dataset builder and LoRA training scripts (need the base model choice from G2).

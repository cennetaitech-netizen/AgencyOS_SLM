# Build Status (Combined Release)

Last updated: 2026-11-23 · Backend tests: **177 passing** (Postgres 16 + Redis) · Widget tests: **19 passing** · Migrations: 0001–0005 · CI: tests, evals, security scans, container build + full-stack smoke test

## Done
| Area | Phase | What exists | Tests |
|---|---|---|---|
| Shared contracts | G1 | Schema for all tables, 3 migrations, interfaces (`docs/contracts/CONTRACTS.md`) | migration up/down + `alembic check` |
| Wallet & metering | 1 | Free trial, atomic reserve/settle/release, shadow mode, overage, expiry, packs, refunds, goodwill, invariant check | 22 |
| Model client & bake-off | 2 | OpenAI-compatible streaming, fallback host; G2 bake-off with overhead ratio | 7 |
| Tools | 3 | Registry, `media_math`, `validate_ad_copy`, 6 product read tools (placeholder endpoints) + mock product | 20 |
| Chat engine | 4 | Gates, context block, agent loop, refusals, disconnect settlement, numeric grounding flags, structured logs, analytics, per-org model routing | 20 |
| **Chat window** | **5** | React widget: streaming chat, conversations, confirmation cards with Undo, token counts, balance, banners, blocked states, feedback, safe markdown, disclaimer, dark mode, phone layout; billing page; **admin settings** (second-approver queue, chat action rules, usage limits, plan and overage limit, billing details, training consent); demo app. Checked in a real browser against the running service | 19 (Vitest) + browser screenshots |
| Payments | 6 | Dev provider, signed webhooks for packs and invoices | in API tests |
| **Ops & quality** | **7** | 32-case full-engine eval with CI gate (`eval.yml`), numeric grounding check, unit economics (overhead ratio, margin, alerts), quality review, analytics events, staff endpoints | 19 |
| Chat actions | 9 | Propose → confirm → execute with all safety rules, undo, kill switches | 25 |
| Subscription billing | 10 | Plans, overage to spend limit, cycle close, proration, dunning, caps, alerts, invoices | 20 |
| **Own SLM** | **11** | Dataset builder (consent, scrub, corrections, eval exclusion, DPO pairs), synthetic generator, short fine-tune prompt, LoRA SFT + DPO scripts (CPU smoke-tested with a tiny model), release gate, canary rollout with automatic rollback, runbook, model card | 13 + training smoke run |
| CI | 7 | Lint, format, migrations, backend tests, widget typecheck/tests/build; model eval on relevant PRs; dependency and secret scans | — |
| **Launch hardening** | **12** | Production start-up guard, security headers/CORS/size limit, audit log, encryption at rest with key rotation, Redis-shared limits, metrics + readiness + 9 SLO alerts, cross-tenant security suite, load test at 3× and failure drill (money checks pass), Dockerfile + compose, runbooks. See [LAUNCH_READINESS.md](LAUNCH_READINESS.md) | 16 + load/drill runs |
| **Prefix caching & prompt v3** | **12** | Cache-friendly prompt layout (static `system_v3` prompts and tool schemas, `[Context]` line on the latest message) shared by engine, evals and training export; prefix 22–24% shorter; cached tokens tracked, priced and alerted on; host setup in `ops/MODEL_HOST.md`. Uncached overhead about 26× → about 3× (simulated and load-tested) | 9 + A/B, simulation and live run |
| **Monitoring** | **12** | Grafana dashboard (21 panels in 5 sections: SLO headline, traffic and latency, tokens and margin, tools and actions, resilience, firing alerts), Prometheus config, provisioning, compose `monitoring` profile, query checker. Every query verified against live data | 4 (metric/label drift, generator, action outcome counts) + live run |

## Blocked on inputs
| Needed | Unblocks |
|---|---|
| **Product repo or API spec** + auth details | Real read/write endpoints (`tools/product.py`), `ProductAuthProvider`, full write inventory |
| **Product-owner approval** of actions, risk tiers, thresholds | Turning on chat actions for beta orgs |
| **Model host + keys** | G2 bake-off, eval CI (`EVAL_MODEL_*` secrets), billing tokenizer pin |
| **Base model choice + GPU + teacher model** | Real SFT/DPO training runs (Phase 11) |
| **Payment provider + invoicing route (D9)**, tax rates | Real checkout, `InvoiceSink` |
| **Plans and prices (D1/D3)**, host and list prices | Plan catalogue; unit-economics numbers |
| Notification channel, analytics tool | Replacing `LogNotifier` / `LogAnalyticsSink` |
| The product's design system | Restyling the widget via `--aos-*` variables; embedding it in the product shell |

## Remaining engineering (no external input needed)
None. The container image and the full compose stack (with monitoring) are built and smoke-tested in CI (`container.yml`). Everything left needs one of the inputs above.

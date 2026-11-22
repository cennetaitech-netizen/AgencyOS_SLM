# Launch Readiness (Phase 12)

Status as of 2026-11-22. ✅ = done and verified · 🟡 = built, needs a real environment or a person · ❌ = not started.

## 1. Phase 12 checklist

| Item | Status | Evidence |
|---|---|---|
| **Production safety guard**: the service refuses to start with dev login, mock product, dev payments, default webhook secret, no billing tokenizer, no encryption key, no Redis, no CORS allow-list, no metrics token, or the local database | ✅ | `config.production_problems`, tests in `tests/test_hardening.py`. Today only "real payment provider (D9)" stands between a fully configured deployment and start-up |
| Security headers, request IDs, 413 for oversized requests, CORS allow-list, API docs off in production | ✅ | `chat_service/api/security.py`, tests |
| Audit log of staff and org-admin changes (who, what, when, request ID); staff-only viewer | ✅ | `chat_audit_log` (migration 0004), `GET /chat/admin/audit` |
| Encryption at rest for stored tool results (client campaign data), with key rotation | ✅ | `chat_service/crypto.py`; tests: data unreadable in the DB, rotation, old key retired |
| Shared rate and concurrency limits across instances (Redis), fail-open if Redis is down | ✅ | `chat_service/engine/limits.py`; tests with real Redis and two simulated servers |
| Metrics (`/metrics`, token-protected), readiness (`/readyz`), liveness (`/healthz`) | ✅ | `chat_service/metrics.py`, tests |
| SLO alert rules | ✅ | `ops/prometheus/alerts.yml` (9 rules, every metric name checked against the code) |
| Automated security tests: every route needs login, staff routes refuse org admins, admin routes refuse members, other orgs can't reach any record by ID | ✅ | `tests/test_security.py` (all 56 routes discovered automatically; 52 non-public routes checked for login, 18 staff routes checked against org admins) |
| Dependency vulnerability scans (Python + widget) and secret scanning in CI | ✅ | `.github/workflows/security.yml`; today: **0 known vulnerabilities** in both |
| Load test at 3× assumed pilot peak, 2 instances | ✅ | §3 |
| Failure drill (model errors + Redis outage under load) | ✅ | §3, including one finding fixed |
| Container image and local full stack | 🟡 | `Dockerfile`, `docker-compose.yml` written; **not built here** (no Docker daemon in this environment). Build and smoke-test in CI or staging |
| **External penetration test** | 🟡 | Scope in §5. Needs a vendor (book for weeks 9–10) |
| **One full real billing cycle** with pilot orgs, reconciled with zero discrepancies | 🟡 | Mechanism and reconciliation built and tested; needs real pilot orgs and the payment provider |
| Status page, on-call rotation, alert routing (PagerDuty/Opsgenie) | 🟡 | Rules and runbooks ready; tooling and people to assign |
| Dashboards | 🟡 | Metrics exist; build a Grafana dashboard with the panels in §2 |
| Go/no-go meeting | ❌ | `docs/business/GOVERNANCE_AND_PRICING.md` §2 (decision D12) |

## 2. SLOs and what to watch

| SLO / signal | Target | Metric | Alert |
|---|---|---|---|
| API availability | ≥ 99.5% (30 days) | `aos_http_requests_total` 5xx ratio | `AosAvailabilityBudgetBurn` (page) |
| Reply failures (not charged) | < 2% | `aos_chat_messages_total{status=~"model_error\|internal_error"}` | `AosChatReplyErrors` (page) |
| p95 time to first token | < 1.5 s | `aos_chat_time_to_first_token_seconds` | `AosTimeToFirstTokenSlow` |
| Confirmed actions failing | ≤ 3 per 30 min | `aos_chat_actions_total{outcome="failed"}` | `AosActionFailures` (page) |
| Answers with unverifiable figures | < 5% | `aos_chat_ungrounded_answers_total` | `AosUngroundedAnswersHigh` |
| Hidden cost per message (margin) | overhead ratio ≤ 20× | host ÷ billable tokens | `AosOverheadRatioHigh` |
| Fallback model host in use | ~0 | `aos_model_fallback_total` | `AosModelFallbackActive` |
| Redis (limits) reachable | always | `aos_limiter_fail_open_total` | `AosLimiterFailOpen` |

Dashboard panels: messages/s by outcome · p50/p95 TTFT and reply time · error codes · tokens billed vs host tokens · tool calls by tool · actions by outcome · fallback and fail-open counters · HTTP 5xx by route.

## 3. Load test and failure drill (2026-11-22, this environment)

Setup: 2 API instances sharing Postgres 16 and Redis; stand-in model host (`ops/fake_model_host.py`) with 150 ms to first token, 20 chunks × 25 ms, a read-tool call on 50% of messages. 25 orgs × 3 users = **75 virtual users**, i.e. up to 75 concurrent replies, 3× an assumed pilot peak of ~20–25. `ops/loadtest.py`, then the money checks in `ops/verify_after_load.py`.

| Run | Messages | Throughput | Success | p95 first token | p95 full reply | Money checks |
|---|---|---|---|---|---|---|
| Baseline, 60 s | 2,085 | 33 msg/s | **99.95%** (1 client connection drop, not charged) | **977 ms** | 2.1 s | ✅ all 25 wallets balanced; 0 reservations left; 148,426 tokens debited = 148,426 charged; 2,084 charges = 2,084 successes |
| Drill: 5% model-host failures + Redis stopped for 10 s | 1,242 | 33 msg/s | 92.6% (82 failed replies, as injected) | 1.2 s | 2.3 s | ✅ **none of the 82 failures charged**; wallets balanced; 0 reservations left |
| Drill, after the fix below | 1,217 | 33 msg/s | 93.6% | 1.4 s | 2.8 s | ✅ same |

**What these numbers mean, and don't:** the stand-in model is fast and cheap, so they measure *our* service (gates, wallet, database, streaming, limits), not a real model. Re-run `ops/loadtest.py` in staging against the chosen model host to measure real latency and to size GPUs.

**Finding fixed:** in the first drill, Redis restarted from an old snapshot and brought back stale concurrency slots, so a few users were briefly told "too many replies in progress". Fix: the limits Redis must run **without persistence** (set in `docker-compose.yml`), and slot leases were shortened to 3 minutes. A later run still showed 4 brief refusals once; they weren't reproducible with logging on. Refusals are always uncharged, and log lines now name the limit that refused, for staging follow-up.

## 4. Runbooks

<a id="rb-api-errors"></a>**API errors / instance down.** Check `/readyz` on each instance (database and Redis status) and the logs for the request ID in the `X-Request-ID` header. If the database is down, the service returns 503 from `/readyz` and the load balancer stops routing. Restart unhealthy instances. If errors started after a deploy, roll back the image.

<a id="rb-model-host"></a>**Model host failing or slow.** Replies that fail are never charged. Check the host's status, then `aos_model_fallback_total`: the fallback host takes over automatically if configured. If both fail: `CHAT_ENABLED_GLOBAL=false`, post on the status page, and re-enable when recovered. If a new model version is involved: `POST /chat/admin/model-rollout/rollback`.

<a id="rb-latency"></a>**Slow first token.** Compare TTFT by model (canary vs production), host queue time, and tool latency (`aos_chat_tool_calls_total` plus product API latency). Mitigations: scale model replicas, lower `CHAT_MAX_OUTPUT_TOKENS`, pause a canary.

<a id="rb-redis"></a>**Redis unreachable.** Chat keeps working (limits fail open; billing is unaffected because the wallet is atomic in Postgres). Restore Redis **without persistence**. If it was restored from a snapshot, delete the `aos:streams:*` keys.

<a id="rb-actions"></a>**Confirmed actions failing.** Turn chat actions off at once: `CHAT_WRITE_ACTIONS_ENABLED_GLOBAL=false` (chat stays read-only). Inspect `GET /chat/admin/actions/pending-approval` and the `chat_actions` rows (status `failed`, `error`). Failed actions are never retried automatically. Contact affected clients and verify each item in the product. Treat it as Sev 1 if a change executed differently from what was displayed.

<a id="rb-quality"></a>**Unverifiable figures rising.** Open `GET /chat/admin/quality-review`, check the flagged answers' tool data, and compare by model and prompt version. Roll back a canary or prompt change if one is involved. Add failing cases to `evals/chat_eval_cases.json`.

<a id="rb-margin"></a>**Overhead ratio too high.** Check `GET /chat/admin/economics` by org (long conversations, heavy tool results). Levers: lower `CHAT_TOOL_RESULT_MAX_CHARS` or `CHAT_HISTORY_TURNS`, prompt caching on the host, shorter prompt or fine-tuned model (Phase 11).

**Other routine operations** (also in `docs/business/SUPPORT_PILOT_LAUNCH_KIT.md`): goodwill credit, disabling chat for one org, pinning an org to a model, invoice approval, key rotation (`python -m chat_service.crypto rotate`).

**Game day (before go-live, then quarterly):**
1. Start the stand-in host with `--fail-rate 0.05` and `--stall-ms 2000`.
2. Run `ops/loadtest.py` and stop Redis partway through.
3. Trigger the kill switches.
4. Confirm the alerts fire and the runbooks work.
5. Confirm `ops/verify_after_load.py` passes.

## 5. External penetration test: scope to hand the vendor
- **Authentication and tenant isolation** on every `/chat/*` route (our automated suite covers the known paths; ask them to try beyond it).
- **Chat actions:** can a prompt, a tool result, a pasted document or an ad-platform field cause a change without a human confirmation? Can confirmation be replayed or forged across users or orgs?
- **Billing:** can tokens be consumed without being charged, or charged twice, through concurrency, disconnects or webhook replay? Can a forged webhook credit tokens?
- **Streaming endpoint** abuse (slowloris, huge messages, many parallel streams), plus the rate and concurrency limits.
- **Widget:** XSS through model output (markdown), link handling, clickjacking.
- **Staff endpoints:** privilege escalation from org admin.
- **Data at rest:** encrypted tool results; key handling.

## 6. Deployment topology (recommended)
Load balancer (TLS, `/readyz` health check, idle timeout above the longest reply, no response buffering on the stream route) → **2+ API containers** (4 workers each) → Postgres (managed, point-in-time recovery, daily backup restore test) · Redis (managed or container, **no persistence**) · 1 **jobs** container (`python -m chat_service.jobs`, single replica) · model host (managed or vLLM) + fallback host · Prometheus + alerting. Secrets (DB URL, model and payment keys, webhook secret, encryption keys, metrics token) come from the secrets manager.

## 7. What still needs people before launch
1. Payment provider and invoicing route (D9): the last item blocking production start-up.
2. Pen-test vendor booked; findings fixed.
3. On-call rotation, alert routing, status page, Grafana dashboard.
4. Container build verified in CI or staging; staging environment with the real product API and model host; load test re-run there.
5. One full real billing cycle with pilot orgs, reconciled.
6. Go/no-go meeting (all owners in the RACI).

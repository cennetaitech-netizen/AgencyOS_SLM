# AgencyOS — Media Campaign SLM

A chat assistant for the AgencyOS product, focused only on media campaigns. Clients chat with **our own small language model (SLM)**, which reads (and, from Phase 9, acts on, with confirmation) their campaign data. They're **billed by token consumption** with free and paid caps.

**Status:** foundation built and tested. See [docs/BUILD_STATUS.md](docs/BUILD_STATUS.md) for what's done, what's blocked on inputs, and what's next.

---

## Run it locally

Requirements: Python 3.12+, PostgreSQL 15+, and an OpenAI-compatible model endpoint (e.g. a local Ollama or vLLM, or a managed host).

```bash
python -m venv .venv && . .venv/bin/activate
```

```bash
pip install -e ".[dev]"
```

```bash
createdb chat_dev && cp .env.example .env
```

```bash
alembic upgrade head
```

```bash
uvicorn chat_service.api.app:create_app --factory --reload --port 8090
```

API docs: http://localhost:8090/docs. In dev mode, requests authenticate with the headers `X-Dev-User-Id`, `X-Dev-Org-Id` and `X-Dev-Roles`. An `agencyos_staff` user enables an org with `PUT /chat/admin/orgs/{org_id}/settings`.

Scheduled jobs (expiry, refunds, retention, wallet checks), every 5 minutes:

```bash
python -m chat_service.jobs
```

Tests (need a `chat_test` database; see `tests/conftest.py`):

```bash
pytest -q
```

Model bake-off for gate G2: copy `evals/models.example.json`, fill in the candidate hosts, then:

```bash
python -m chat_service.evals.bakeoff --models evals/models.json
```

## Code map
| Path | What |
|---|---|
| `chat_service/db/schema.py` | Database schema: the shared contract (M1 + M2 tables) |
| `chat_service/billing/wallet.py` | Token wallet: free trial, packs, reserve/settle/release, shadow mode, expiry, refunds |
| `chat_service/billing/tokenizer.py` | Fixed, versioned billable-token counting |
| `chat_service/billing/payments.py` | Payment provider interface (dev provider; Stripe/Razorpay later) |
| `chat_service/engine/agent.py` | Chat engine: gates → reserve → model ⇄ tools loop → settle |
| `chat_service/actions/` | Chat actions: specs (tiers, preview, undo) and the propose → confirm → execute service |
| `chat_service/billing/subscriptions.py`, `invoices.py`, `caps.py` | Plans, overage, cycle close, dunning; invoices; caps and alerts |
| `chat_service/engine/prompts/` | Versioned system prompts |
| `chat_service/tools/` | Tool registry, `media_math`, `validate_ad_copy`, product read tools (+ mock product) |
| `chat_service/llm/` | OpenAI-compatible streaming client with fallback; scripted fake model |
| `chat_service/api/` | FastAPI app, auth providers |
| `chat_service/evals/bakeoff.py` | Gate G2 model bake-off |
| `chat_service/analytics/` | Unit economics, quality review, analytics events |
| `chat_service/evals/` | G2 bake-off and the full-engine eval (CI gate) |
| `chat_service/ml/` | Training data builder, synthetic generator, release gate |
| `chat_service/llm/router.py` | Model rollout: canary, per-org pinning, automatic rollback |
| `ml/` | LoRA SFT/DPO training scripts, configs, runbook ([ml/README.md](ml/README.md)) |
| `widget/` | React chat window and billing page ([widget/README.md](widget/README.md)) |
| `ops/` | Load test, failure-drill model host, money checks, Prometheus alert rules |
| `Dockerfile`, `docker-compose.yml` | Container image and local full stack |
| `migrations/` | Alembic migrations |

## Planning documents
- **Launch readiness (Phase 12):** [docs/LAUNCH_READINESS.md](docs/LAUNCH_READINESS.md) · **Build status:** [docs/BUILD_STATUS.md](docs/BUILD_STATUS.md) · **Shared contracts:** [docs/contracts/CONTRACTS.md](docs/contracts/CONTRACTS.md)
- **Combined release plan (M1 + M2 together, ~14–16 weeks):** [docs/COMBINED_RELEASE_PLAN.md](docs/COMBINED_RELEASE_PLAN.md)
- **Implementation guide (Phases 0–12):** [docs/IMPLEMENTATION_GUIDE.md](docs/IMPLEMENTATION_GUIDE.md)
- **Business readiness drafts:** [governance & pricing](docs/business/GOVERNANCE_AND_PRICING.md) · [legal & finance](docs/business/LEGAL_AND_FINANCE_DRAFTS.md) · [support, pilot & launch](docs/business/SUPPORT_PILOT_LAUNCH_KIT.md)
- **Gap review:** [docs/GAP_REVIEW.md](docs/GAP_REVIEW.md)
- Background: [lean MVP](docs/LEAN_MVP.md) · [chat engine design](docs/CHAT_ENGINE_SLM_PLAN.md) · [prerequisites & guardrails](docs/BUILD_READINESS.md) · [token caps & invoicing](docs/TOKEN_CAPS_AND_INVOICING.md) · [original blueprint](docs/PRODUCT_BLUEPRINT.md)

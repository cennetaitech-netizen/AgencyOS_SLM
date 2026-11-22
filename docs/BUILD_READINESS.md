# AgencyOS Chat SLM — Prerequisites, Guardrails and Challenges

> **Important:** the plan documents in `docs/` are **specifications, not executable code.** Running them through a tool won't produce an SLM. Building one takes **data, code, GPUs and people**. This document lists what must be in place, the guardrails we must build, and the challenges to expect.

---

## 1. What actually has to be built (the workstreams)

| # | Workstream | Output | Built from |
|---|---|---|---|
| 1 | **Tool layer** | Every AgencyOS action exposed as a callable tool (JSON schema) | Existing product API |
| 2 | **Training data** | 10k–30k validated chat conversations + refusals + eval set | Product logs, SMEs, teacher-model generation, sandbox validation |
| 3 | **SLM training pipeline** | Fine-tuned, quantised media-campaign model + eval reports | Open-weight base model, training code, GPUs |
| 4 | **Model serving** | Scalable inference endpoint (vLLM) | GPU infra |
| 5 | **Chat engine** | Gateway, scope guard, router, agent loop, confirmations, memory | Backend code |
| 6 | **Chat UI** | Widget with streaming, rich blocks, confirm cards, token display | Frontend code |
| 7 | **Metering, caps and invoicing** | Ledger, wallets, free/paid caps, invoices | Backend + billing provider (see [TOKEN_CAPS_AND_INVOICING.md](TOKEN_CAPS_AND_INVOICING.md)) |
| 8 | **Guardrails and safety** | Layered checks (Section 3) | Backend + model training |
| 9 | **Ops** | Monitoring, cost dashboards, rollback, kill switches | DevOps |

---

## 2. Prerequisites (must be in place before / during build)

### 2.1 Business and legal
- [ ] **Billing policy signed off:** what counts as a billable token, input/output rates, free tier size, paid plan allowances, overage rules, credit expiry, refund policy.
- [ ] **Updated client contracts / ToS:** token-based charges, liability for actions the chat executes on live campaigns (always user-confirmed), AI disclosure, data use for training (opt-in/opt-out).
- [ ] **Data rights:** written permission before using any client conversation or campaign data for training. Default to anonymised, aggregated data plus synthetic data.
- [ ] **Base model licence review:** commercial use, resale and on-prem redistribution allowed.
- [ ] **Teacher-model terms:** confirm the provider used to generate synthetic training data allows its outputs to be used for training.
- [ ] **Tax registration** for digital services in each market you invoice (GST/VAT etc.) and e-invoicing obligations where they apply.

### 2.2 Product and engineering
- [ ] **API coverage:** every action the chat should perform is available via an API (ideally with an **OpenAPI spec**). *Anything not in the API can't be done by chat.*
- [ ] **Sandbox / staging environment** with test data and **test ad accounts** (e.g., Google Ads test accounts, Meta sandbox ad accounts) to safely run generated tool calls.
- [ ] **RBAC** (role-based permissions) enforced in the API, not just the UI.
- [ ] **Idempotent write endpoints** (request IDs), so retries never double a budget change.
- [ ] **Audit logging** for all write actions.
- [ ] **Billing system** that supports metered usage and prepaid credits (e.g., Stripe, Chargebee, Razorpay, or in-house with a ledger).

### 2.3 Data
- [ ] Inventory of product actions (≈30–80 tools), each marked **read** or **write/spend**.
- [ ] Real user language: support tickets, help-centre searches, onboarding call notes, sales demo scripts.
- [ ] Media SME time: roughly **0.5 FTE for 2–3 months** to write and review examples and grade outputs.
- [ ] A frozen **evaluation set** of 500–1,000 conversations with expected tool calls and answers.

### 2.4 Infrastructure
- [ ] **Training GPU:** 1× 80 GB-class GPU (A100/H100), rented by the hour, is enough for LoRA fine-tuning a 7–8B model.
- [ ] **Serving GPU:** 1–2 inference GPUs (L40S/A100/H100) or a managed provider that hosts fine-tuned models; autoscaling as usage grows.
- [ ] Cloud account, PostgreSQL, Redis, object storage, a vector index (pgvector), secrets manager.
- [ ] Observability: LLM tracing (Langfuse/OpenTelemetry), error tracking, GPU utilisation and cost dashboards.

### 2.5 People and skills
| Role | Needed for | Skills |
|---|---|---|
| ML engineer | Data pipeline, fine-tuning, evals, serving | Python, PyTorch, Hugging Face (TRL/PEFT), vLLM |
| 2 × backend engineers | Tool layer, chat engine, metering/caps/invoicing | Python/Node, APIs, Postgres, queues |
| Frontend engineer | Chat widget, usage dashboards, cap settings | React/TypeScript, streaming |
| Media SME (part-time) | Training data quality, evals, refusal rules | Media planning/buying |
| QA | Sandbox testing, billing reconciliation tests | Test automation |

---

## 3. Guardrails (layered: no single layer is trusted alone)

### Layer 1: Before the model is called (gateway)
| Guardrail | Rule |
|---|---|
| Authentication | Valid AgencyOS session; tenant resolved |
| **Token cap check** | Free/paid balance and user/workspace caps checked; **tokens reserved** before the call (see the caps doc) |
| Rate limits | e.g., requests per minute per user and per org; concurrent chats per org |
| Input size limits | Max characters per message, max file size/pages, allowed file types |
| **Scope guard** | Classifier: media/campaign/product topics only. Off-topic → short polite refusal; minimal or zero billing |
| Abuse / jailbreak detection | Block attempts to extract system prompts, other tenants' data, or bypass rules |
| PII detection | Mask personal data in prompts and logs where not needed |

### Layer 2: The model itself
| Guardrail | Rule |
|---|---|
| Trained behaviour | Fine-tuned on refusals, confirmation-before-write, concise answers |
| **Max output tokens** per reply | Hard limit (e.g., 1,500), and never more than the remaining balance |
| **Max tool-call steps** per message | e.g., 8. Stops runaway loops from burning tokens |
| Structured output | JSON-schema-constrained decoding for tool calls; invalid calls rejected, not executed |
| Context limit | Old turns summarised; maximum context size enforced |

### Layer 3: Actions and tools
| Guardrail | Rule |
|---|---|
| **User's own permissions** | The agent acts as the user (RBAC). A viewer can't change budgets through chat |
| **Confirmation for every write/spend** | Confirm card showing the exact change (before → after); nothing executes without a click |
| **Business-rule validation** | e.g., a budget change above X% or above an amount threshold → extra approval; no launch without tracking set; dates within flight |
| **Spend safety limits** | Per-org daily limit on chat-initiated spend changes; two-person approval for large changes (configurable) |
| Idempotency | Every write carries an idempotency key |
| Dry-run / preview | The tool shows the predicted effect before execution where possible |
| Undo | Reversible actions keep an undo option for a time window |
| **Global kill switch** | Ops can disable all chat write actions instantly (read-only mode) |

### Layer 4: Before the answer reaches the user
| Guardrail | Rule |
|---|---|
| **Numeric grounding** | Every number in the answer must come from a tool result; otherwise the answer is regenerated or flagged |
| ID grounding | Campaign/ad set/account IDs must exist in tool results (no invented IDs) |
| Ad copy compliance | Platform character limits, prohibited claims, regulated categories (health, finance, alcohol, gambling, political), local advertising codes |
| Tenant isolation | Retrieval and memory scoped to the tenant; output scanned for other tenants' identifiers |
| Prompt-injection defence | Text from uploaded briefs, web pages and ad platforms (e.g., an ad name containing instructions) is treated as **data**, never as commands |

### Layer 5: Billing guardrails
| Guardrail | Rule |
|---|---|
| Hard caps can't be exceeded | Output is capped at the remaining balance; reservation/settlement prevents race conditions |
| Don't bill our failures | Model errors, timeouts, tool failures caused by us, and runaway loops are not billed (or are auto-credited) |
| Pre-flight cost warning | "This report will use about 40k tokens (≈ X credits). Continue?" for expensive operations |
| Alerts | 50 / 80 / 90 / 100% of caps → in-app + email to admins |
| Anomaly detection | Sudden spikes in an org's usage → alert + optional auto-pause |

### Layer 6: Operations
- Model version pinning per tenant, staged rollout, instant rollback.
- Weekly review of flagged conversations; regression tests on the eval set before every release.
- Full audit trail: who asked what, what the model proposed, who confirmed, what executed, what was billed.

---

## 4. Challenges to expect (and how to handle them)

| # | Challenge | Why it's hard | Mitigation |
|---|---|---|---|
| 1 | **Training data is the real work** | Training an 8B model takes hours; producing 10k–30k *correct* conversations takes weeks | Generate with a teacher model, auto-validate in the sandbox, SME review samples; start with the top 20 workflows |
| 2 | **SLM accuracy with many tools** | Small models pick wrong tools or arguments when given 60+ options | Intent router loads only one skill group per turn; constrained JSON; strong eval gate |
| 3 | **Liability for live-campaign actions** | A wrong budget change costs the client real money | Mandatory confirmations, thresholds, spend limits, undo, contractual terms |
| 4 | **Clients don't understand "tokens"** | Marketers and finance teams think in money, not tokens | Show cost in currency or credits next to tokens; pre-flight estimates; simple plan allowances |
| 5 | **Billing disputes and trust** | Clients can't independently verify token counts | Per-message counts, published reference tokenizer, downloadable usage ledger behind every invoice |
| 6 | **Non-English languages cost more tokens** | Many tokenizers split non-Latin scripts (e.g., Hindi, Arabic, Thai) into many more tokens, so the same request costs more | Choose a billing tokenizer that's efficient for your markets, or apply per-language normalisation; disclose it clearly |
| 7 | **Incentive conflict** | A better, more concise model earns less per task | Plans with included tokens; credits; fixed-price premium actions |
| 8 | **Real-time cap enforcement with streaming** | Output length is unknown before generation; parallel chats drain the same wallet | Reserve → stream → settle pattern; `max_tokens` = min(limit, remaining balance) |
| 9 | **Free-tier abuse** | Multiple sign-ups, bots, scripted use | Verified business email / phone, one free grant per org/domain, rate limits, optional card on file |
| 10 | **GPU cost vs utilisation** | Idle GPUs still cost money; usage peaks (Monday mornings, month-end reporting) | Managed hosting first; autoscaling; reserved capacity once usage is steady; queue for non-urgent jobs |
| 11 | **Prompt injection from ad data and briefs** | Campaign names, landing pages or uploaded briefs can contain malicious text | Treat as data, confirmations on writes, output checks |
| 12 | **Ad platform changes** | APIs, ad formats and policies change frequently | Tools absorb most changes; monthly data refresh and retrain; regression tests |
| 13 | **Model upgrades change behaviour and bills** | New versions answer differently and may tokenize differently | Fixed billing tokenizer; staged rollout; per-tenant version pinning |
| 14 | **Invoicing compliance** | Tax on digital services, e-invoicing rules, multi-currency, prepaid-credit accounting | Use a billing provider that handles tax; treat prepaid credits as deferred revenue; finance sign-off |
| 15 | **Hiring the right ML skills** | Fine-tuning + serving + evals is specialised | One strong ML engineer + managed tooling; external consultants for the initial setup |

# AgencyOS — Chat Engine on a Media-Campaign SLM, Billed by Token

**Scope:** The AgencyOS product already exists. This plan adds a **chat engine** so clients can do *every* product activity by chatting. The chat runs on our **own small language model (SLM)**, specialised **only for media campaigns**, and clients are **charged by token consumption**.

> This supersedes the build-from-scratch sections of [PRODUCT_BLUEPRINT.md](PRODUCT_BLUEPRINT.md). That document stays as background for data, compliance and go-to-market.

---

## 1. Why an SLM is the right call for this business model

With token-based pricing, **our gross margin = (price per token − cost per token) × tokens**. A third-party LLM API puts a supplier's price, and its price changes, inside our cost line.

| | Third-party frontier LLM API | Our own media-campaign SLM |
|---|---|---|
| Cost per 1M tokens to us | Set by the vendor; can change any time | GPU-hours ÷ throughput. Typically **10–50x lower** at decent utilisation |
| Margin control | Low | **Full**: we set the price per token |
| Data stays with us / client | Data goes to the vendor | Yes. Can also run in the client's own cloud |
| Domain focus | General; wanders off-topic | Trained to do **media campaigns only** and decline the rest |
| Latency | Network + queue | Low. Small model, our own servers |
| Quality on open-ended reasoning | Higher | Lower. Fine for **tool-driven product actions**, which is most of what the chat does |

**Why an SLM is enough:** most chat turns are *"understand the request → call the right product function with the right arguments → explain the result"*. That is intent detection, tool calling and summarisation. A fine-tuned 3–14B model does this very well. The product's own code does the heavy lifting: plans, forecasts, numbers.

---

## 2. Target architecture

```
 Client (web chat widget in AgencyOS · optional Slack / Teams / WhatsApp)
        │  streaming (SSE / WebSocket)
        ▼
┌───────────────────────────── CHAT ENGINE ─────────────────────────────────┐
│ 1. Gateway: auth (client's existing AgencyOS login), tenant, rate limits,  │
│    token-balance check (block or warn if wallet/quota empty)               │
│ 2. Scope guard: tiny classifier. Off-topic (not media/campaign) →         │
│    polite refusal, minimal tokens                                          │
│ 3. Intent router: picks a skill group (Planning · Campaign setup ·         │
│    Budgets & bids · Creative/copy · Reporting · Optimisation · Admin)      │
│    and loads ONLY that group's tools, keeping prompts short and accurate   │
│ 4. Agent loop: SLM ⇄ tools until answer; write actions → confirmation card │
│ 5. Context manager: conversation memory, summarise old turns, pin          │
│    active client/campaign, retrieval over the client's docs (RAG)          │
│ 6. Metering: counts tokens on every model call → usage events → ledger     │
└────────────┬──────────────────────────┬──────────────────────┬────────────┘
             │                          │                      │
     ┌───────▼────────┐        ┌────────▼─────────┐    ┌──────▼───────────────┐
     │ SLM serving    │        │ Tool layer        │    │ Billing & usage      │
     │ vLLM / SGLang  │        │ = EXISTING        │    │ Usage ledger (PG)    │
     │ base SLM +     │        │ AgencyOS APIs     │    │ Wallet / credits     │
     │ LoRA adapters  │        │ wrapped as tools  │    │ Stripe metered /     │
     │ guided JSON    │        │ (RBAC of the user)│    │ prepaid invoicing    │
     └────────────────┘        └──────────────────┘    │ Usage dashboard      │
                                                       └──────────────────────┘
```

### Core rules
1. **Every product action becomes a tool.** Generate tool schemas from the existing API (OpenAPI spec → JSON-schema tools). The chat can't do anything the API can't, and it can do everything the API can.
2. **The agent acts as the logged-in user.** It uses the user's own permissions (RBAC), never a super-user. Every action is audit-logged with `conversation_id` and `message_id`.
3. **Read actions run immediately. Write and spend actions need confirmation.** Creating or launching campaigns, changing budgets or bids, and pausing all show a confirmation card ("Increase Meta budget for Campaign X from $5,000 to $7,000? [Confirm] [Edit] [Cancel]"). Writes are idempotent (request IDs), so a retry never doubles a budget.
4. **Numbers come from tools, never from the model.** Forecasts, KPIs and budget splits are returned by the product. The SLM only explains them.
5. **Tools return compact data.** Tools return summarised or aggregated results (top-N rows, totals), never 10,000 raw rows. This protects accuracy *and* the client's token bill.
6. **Rich replies, not just text.** The SLM outputs structured blocks (`table`, `chart`, `plan_card`, `confirm_action`, `file_download`). The widget renders them.

### Example tool definition (generated from the existing API)
```json
{
  "name": "update_campaign_budget",
  "description": "Change the daily or lifetime budget of a campaign. Requires user confirmation.",
  "group": "budgets_bids",
  "requires_confirmation": true,
  "parameters": {
    "type": "object",
    "properties": {
      "campaign_id":  { "type": "string" },
      "budget_type":  { "type": "string", "enum": ["daily", "lifetime"] },
      "amount":       { "type": "number", "minimum": 0 },
      "currency":     { "type": "string" }
    },
    "required": ["campaign_id", "budget_type", "amount"]
  }
}
```

---

## 3. Token metering and billing design

### 3.1 What we count
Each model call records `prompt_tokens`, `completion_tokens` and `cached_prompt_tokens`. One user message can trigger several model calls (tool call → tool result → final answer). All of them are recorded.

### 3.2 What we bill: a decision to make
| Option | Billed | Pros | Cons |
|---|---|---|---|
| **A. Everything processed** | System prompt + tool schemas + history + tool results + output | Matches our cost exactly | Clients pay for our overhead; bills feel opaque and are hard to dispute |
| **B. Conversation tokens (recommended)** | User input + assistant output + documents the user attaches | Transparent: the client sees the count on every message | Our internal overhead must be priced into the per-token rate |
| **C. Hybrid** | B + tool-result data at a lower rate | Fair for heavy data pulls | More complex to explain |

**Recommendation:** start with **B**, with separate input and output rates, like the API vendors. Show the token count on each message and in a usage dashboard. Prefix caching of the system prompt and tool schemas in vLLM keeps our overhead cost very low.

### 3.3 Make the billing unit independent of the model
Different base models tokenize the same text into different token counts. If we bill on the serving model's own tokenizer, **upgrading the SLM would silently change every client's bill.**
→ **Bill in "AgencyOS tokens" counted with one fixed reference tokenizer**, versioned and published, regardless of which model serves the request. Internally we still track real serving tokens for cost.

### 3.4 Watch the incentive conflict
If we bill purely per token, **making the model more efficient (shorter, sharper answers) lowers our revenue**, and verbose answers raise the client's bill. Clients will notice. Mitigations:
- Sell **plans with included tokens + overage** (predictable for clients, predictable revenue for us).
- Optionally convert to **credits** (1 credit = N tokens). That lets us later price heavy actions (e.g., "generate a full media plan") as a fixed credit cost without changing the pricing model.
- Keep answers concise by design. Trust drives retention more than a few extra tokens.

### 3.5 Billing models to offer
| Plan | Mechanism |
|---|---|
| **Prepaid wallet** (default for SMB/self-serve) | Client buys a token pack; balance decreases in real time; auto top-up option; chat blocks or warns at zero |
| **Subscription + included tokens + overage** (agencies) | e.g., monthly fee includes X M tokens; overage billed per 1M via Stripe metered billing |
| **Enterprise committed use** | Annual commitment of Y tokens at a discount; invoiced |

**Controls clients need:** per-workspace and per-user monthly caps, alerts at 50/80/100%, usage by user / client / campaign, and CSV export of the usage ledger for audit.

### 3.6 Usage ledger schema (append-only)
```sql
CREATE TABLE usage_events (
  id                 BIGSERIAL PRIMARY KEY,
  occurred_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  tenant_id          UUID NOT NULL,
  user_id            UUID NOT NULL,
  conversation_id    UUID NOT NULL,
  message_id         UUID NOT NULL,
  model_call_seq     INT  NOT NULL,          -- nth model call for this message
  model_version      TEXT NOT NULL,          -- e.g. media-slm-v1.3-lora-tenantX
  serving_prompt_tokens      INT NOT NULL,   -- real cost tracking
  serving_completion_tokens  INT NOT NULL,
  serving_cached_tokens      INT NOT NULL DEFAULT 0,
  billable_input_tokens      INT NOT NULL,   -- reference-tokenizer, per billing policy
  billable_output_tokens     INT NOT NULL,
  billing_tokenizer_version  TEXT NOT NULL,
  price_book_id      TEXT NOT NULL,          -- rate in force at the time
  amount_micros      BIGINT NOT NULL,        -- charge in currency micro-units
  idempotency_key    TEXT UNIQUE NOT NULL    -- no double charging on retries
);
-- Wallet balance = credits purchased − SUM(amount_micros); hourly roll-ups push to Stripe.
```

### 3.7 Unit economics (how to calculate it; benchmark on your own load)
```
Our cost per 1M tokens = GPU $/hour ÷ (sustained tokens/hour ÷ 1,000,000)

Example (illustrative only):
  1 GPU at $2.50/hour serving a quantised 8B model with vLLM batching,
  sustaining ~2,000 output tokens/sec ≈ 7.2M tokens/hour
  → ≈ $0.35 per 1M output tokens (input/prefill tokens are much cheaper)
```
Set the client price per 1M tokens so that, **at realistic GPU utilisation (30–60%, not 100%)**, gross margin is at least 70–80% after hosting, monitoring and support. **Run shadow billing first** (Section 6) to set the real price from real usage.

---

## 4. Building the media-campaign SLM

### 4.1 Base model
Use an open-weight **instruct model of 3B–14B** with strong **function calling** and a **commercial-friendly licence** (e.g., current Qwen, Mistral, Llama, Gemma or Phi releases; check the licence for resale and on-prem use). Run a bake-off of 2–3 candidates on our own eval set (Section 4.4). Start with **7–8B**: a good balance of quality and cost, and it runs on one GPU.

### 4.2 Training data: generated from our own product
Our biggest advantage is that **we already have the product and its API**. So:

1. **Tool inventory:** export every API action into tool schemas grouped by skill (~30–80 tools is typical).
2. **Real logs:** support tickets, help-centre questions, onboarding calls, and how users click through the product. These show what people actually ask for.
3. **Synthetic conversations (teacher → student):** use a strong frontier model *offline* as a "teacher" to generate thousands of realistic multi-turn chats in media language, e.g. *"move 20% of the search budget into Meta retargeting for the Diwali flight"*. Each chat comes with the correct tool calls. **Execute every generated tool call against a sandbox copy of AgencyOS** and keep only conversations that succeed. Media experts review a sample. (Check the teacher model's terms allow using outputs for training.)
4. **Domain knowledge:** media terminology, KPI definitions, platform specs, ad policies, so the SLM explains results like a planner would.
5. **Refusals:** examples of off-topic requests (coding help, general trivia, other industries) with short, polite declines. This enforces the "media campaigns only" focus and saves tokens.
6. **Concise style:** train on short, direct answers. This affects quality *and* the client's bill.

**Volume for v1:** about 10k–30k validated conversations (roughly 200–500 per tool), 1k–3k refusal examples, and 2k–5k preference pairs (good vs bad answer) for the alignment step.

### 4.3 Training steps
```
1. SFT with LoRA/QLoRA on tool-calling conversations    (TRL / Axolotl / Unsloth / LLaMA-Factory)
2. Preference tuning (DPO) on expert-ranked answer pairs → accuracy, tone, brevity
3. Quantise (AWQ / FP8) for cheap, fast serving
4. Optional per-client LoRA adapter (client's naming conventions, brands, KPIs)
```
Runs on rented GPUs. A LoRA fine-tune of an 8B model is hours, not weeks. Data work takes the time.

### 4.4 Evaluation (release gate before any client sees a new version)
| Metric | Target for launch |
|---|---|
| Correct tool selected | ≥ 95% |
| Correct, valid tool arguments | ≥ 93% |
| End-to-end task success (sandbox) | ≥ 90% |
| Write actions always go through confirmation | **100%** |
| Off-topic requests correctly declined | ≥ 95%, with < 2% wrongly refused media requests |
| Numbers in answers traceable to tool output | **100%** |
| Average billable tokens per task | Tracked; must not grow between versions |
| p95 time to first token | < 1 second |

Keep a frozen test set of 500–1,000 real-style conversations. Every new model version must pass it.

### 4.5 Fallback (optional)
For rare hard requests (long strategic analysis), the router can escalate to a larger model. **If we do this, it must have its own token price** (a "Pro reasoning" rate), or our margin disappears. Alternatively, run a second, larger self-hosted open model (e.g., 30B-class) instead of an external API.

---

## 5. Serving and operations

| Concern | Approach |
|---|---|
| Inference engine | **vLLM** (multi-LoRA, prefix caching, guided JSON decoding, streaming); SGLang as an alternative |
| Hosting at start | Managed GPU providers that host fine-tuned models/LoRAs (e.g., Together, Fireworks, Modal, RunPod, Bedrock custom model import) **→** move to reserved GPUs on our own cloud once utilisation is steady (cheapest per token) |
| Scaling | Autoscale replicas on queue depth; keep 1 warm replica minimum |
| Enterprise / private | Same container + quantised model deployed in the client's VPC; metering still reports to our ledger |
| Observability | Langfuse/OpenTelemetry traces per conversation: tokens, latency, tool errors, model version |
| Safety | Prompt-injection defence on documents and tool results (treat them as data), PII redaction in logs, tenant isolation tests |
| Continuous improvement | Thumbs-up/down + "user corrected the action" signals → weekly review → monthly retrain |

---

## 6. Rollout plan (existing product → chat + token billing)

| Weeks | Milestone | Done when |
|---|---|---|
| **1–2** | **Tool inventory & design.** List every product action, mark read vs write, generate tool schemas from the API, define skill groups, decide billing policy (Section 3.2), set up the sandbox | All actions mapped; pricing policy signed off |
| **3–6** | **Chat engine MVP on an *untuned* open SLM.** Gateway, scope guard, router, agent loop, confirmation cards, rich blocks, **metering + ledger**, internal usage dashboard | Internal team can run the top 20 workflows by chat |
| **5–9** | **Data + fine-tune SLM v1.** Synthetic conversations validated in the sandbox, refusal set, expert review, SFT + DPO, eval harness | v1 passes the Section 4.4 targets |
| **9–12** | **Beta with 5–10 clients + shadow billing.** Real usage; show token counts and "what this would cost" without charging | Real tokens-per-task and cost-per-task data; price per 1M tokens set |
| **12–14** | **Billing live.** Wallets / plans, Stripe metered billing, caps & alerts, invoices, usage export, terms updated | First token invoices paid |
| **Ongoing** | Monthly retrain from feedback; add channels (Slack/Teams/WhatsApp); per-client adapters; private deployment offer | Tool accuracy and margin trending up |

**Team for this:** 1 ML engineer (SLM training/serving), 2 backend engineers (chat engine, tools, metering/billing), 1 frontend engineer (chat widget, rich blocks, usage dashboard), 1 media SME (part-time: data review, evals), plus product/QA from the existing team.

---

## 7. Key risks

| Risk | Mitigation |
|---|---|
| Chat executes a wrong spend action | Mandatory confirmation, argument validation in the API, idempotency, undo where the platform allows, audit log |
| Clients dispute token bills | Per-message token display, a published reference tokenizer, exportable ledger, caps and alerts |
| Token bill shock from big data pulls | Tools return aggregates; warn before expensive operations ("this report will use ~40k tokens, continue?") |
| SLM struggles with a large tool list | Intent router loads only one skill group's tools per turn |
| Model upgrade changes bills | Billing tokenizer fixed and versioned (Section 3.3) |
| Prompt injection via uploaded briefs or platform data | Treat all tool and document content as data; the model cannot run writes without user confirmation |
| Revenue falls as the model gets more efficient | Plans with included tokens, a credits layer, fixed-credit premium actions |

---

## 8. Information needed from the product team to start building

1. **Tech stack** of the existing product (backend language/framework, database, cloud) and whether a **public/internal API (OpenAPI spec)** exists.
2. **List of product activities/modules** the chat must cover, and which ones spend money or change live campaigns.
3. **Ad platforms** currently integrated (Google, Meta, DV360, TTD, TikTok, LinkedIn, Amazon, others).
4. **Expected volume:** number of client users, chats per day. This sizes the GPUs.
5. **Billing today:** existing billing system (Stripe/Chargebee/Razorpay/in-house) and currency/markets.
6. **Languages** the chat must support.
7. Any requirement for **private / on-prem deployment** for specific clients.

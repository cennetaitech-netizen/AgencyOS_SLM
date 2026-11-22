# AgencyOS Chat — Lean MVP (build it ourselves, in stages)

The guardrail list in [BUILD_READINESS.md](BUILD_READINESS.md) is the **end state** for a mature product. **We don't need all of it on day one.** Most guardrails exist to protect *write actions on live campaigns* and *complex billing*. If we leave both out of the first release, the list shrinks to a handful of simple rules.

---

## The three simplifications

| Simplify | MVP choice | What it removes for now |
|---|---|---|
| **1. Chat is read-only + drafts** | Chat can **ask, analyse, report, explain, and draft** plans, copy and recommendations. It **can't change live campaigns**. The user clicks the existing product button to apply. | Confirmation cards, spend thresholds, undo, two-person approval, kill switch, idempotency work, liability terms |
| **2. Prepaid wallet only** | Free trial grant + prepaid token packs. **No subscriptions, no overage, no post-paid invoices.** | Monthly invoicing, overage, spend limits, proration, deferred-revenue complexity (receipt at purchase only) |
| **3. No fine-tuning yet** | Use an **off-the-shelf open SLM** (7–8B instruct model with tool calling) on a **managed host**. Limit to the **10–15 most-used tools**. | Training pipeline, GPUs to manage, ML specialist (initially) |

---

## Minimum guardrails for launch (only these 8)

| # | Guardrail | How hard is it? |
|---|---|---|
| 1 | Chat uses the user's **existing login and permissions** | Reuse what the product already has |
| 2 | **Token balance check** before each message; block at zero | One atomic database update |
| 3 | **Max reply length** (`max_tokens`) and **max 5 tool steps** per message | Two config values |
| 4 | **Media-only topic rule** in the system prompt + polite refusal | Prompt text |
| 5 | **Numbers only from tools**: tools return totals/KPIs, the model explains | Design rule in tool code |
| 6 | **Don't bill failed replies** | `try / except` → refund the reservation |
| 7 | Treat uploaded files and ad-platform text as **data, not instructions** | Prompt rule + no write actions = low risk |
| 8 | **Log every conversation**, token count and tool call | Logging table |

Everything else in BUILD_READINESS.md is added **only when the feature that needs it is added**.

**Also required at launch:** protection for *our own* costs (global kill switch, daily token ceilings, model-host spend alerts) and the operational items in [GAP_REVIEW.md](GAP_REVIEW.md). These are configuration and plumbing, not extra chat guardrails.

---

## Staged roadmap

| Stage | Duration | Scope | Guardrails added at this stage |
|---|---|---|---|
| **1. Lean MVP** | ~8–10 weeks | Read-only chat over 10–15 tools; off-the-shelf SLM; free trial + prepaid packs; usage counter in UI | The 8 above |
| **2. Own SLM** | +6–8 weeks | Fine-tune on *real* MVP conversations (better data than synthetic); more tools; lower cost per token | Eval test set before each release |
| **3. Actions** | +4–6 weeks | Chat can change budgets, pause/launch campaigns | Confirmation cards, thresholds, audit, kill switch |
| **4. Business plans** | +4–6 weeks | Subscriptions with included tokens, overage, monthly invoices, per-user/workspace caps | Spend limits, alerts, invoice reconciliation |
| **5. Enterprise** | Later | Per-client models, private deployment, SSO | Anomaly detection, advanced compliance |

---

## Team for Stage 1
- **2 backend developers** (Python or Node): chat engine, tools, wallet.
- **1 frontend developer**: chat widget, usage counter, buy-tokens page.
- **Media expert** (a few hours a week): test conversations, judge answers.
- **No ML specialist needed yet.** For Stage 2, hire or contract one, or use a managed fine-tuning service.

## Done criteria for Stage 1
- 20 real user questions answered correctly by chat, using real product data.
- Token counts on every reply; wallet never goes below zero; failed replies not charged.
- 5 pilot clients using it weekly, with real token usage data to set the final price per token.

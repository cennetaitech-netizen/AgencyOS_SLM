# Gap Review: What Was Missing from the Stage 1 Plan

A review of `LEAN_MVP.md` and `IMPLEMENTATION_GUIDE.md` before development starts.
**Agent** items have been added to `IMPLEMENTATION_GUIDE.md` (the phase is noted). **People** items are decisions or work the coding agent can't do.

Priority: 🔴 must resolve before or during build · 🟠 before pilot · 🟢 before scaling

---

## A. Commercial and pricing

| # | Gap | Priority | Who | Resolution |
|---|---|---|---|---|
| A1 | **Hidden cost per message.** We only bill the user's text and the visible reply, but the model also processes the system prompt, tool definitions, history and tool results on every step. Real model usage can be **5–20× the billable tokens**. Pricing without measuring this could make every message lose money. | 🔴 | Agent + finance | Measure the **overhead ratio** in the Phase 2 bake-off and Phase 7 dashboard; set the price per billable token from it |
| A2 | No **shadow billing** in the guide, so the price would be set before real usage data exists | 🔴 | Agent | `CHAT_BILLING_MODE=shadow` (Phase 1); go live after 2–4 weeks of pilot |
| A3 | No protection for **our own** model-host bill (bugs, loops, abuse) | 🔴 | Agent + ops | Global kill switch, global daily host-token ceiling, per-org and free-trial daily ceilings, host spend alerts (Phases 1, 2) |
| A4 | Long conversations get more expensive for us every turn (history isn't billed) | 🟠 | Agent | `CHAT_MAX_TURNS_PER_CONVERSATION` (Phase 4) |
| A5 | **Free-trial abuse** if orgs can self-sign-up | 🟠 | Agent + product | Phase 0 checks how orgs are created; free-trial daily ceiling; add verification if self-signup |
| A6 | Price per token, pack sizes and pilot success criteria not decided | 🔴 | **People** (leadership/finance) | Decide after the shadow-billing data; suggested pilot criteria are in Phase 8 |

## B. Finance, tax and legal

| # | Gap | Priority | Who | Resolution |
|---|---|---|---|---|
| B1 | **A payment receipt isn't always a tax invoice.** B2B clients need a compliant tax invoice (legal name, address, tax ID) to claim tax credits | 🔴 | Agent + finance | Billing profile + tax-compliant invoice via provider invoicing or the accounting system (Phase 6) |
| B2 | No handling of **refunds and chargebacks** on token packs | 🟠 | Agent + finance | `reverse_purchase` + refund/dispute webhooks (Phase 6) |
| B3 | **Prepaid credit expiry** and refund rules can be regulated in some jurisdictions; prepaid credits are deferred revenue | 🟠 | **People** (legal/finance) | Confirm the expiry and refund policy per market; accounting treatment |
| B4 | **Client data goes to a third-party model host** in Stage 1. That needs a DPA, zero data retention, no training on our data, and an acceptable region. It also affects the privacy story in sales | 🔴 | **People** (legal) + agent | Host selection checklist (Phase 2); DPA signed before the pilot |
| B5 | **Consent to use chat data for training** isn't captured, but Stage 2 depends on fine-tuning with real conversations | 🔴 | **People** (legal) + agent | Consent clause in terms + `training_consent` flag per org (Phase 4). Without consent, Stage 2 must use synthetic data |
| B6 | **Data retention and deletion** (user deletes chat, org leaves, data-protection requests) | 🟠 | Agent | Retention job + deletion endpoints (Phase 4) |
| B7 | **AI disclaimer** and liability for wrong recommendations, even with read-only chat | 🟠 | **People** (legal) + agent | UI disclaimer (Phase 5) + terms update |

## C. Answer quality and correctness

| # | Gap | Priority | Who | Resolution |
|---|---|---|---|---|
| C1 | **The model doesn't know today's date**, the org's timezone or currency, so "last month" or "this quarter" would be wrong | 🔴 | Agent | Context block in the system prompt (Section 5.1, Phase 4) |
| C2 | **Drafting media plans needs arithmetic** (budget splits, impressions from CPM), which breaks "numbers only from tools" | 🔴 | Agent | Deterministic `media_math` tool (Phase 3) |
| C3 | **Small models can't count characters**, so ad copy would break platform limits | 🟠 | Agent | `validate_ad_copy` tool + human-maintained limits table (Phase 3) |
| C4 | "How do I… in AgencyOS" was in scope with no help source, so the model would **invent menu paths** | 🟠 | Agent | `search_help_docs` tool, or remove from scope (Phase 3) |
| C5 | Answers didn't state **data freshness** (ad platform sync lag) | 🟠 | Agent | `data_as_of` from every tool; stated in answers |
| C6 | No **user feedback** capture, so quality can't be measured or improved | 🔴 | Agent | Thumbs up/down + reason (Phases 4, 5) |
| C7 | **Full tool results** weren't stored, so wrong answers can't be debugged and Stage 2 data can't be built | 🟠 | Agent | Encrypted `result` column with retention (Phase 4) |
| C8 | Prompt/model **versions** not recorded per answer | 🟠 | Agent | `prompt_version`, `model_name`, serving host per message |
| C9 | Evals only run manually | 🟠 | Agent | Eval harness in CI blocks merges on regressions (Phase 7) |
| C10 | **Agency multi-client scoping**: users who manage many brands need correct client context | 🟠 | Agent | Phase 0 documents scoping; tools take explicit client IDs |

## D. Engineering and operations

| # | Gap | Priority | Who | Resolution |
|---|---|---|---|---|
| D1 | No **fallback** if the model host is down | 🟠 | Agent | Same model on a second provider; retry before the first token (Phase 2) |
| D2 | **Streaming gotchas:** the browser `EventSource` can't send auth headers; proxies buffer streams; load balancers time out | 🔴 | Agent | fetch-streaming, heartbeats, proxy buffering off, idle timeouts (Phase 4) |
| D3 | No **load test**; managed hosts have tokens/min limits | 🟠 | Agent | Load test at 2× pilot peak (Phase 8) |
| D4 | No **security review** of the new endpoints (cross-org access, prompt injection, XSS) | 🔴 | **People** + agent | Security review in the Phase 8 checklist |
| D5 | **Stage 2 continuity:** the Stage 1 model and host must support fine-tuning and serving the *same* base model later | 🟠 | Agent + people | Host checklist item (Phase 2) |
| D6 | No **product analytics** for activation, trial-to-paid conversion and retention | 🟠 | Agent | Analytics events (Phase 7) |
| D7 | No email notifications for purchases, low balance, tokens exhausted | 🟢 | Agent | Phase 6 |

## E. People, process and go-to-market (not coding-agent work)

| # | Gap | Priority | Owner to assign |
|---|---|---|---|
| E1 | **Named owners:** product owner, tech lead (approves checkpoints), finance and legal contacts, media expert | 🔴 | Leadership |
| E2 | **Timeline and budget sign-off** (team time + model hosting + tools) | 🔴 | Leadership |
| E3 | **Pilot client selection** (5 orgs), pilot agreement, feedback cadence | 🟠 | Product / sales |
| E4 | **Support readiness:** FAQ ("what is a token?", "why was I charged?"), dispute process, runbook | 🟠 | Support |
| E5 | **Launch materials:** pricing page, announcement, sales demo script, objection handling ("is my data safe?") | 🟢 | Marketing / sales |
| E6 | **Incident process** for a wrong answer that a client acted on | 🟠 | Product + support |
| E7 | **Stage 2 data plan:** if consent rates are low, budget for synthetic data generation and expert review | 🟢 | Product + ML |

---

## Will this build close the gaps?

| Coverage | Gaps | Condition |
|---|---|---|
| ✅ **Closed by the build** | A2, A3, A4, B2, B6, C1, C2, C3, C5, C6, C7, C8, C9, D1, D2, D3, D6, D7 | The agent implements the phase, the tests pass, and a human reviews the checkpoint |
| 🟡 **Build provides the mechanism; people supply the input** | A1 (finance sets the price from the data), A5 (product decides on signup verification), B1 (finance picks the invoicing route), B4 (legal signs the host DPA), B5 (legal writes the consent clause), B7 (legal approves the disclaimer and terms), C4 (needs help-centre content), C10 (depends on product API scoping), D4 (people run the security review), D5 (choice of host and model) | Closed only when the Section 1 inputs are filled and the human tasks are done |
| ❌ **Not solved by code** | A6, B3, E1–E7 | Leadership, finance, legal, sales and support work |

### Update: full-solve coverage (Release 1.0)
Every 🟡 and ❌ item now has either a build phase or a **ready draft**, so the remaining human work is **review and sign-off**, not creation:

| Gap | Covered by |
|---|---|
| A1, A6 (pricing) | `business/GOVERNANCE_AND_PRICING.md` §4: cost floor, value pricing, plan structure, the steps to set the price from shadow data |
| A5 (free-trial abuse) | Phase 1 daily ceiling + Phase 0 signup check; product decision D2 |
| B1, B3 (tax invoice, expiry/refunds) | Phase 6/10 build + `business/LEGAL_AND_FINANCE_DRAFTS.md` §7–9 |
| B4, B5, B7 (DPA, consent, disclaimer, terms) | `business/LEGAL_AND_FINANCE_DRAFTS.md` §1–6 |
| C4 (help content) | Phase 3 tool; content owner in the RACI |
| D4 (security review) | Phase 8 review + Phase 12 external penetration test |
| E1, E2 (owners, budget, timeline) | `business/GOVERNANCE_AND_PRICING.md` §1–3 |
| E3 (pilot) | `business/SUPPORT_PILOT_LAUNCH_KIT.md` §2 |
| E4, E6 (support, incidents) | `business/SUPPORT_PILOT_LAUNCH_KIT.md` §1 |
| E5 (launch materials) | `business/SUPPORT_PILOT_LAUNCH_KIT.md` §3 |
| E7 (Stage 2 data) | Phase 11 dataset builder (consented pilot data + sandbox-validated synthetic data) |
| "Chat must perform all activities" | Phase 9 (actions with confirmation) |
| "Invoice on token consumption, free + paid caps" | Phase 1 + 6 (wallet, free trial, packs) and Phase 10 (plans, caps, overage, monthly invoices) |
| "Our own media-campaign SLM" | Phase 11 (fine-tuning, eval gate, canary) |

### Risks that remain even after a complete build
1. **Small-model quality is unproven.** Phase 2 tests it before most of the build; if it fails, use a larger model, fewer tools, or simpler tool descriptions.
2. **The overhead ratio may force a high price per token.** Levers: shorter system prompt, fewer tools per turn, prompt caching on the host, self-hosting at higher volume, and Stage 2 fine-tuning (a tuned model needs shorter instructions).
3. **Clients may not like token pricing.** Shadow billing shows this before money changes hands; credits or plans with included tokens are the fallback.
4. **Wrong answers are reduced, not eliminated.** The disclaimer, feedback, weekly review and read-only scope limit the impact.
5. **Dependence on a third-party model host** until Stage 2 self-hosting.

**Proof of closure:** every ✅ and 🟡 item maps to a test or checklist line in `IMPLEMENTATION_GUIDE.md` (Phases 1–8). A gap counts as closed only when its test passes or its Phase 8 checklist item is ticked.

## Top 5 to act on now
1. **Measure the hidden cost per message (A1) and start with shadow billing (A2).** Otherwise pricing is a guess.
2. **Legal:** model host DPA and data terms (B4), training-consent clause (B5), AI disclaimer and terms (B7).
3. **Finance:** tax-invoice process for token packs (B1) and the refund/expiry policy (B3).
4. **Cost protection** (A3) before any pilot user gets access.
5. **Name owners and approve timeline and budget** (E1, E2).

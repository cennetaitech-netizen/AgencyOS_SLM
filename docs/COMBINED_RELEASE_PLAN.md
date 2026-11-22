# Combined Release Plan: M1 + M2 Together (~14–16 weeks to launch)

Delivers the full Release 1.0 scope from [IMPLEMENTATION_GUIDE.md](IMPLEMENTATION_GUIDE.md) (read + actions, own SLM, free/paid caps, token invoicing) as **one release** built by **parallel workstreams**, instead of a separate pilot followed by a second build.

**Saves roughly 6–10 weeks** compared with M1 then M2 (20–24 weeks). It costs a **bigger team** and needs **four replacements** for things the sequential plan learned from the pilot (Section 3).

---

## 1. How it works: contract first, then parallel

Sequential phases are replaced by **six workstreams** that start together once the **shared contracts** are agreed in weeks 1–2. Each workstream can then be built by its own engineers and AI coding agents (separate branches/worktrees) without waiting for the others.

**Shared contracts (agreed by end of week 2, owned by the tech lead):**
1. Database schema: all tables in the implementation guide (Sections 6.1, Phase 9, Phase 10).
2. Chat API + SSE events (Section 7), including `action.proposed` and the action confirm/cancel endpoints.
3. Tool registry interface (Section 5.3), read and write.
4. Wallet service interface: `reserve`, `settle`, `release`, caps and overage.
5. Model client interface (OpenAI-compatible), so the model can be swapped (base ↔ fine-tuned) without code changes.

---

## 2. Workstreams and timeline

| Workstream | Implementation-guide phases | Weeks | Team |
|---|---|---|---|
| **WS1 Chat core & tools** | 0, 3, 4, 9 (read + write tools, agent loop, confirmation flow) | 1–10 | 2 backend |
| **WS2 Billing** | 1, 6, 10 (wallet, free trial, packs, plans, caps, overage, invoices, reconciliation, dunning) | 1–10 | 2 backend |
| **WS3 Frontend** | 5 + Phase 9/10 UI (chat, confirmation cards, undo, usage, caps, billing pages) | 3–11 | 1–2 frontend |
| **WS4 Model / ML** | 2 (bake-off) + 11 (fine-tune v1 on synthetic + SME data) | 2–10 | 1 ML engineer + SME (part-time) |
| **WS5 Platform, quality & security** | 7, 8, 12 (observability, evals in CI, load test, pen test, SLOs, on-call) | 1–14 | 1 QA + 1 DevOps (part-time) |
| **WS6 Business readiness** | `business/` docs: legal, finance, pricing, support, beta, launch | 1–12 | Owners from the RACI |

```
Week:        1  2  3  4  5  6  7  8  9  10 11 12 13 14 (15 16 buffer)
Contracts    ███
WS1 Chat     ██████████████████████████████
WS2 Billing  ██████████████████████████████
WS3 Front          ██████████████████████████
WS4 ML          ████(bake-off)███████████████(gate)
WS5 Quality  ███████████████████████(pen test)█████████████
WS6 Business ████████████████████████████████████
Integration           ▲     ▲     ▲     ▲   (fortnightly demos)
Beta (3–5 clients)                         ████████
Go/no-go → GA                                       ▲
```

### Integrated gates (replace the 12 sequential checkpoints)
| Gate | Week | Pass criteria |
|---|---|---|
| **G1 Contracts & discovery** | 2 | DISCOVERY.md (read + write inventory), contracts signed off, Section 1 inputs filled, pen test **booked** |
| **G2 Model choice** | 4 | Bake-off on read **and** write tools meets targets; host DPA in progress |
| **G3 Feature complete (staging)** | 10 | All phases' tests green; action tests on real test ad accounts; **5 accelerated billing cycles** reconciled; fine-tune gate result known |
| **G4 Beta start** | 11 | Pen test critical/high findings fixed; legal terms and pricing approved; support trained |
| **G5 Go/no-go → GA** | 13–14 | Beta success criteria (Section 4) met; first live invoices approved by finance |

---

## 3. What we lose by merging, and how we replace it

| The sequential plan relied on | Combined-release replacement |
|---|---|
| **Pilot data to set prices** | Set the launch price from the **cost floor** (bake-off overhead ratio) + value pricing (`business/GOVERNANCE_AND_PRICING.md` §4); launch with **introductory pricing for 3 months**, with the price-change notice clause already in the terms; review it after 60 days of real data |
| **Real pilot conversations for fine-tuning** | **SLM v1** trained on **synthetic conversations validated in the sandbox + SME-written examples**. **SLM v2** retrains on consented real conversations 6–8 weeks after launch. **The launch doesn't depend on the fine-tune:** whichever model (base or fine-tuned) passes the eval gate at G3 goes live, and the other is swapped in later behind the model-client interface |
| **8 weeks of read-only pilot before actions** | **Action safety defaults ON:** write actions opt-in per org by the admin; second approval required for tier 3; max budget change 20% per action at launch; daily spend-change cap; 30+ action eval cases; staging tests on real test ad accounts |
| **One full real billing cycle before launch** | **5 accelerated billing cycles in staging** (cycle length = 1 day) reconciled with zero discrepancy; then the **first 2 real invoice runs need manual finance approval** before they're sent |
| **Pilot for user feedback** | A **2–3 week beta with 3–5 clients** (weeks 11–13): week 1 in shadow billing, then live purchases. Runs alongside final hardening, so it adds little time but catches real-world problems before general availability |

---

## 4. Beta success criteria (gate G5)
- ≥ 70% 👍 on rated answers; **zero** wrong-number incidents; **zero** actions executed differently from what was confirmed.
- ≥ 95% correct action proposals in beta usage review.
- Billing: beta purchases and the accelerated cycles reconciled with zero discrepancy.
- p95 first token < 1.5 s; error rate < 2%.
- At least 2 beta clients accept the launch price.

---

## 5. What must happen in week 1 (long lead times)
1. **Name all owners** (RACI in `business/GOVERNANCE_AND_PRICING.md`) and approve the budget for the larger team.
2. **Fill Section 1 of the implementation guide** (stack, API spec, auth, staging, test ad accounts, model host shortlist, payment provider, currency).
3. **Legal starts** on terms, DPA and consent (`business/LEGAL_AND_FINANCE_DRAFTS.md`). Target: approved by week 10.
4. **Finance confirms** the tax-invoice route and refund/expiry policy. Target: week 6 (Phase 10 depends on it).
5. **Book the external penetration test** for week 9–10 (vendors need lead time).
6. **Request test ad accounts** on each ad platform (Google Ads test accounts, Meta sandbox) for action testing.
7. **Hire or contract the ML engineer** (needed from week 2).
8. **Shortlist 3–5 beta clients** and agree the beta terms.

---

## 6. If even 14–16 weeks is too long: cut scope, never safety
| Lever | Saves | Trade-off |
|---|---|---|
| Launch with the **top 5–8 write actions** only (e.g., budget change, pause/resume, bid change); add the rest monthly | 2–3 weeks | Fewer "do it by chat" actions at launch |
| Launch with **Free trial + packs + one subscription plan**; Enterprise plans after launch | 1–2 weeks | Enterprise deals handled manually at first |
| **Launch on the base SLM** if the fine-tune isn't ready by G3 | Removes a dependency | Higher cost per message until SLM v1 ships |
| Launch on **2 ad platforms** (Google, Meta), add more after | 1–2 weeks | Narrower coverage |

**Never cut:** confirmations on actions, wallet/caps correctness, reconciliation, permission checks, kill switches, the security test, legal terms.

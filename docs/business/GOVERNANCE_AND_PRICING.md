# Governance, Decisions, Budget and Pricing (draft for leadership)

Fill in the names, then use this as the operating document for Release 1.0.

---

## 1. Owners (RACI)

R = does the work · A = approves (one per row) · C = consulted · I = informed

| Activity | Exec sponsor | Product owner | Tech lead | ML lead | Finance | Legal | Media SME | Support | Sales/Mktg |
|---|---|---|---|---|---|---|---|---|---|
| Implementation checkpoints 0–8 | I | **A** | R | C | I | I | C | I | I |
| Wallet/billing code review (Ph 1, 10) | I | C | **A**/R | | C | | | | |
| Tools/permissions/actions review (Ph 3, 9) | I | C | **A**/R | | | C | C | | |
| Model selection & fine-tuning (Ph 2, 11) | I | C | C | **A**/R | | C | C | | |
| Pricing, plans, packs | **A** | R | C | C | R | C | | | C |
| Terms, DPA, consent, disclaimer | I | C | C | | C | **A**/R | | | I |
| Tax invoices, revenue recognition | I | C | C | | **A**/R | C | | | |
| Pilot client selection & agreement | I | **A**/R | | | C | C | C | C | R |
| Support FAQ, runbooks, incidents | I | C | C | | | | C | **A**/R | |
| Launch materials | I | C | | | C | C | C | C | **A**/R |
| Go/no-go M1 (pilot) and M2 (launch) | **A** | R | R | R | R | R | C | R | R |

**Names:** Exec sponsor `TODO` · Product owner `TODO` · Tech lead `TODO` · ML lead `TODO` · Finance `TODO` · Legal `TODO` · Media SME `TODO` · Support `TODO` · Sales/Marketing `TODO`

**Cadence:** weekly 30-minute steering (product owner chairs); checkpoint reviews within 2 working days of the agent's report.

---

## 2. Decision log (each needs an owner and a date)

| ID | Decision | Recommended default | Owner | Due | Status |
|---|---|---|---|---|---|
| D1 | Price per 1M billable tokens | Set from Section 4 after 2–4 weeks of shadow data | Exec + Finance | End of pilot | Open |
| D2 | Free trial size & validity | 200k tokens, 30 days, 50k/day ceiling, one per org | Product | Phase 1 | Open |
| D3 | Plan catalogue (allowances, fees, overage) | Section 4.3 | Exec + Finance | Before Phase 10 | Open |
| D4 | Credit expiry & refund policy | See LEGAL_AND_FINANCE_DRAFTS.md §7 | Legal + Finance | Before pilot | Open |
| D5 | Model host (+ fallback) | Phase 2 bake-off recommendation | ML lead | Phase 2 | Open |
| D6 | Base model family | Phase 2 bake-off recommendation | ML lead | Phase 2 | Open |
| D7 | Write actions & risk tiers | Phase 9 inventory | Product | Phase 9 | Open |
| D8 | Action safety thresholds | 30% max change; second approval above `TODO` | Product + Legal | Phase 9 | Open |
| D9 | Invoicing route | Payment provider invoicing, or accounting-system integration | Finance | Phase 6 | Open |
| D10 | Retention period | 365 days | Legal | Phase 4 | Open |
| D11 | Pilot orgs (5) | SUPPORT_PILOT_LAUNCH_KIT.md §2 | Product + Sales | Before Phase 8 | Open |
| D12 | Go/no-go M1 / M2 | Checklists in the implementation guide | Exec | Phase 8 / 12 | Open |

---

## 3. Budget template (fill in with your own rates)

| Category | Basis | M1 (≈ 10–12 weeks) | M2 (≈ 10–12 weeks) |
|---|---|---|---|
| Engineering team | 3 backend, 1 frontend, 1 QA × weeks × rate | `TODO` | `TODO` |
| ML engineer | 1 from ~Phase 8 (hire or contract) | `TODO` | `TODO` |
| Media SME | ~0.25–0.5 FTE | `TODO` | `TODO` |
| Model hosting | host tokens × host price (shadow-pilot usage) | small | grows with launch |
| Fine-tuning compute | rented GPU hours for LoRA runs | — | low (typically hundreds to a few thousand USD) |
| Teacher-model API for synthetic data | tokens × price | — | `TODO` |
| Payment provider fees | % of sales | — | `TODO` |
| Legal review (terms, DPA, policies) | fixed | `TODO` | — |
| Penetration test | fixed | — | `TODO` |
| Tooling (monitoring, analytics, error tracking) | monthly | `TODO` | `TODO` |
| **Contingency** | 15–20% | | |

**Timeline:** M1 pilot ≈ 10–12 weeks; M2 commercial launch ≈ a further 10–12 weeks (Phases 9 and 10 run in parallel; Phase 11 overlaps). **Total ≈ 5–6 months** with this team.

---

## 4. Pricing model

### 4.1 Cost floor (what each billable token costs us)
```
host cost per billable token = host price per host token × overhead ratio
                               (overhead ratio = host tokens ÷ billable tokens, measured in Phase 2/7)
cost per billable token      = host cost per billable token + allocation (infra, support, tooling)
price floor                  = cost per billable token ÷ (1 − target gross margin)
```
**Illustrative only** (replace with measured values): host price $0.20 per 1M host tokens × overhead ratio 12 = $2.40 per 1M billable tokens; + $0.60 allocation = $3.00; at a 75% target margin, the **floor is $12 per 1M billable tokens**.

### 4.2 Price on value, not cost (important)
An SLM makes our cost per message very low (in the example above, a typical 500-token message costs us fractions of a cent). **Pricing at cost-plus would leave most of the value on the table.** Set the price from:
- **Time saved:** e.g., a weekly cross-channel report that takes a planner 1–2 hours, done in a 3,000–8,000-token chat.
- **Alternatives:** the planner's hourly cost, competing tools' seat prices, and general AI subscriptions.
- **Shadow data:** tokens per org per month for real pilot usage → what each plan's allowance should be.

The cost floor guarantees margin; the **value price** decides revenue.

### 4.3 Recommended plan structure (fill in prices after the pilot)
| Plan | Fee | Included billable tokens / month | Overage | Caps |
|---|---|---|---|---|
| Free trial | 0 | 200k one-time (30 days) | No | Hard stop; 50k/day |
| Starter | `TODO` / month | `TODO` (≈ pilot median usage of a small team) | Off by default | Org cap |
| Pro / Agency | `TODO` / month | `TODO` (≈ pilot 75th percentile) | Yes, `TODO` per 1M | Org spend limit + workspace/user caps |
| Enterprise | Annual commitment | Pooled `TODO` / year | Discounted | Custom |
| Top-up packs | `TODO` | 1M / 5M / 20M, 12-month validity | — | — |

### 4.4 How to set the final price from shadow data (end of pilot)
1. Export per-org monthly billable tokens and the overhead ratio from the Phase 7 dashboard.
2. Compute the cost floor (4.1) with measured values.
3. Size plan allowances so ~70% of pilot orgs fit inside Pro without overage.
4. Test 2–3 price points with pilot champions ("would you pay X for this allowance?").
5. Record the decision as D1/D3 in the decision log; update the plan catalogue (Phase 10 input).

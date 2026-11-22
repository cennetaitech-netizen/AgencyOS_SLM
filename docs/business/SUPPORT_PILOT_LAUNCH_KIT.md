# Support, Pilot and Launch Kit (drafts)

---

## 1. Support

### 1.1 Client FAQ (publish in the help centre)
**What is a token?**
A token is a small piece of text, roughly ¾ of an English word on average. A typical question and answer uses a few hundred tokens; a detailed report uses a few thousand. Every reply shows how many tokens it used.

**What am I charged for?**
Only your messages and the Assistant's visible replies. Our internal processing is included in the price.

**Am I charged if something goes wrong?**
No. Failed or timed-out replies, and replies to off-topic questions, aren't charged.

**How do I control spending?**
Admins can set an organisation spend limit and caps per workspace or user, and get alerts at 50/80/90/100%.

**What happens when tokens run out?**
Chat pauses until you top up, upgrade or your monthly allowance renews. Nothing else in AgencyOS is affected.

**Can the Assistant change my campaigns?**
Only when you confirm a change on the confirmation card. Large changes may need a second approver, set by your admin, and many changes can be undone for 24 hours.

**Is my data used to train AI?**
Only if your admin opts in. You can withdraw at any time. Your data is never shown to other clients.

**Do my tokens expire?**
Monthly allowances renew each cycle without rollover; prepaid packs last 12 months; the free trial lasts 30 days.

**How do I see my usage?**
Settings → Assistant → Usage, with breakdowns by user, workspace and brand, plus a downloadable CSV.

**I think a charge is wrong.**
Contact support within 30 days with the invoice number. We'll check the usage ledger and credit any error.

### 1.2 Billing dispute process
1. Log the ticket with org, invoice number and disputed messages/dates.
2. Pull the usage ledger for the period (admin page → org → usage export).
3. Check for errored/refused messages charged by mistake, duplicate charges, or a cap/limit failure.
4. Respond within **2 working days**. If our error: credit note + root-cause ticket. If not: send the ledger extract and explain.

### 1.3 Runbooks (internal)
| Task | Steps |
|---|---|
| Check an org's usage | Admin page → org → wallet, buckets, daily usage, top users |
| Issue goodwill credit | Admin page → org → "Goodwill credit" → amount + reason (audited) |
| Disable chat for one org | Admin page → org → chat_enabled = off |
| Disable all write actions | Set `CHAT_WRITE_ACTIONS_ENABLED_GLOBAL=false` (takes effect immediately) |
| Disable chat globally | Set `CHAT_ENABLED_GLOBAL=false`; post a status page update |
| Roll back the model | Pin all orgs to the previous model version; announce in the incident channel |
| Investigate a wrong answer | Open the conversation → check the stored tool results vs the answer → classify: data issue / tool bug / model error |

### 1.4 Incident severity and process
| Severity | Example | Response |
|---|---|---|
| **Sev 1** | A confirmed action executed differently from what was displayed; cross-org data exposure; charging failures at scale | Acknowledge ≤ 1 hour; kill switch if needed; exec + legal informed; client contacted the same day |
| **Sev 2** | Wrong figures in answers for an org; wrong charges for several orgs | Acknowledge ≤ 4 hours; fix or roll back ≤ 1 working day |
| **Sev 3** | Single wrong answer, UI bug | Next working day |

**After every Sev 1/2:** root cause analysis within 5 working days, credit where appropriate, a **new eval case** that reproduces the issue, and the fix verified in CI.

---

## 2. Pilot plan (Milestone M1)

**Selection (5 orgs):** a mix of 2 agencies + 2 brand teams + 1 heavy user of the product; active on Google Ads and Meta; a named champion willing to give 30 minutes of feedback weekly; an admin willing to opt in to training consent.

**Pilot agreement outline:** 6–8 weeks · free (shadow billing; token counts shown) · read-only chat first, actions offered from Phase 9 · training-data consent (opt-in) · weekly feedback call · optional case study · **founding-customer offer** on conversion (e.g., discounted first 6 months, price locked for 12 months).

**Cadence:** kickoff (demo + starter questions) → weekly 30-minute feedback → week 4 mid-review (usage, ratings, issues) → week 8 exit review (price test, conversion offer).

**Success criteria (go to M2):** ≥ 60% of pilot users chat weekly · ≥ 70% 👍 on rated answers · zero confirmed wrong-number incidents · margin per billable token ≥ finance target · ≥ 2 orgs willing to buy at the proposed price.

---

## 3. Launch kit (Milestone M2)

**Positioning:** "AgencyOS Assistant: run your media campaigns by chat. Ask, analyse, plan and act across your campaigns, with every number from your own data and every change confirmed by you. Pay only for what you use."

**Pricing page structure:** plan cards (Free trial · Starter · Pro/Agency · Enterprise) → "What is a token?" explainer with examples ("a weekly report ≈ 5,000 tokens") → controls (caps, alerts, spend limit) → FAQ → "Start free trial".

**Announcement email (draft):**
> Subject: Run your campaigns by chat. Meet AgencyOS Assistant
> Ask "How did Meta perform last week versus Google?" or "Shift 10% of budget to the best ad set" and get the answer, or the change ready to confirm, in seconds. Every figure comes from your own AgencyOS data, and nothing changes without your click. Start with [200k] free tokens today.

**Demo script (5 minutes):** 1) performance question across platforms → table · 2) "why did CPA rise?" → explanation from data · 3) draft ad copy → validated lengths · 4) budget shift → confirmation card → confirm → undo shown · 5) usage panel and caps.

**Objection handling**
| Objection | Answer |
|---|---|
| "Is our data safe?" | Our own model on a host with zero data retention; your data isn't used for training unless you opt in; full audit log |
| "What if it's wrong?" | Every number comes from your data, not the AI; changes need your confirmation; undo; disclaimer and support |
| "Token costs feel unpredictable." | Plans with included tokens, spend limits, caps per user/team, alerts, per-message counts |
| "Why not use ChatGPT?" | It can't see or safely act on your AgencyOS campaigns, permissions and approvals, and it isn't specialised for media campaigns |

**Launch checklist:** pricing page live · help-centre FAQ live · support trained on runbooks · sales demo environment · announcement scheduled · status page · founding-customer offers sent to pilot orgs.

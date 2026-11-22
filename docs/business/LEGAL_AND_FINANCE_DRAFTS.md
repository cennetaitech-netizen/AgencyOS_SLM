# Legal and Finance Drafts (starting drafts for your lawyer and accountant)

> **These are drafting starting points, not legal or tax advice.** They must be reviewed and adapted by qualified counsel and a chartered accountant / tax adviser for each market where you sell. Placeholders are in `[brackets]`.

---

## 1. Token-based usage terms (clause for the subscription agreement / ToS)

**1.1 Definitions.** "**Token**" means the unit of text measured by [Company]'s published Billable Token Counting Method (version-numbered, available at [URL]). "**Billable Tokens**" means Tokens in (a) messages the Customer's users send to the AgencyOS Assistant and (b) the Assistant's visible replies, as recorded in [Company]'s usage ledger.

**1.2 Charges.** Use of the AgencyOS Assistant consumes Billable Tokens from, in order: (i) free or promotional grants, (ii) the monthly allowance included in the Customer's plan, (iii) prepaid token packs, and (iv) where the Customer has enabled overage, post-paid overage at the rate in the Customer's Order Form or price list.

**1.3 Not charged.** Replies that fail because of an error on [Company]'s side, timed-out replies, and replies declining out-of-scope requests aren't charged.

**1.4 Caps.** The Customer may set spend limits and usage caps. [Company] will stop overage at the Customer's spend limit. The Customer is responsible for the caps it configures.

**1.5 Validity.** Monthly allowances expire at the end of each billing cycle and don't roll over. Free trial grants expire after [30] days. Prepaid token packs expire [12] months after purchase.

**1.6 Records and disputes.** [Company]'s usage ledger is the record of consumption. A downloadable usage report accompanies each invoice. The Customer must raise billing disputes within [30] days of the invoice date. [Company] will investigate in good faith and issue a credit note where a charge was incorrect.

**1.7 Price changes.** [Company] may change token prices or plan allowances with at least [30] days' written notice, effective from the next billing cycle.

---

## 2. AI assistant disclaimer

**In-product (always visible):** "AI answers can be wrong. Check important figures before acting."

**Terms clause:** "The AgencyOS Assistant uses artificial intelligence to analyse data and generate recommendations and drafts. Outputs may be inaccurate or incomplete. The Customer remains responsible for reviewing outputs and for its decisions, including media spend decisions. [Company] doesn't guarantee any campaign result."

---

## 3. Chat actions (write operations) clause

"The Assistant may propose changes to campaigns or settings. A change is executed only after an authorised user of the Customer confirms it (and, where configured, a second approver approves it). **Confirmation constitutes the Customer's instruction to perform the change as displayed.** [Company] is responsible for executing confirmed changes as displayed and for the safeguards described in the documentation (permission checks, configured thresholds, audit logging). [Company] isn't responsible for the commercial outcome of confirmed changes, or for changes that third-party advertising platforms reject or alter. Liability is subject to the limitation of liability clause."

---

## 4. Data use and model-improvement consent (opt-in)

"With the Customer's opt-in consent (which an administrator may give or withdraw at any time in Settings), [Company] may use Assistant conversations, tool results and feedback to train and improve [Company]'s models. Before use, [Company] removes personal data and Customer identifiers. Models trained on such data are shared across customers but won't reproduce one Customer's confidential data to another. Withdrawal of consent applies to data created after withdrawal and to future training runs. Without consent, [Company] uses Customer data only to provide the service."

---

## 5. Retention and deletion

"Assistant conversations, messages and tool results are retained for [365] days and then deleted. Users may delete their own conversations at any time. On termination, Customer chat data is deleted within [30] days, except usage records required for invoicing, tax and legal obligations, which contain no message content."

---

## 6. Model host (sub-processor) due-diligence checklist

- [ ] Data Processing Agreement signed
- [ ] **Zero data retention** for prompts and outputs (or retention ≤ [X] days for abuse monitoring only), in writing
- [ ] **No training** on our data, in writing
- [ ] Processing region(s) acceptable for our clients' data-protection laws (e.g., GDPR, India DPDP Act, CCPA/CPRA)
- [ ] Security certifications (e.g., SOC 2 Type II / ISO 27001) and breach-notification commitment
- [ ] Listed in our public sub-processor list; customers notified of changes per our DPA
- [ ] The same checks for the **fallback host** and the **teacher-model** provider (plus terms allowing use of outputs for training)

---

## 7. Credit expiry, refund and chargeback policy (draft)

| Item | Draft policy |
|---|---|
| Free trial | [200k] tokens, [30] days, one per organisation, non-transferable, no cash value |
| Monthly allowance | Expires at cycle end, no rollover |
| Prepaid packs | Valid [12] months; refund of an unused pack within [14] days of purchase if less than [10]% used; otherwise non-refundable, **except where local law requires** |
| Overage | Invoiced monthly in arrears; not refundable once consumed |
| Our errors | Automatically not charged; goodwill credits at support's discretion (audited) |
| Chargebacks | Remaining tokens in the disputed purchase are frozen; account may become read-only until resolved |
| Plan downgrade | Effective next cycle; no partial refunds |

*Check consumer/prepaid-credit rules in each market; some jurisdictions restrict expiry of paid credits.*

---

## 8. Tax invoice checklist (B2B)

Every invoice must carry (verify the exact requirements per country):
- [ ] Seller's legal name, address and **tax registration number**
- [ ] Unique sequential invoice number; invoice date
- [ ] Buyer's legal name, billing address and **tax ID** (from the billing profile)
- [ ] Place of supply / buyer country
- [ ] Description of service (e.g., "AgencyOS Assistant: Pro plan, December 2026" / "Overage: 3.2M billable tokens") and the **service classification code** where required
- [ ] Taxable value, tax rate(s) and amount(s) split as required, total, currency
- [ ] E-invoicing registration reference / QR code where mandated
  - *Example, India:* GSTIN of both parties, SAC code, CGST+SGST or IGST split, and e-invoice IRN/QR for suppliers above the e-invoicing turnover threshold. Confirm with your CA.
- [ ] Reverse-charge wording for cross-border B2B where applicable

**Route decision (D9):** payment-provider invoicing (if it supports the local requirements) **or** push each sale and month-end usage to the accounting system that issues compliant invoices.

---

## 9. Revenue recognition (confirm with your accountant)

| Revenue type | Typical treatment |
|---|---|
| Prepaid token packs | **Deferred revenue** at purchase → recognised as tokens are consumed; unused balance recognised at expiry ("breakage"), subject to local rules |
| Subscription fee | Recognised evenly over the billing period |
| Included monthly allowance | Part of the subscription fee |
| Overage | Recognised in the period consumed |
| Free trial | No revenue; track as a marketing cost |

---

## 10. Month-end finance checklist
- [ ] Reconciliation job: ledger totals == invoice totals (zero discrepancy)
- [ ] Deferred revenue roll-forward for prepaid packs (opening + sales − consumption − breakage = closing)
- [ ] Review credit notes, goodwill credits and chargebacks
- [ ] Gross margin report: revenue vs model-host cost (from the Phase 7 dashboard)
- [ ] Dunning list review (past-due orgs)

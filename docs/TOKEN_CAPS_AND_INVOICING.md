# AgencyOS Chat — Free/Paid Token Caps and Invoicing on Token Consumption

This document covers how clients get **free tokens and paid tokens**, how **caps** are enforced in real time, and how we **invoice on token consumption**.

---

## 1. Plans and token entitlements (illustrative numbers; finalise after shadow billing)

| Plan | Token allowance | Cap behaviour | Overage | Notes |
|---|---|---|---|---|
| **Free trial** | One-time grant, e.g. **200k tokens**, valid **14–30 days** | **Hard cap**: chat stops at zero → upgrade prompt | None | One grant per organisation/domain; verified business email; optionally write actions limited to sandbox or a single ad account |
| **Free forever (optional)** | e.g. **50k tokens / month**, resets monthly, no rollover | **Hard cap** | None | Read/report-only features; lower rate limits |
| **Starter (paid)** | e.g. **2M tokens / month** included | Hard cap by default; client may enable overage | Optional, per 1M tokens | Top-up packs available |
| **Pro / Agency (paid)** | e.g. **10M tokens / month** included, pooled across the org | **Soft cap** at 100% → overage continues **up to the client's spend limit** | Yes, per 1M tokens | Per-workspace/per-brand sub-caps; usage by client for re-billing |
| **Enterprise** | Committed annual volume (e.g., 500M tokens/year), pooled | Soft cap + client-defined hard spend limit | Discounted overage | Custom rates, invoiced, private deployment option |
| **Top-up packs** | e.g. 1M / 5M / 20M tokens, prepaid | Added to the wallet | — | Expire after e.g. 12 months |

### Caps available at every level
| Level | Who sets it | Example |
|---|---|---|
| **Organisation** | Plan + client admin spend limit | "Never exceed 15M tokens or $X this month" |
| **Workspace / brand / client account** | Client admin | "Brand A: max 3M tokens/month" |
| **User** | Client admin | "Junior planners: max 200k tokens/day" |
| **Per message** | Us (system) | Max 1,500 output tokens; max 8 tool steps |
| **Rate limit** | Us, per plan | e.g., Free: 5 req/min; Pro: 60 req/min per user |

**Alerts:** at 50%, 80%, 90% and 100% of any cap → in-app banner + email to org admins (and the user, for user caps).

---

## 2. Credit buckets and consumption order

Every token allowance is a **bucket** with its own source, amount, validity and price. Tokens are consumed in this order:

```
1. Promotional / free grant buckets   (earliest expiry first)
2. Plan-included monthly allowance     (resets each billing cycle, no rollover)
3. Prepaid top-up packs                (earliest expiry first)
4. Overage (post-paid)                 (only if enabled, and only up to the spend limit)
→ If nothing is left: block with an "upgrade / top up" message
```

This order uses the cheapest-to-us / soonest-to-expire tokens first and keeps invoices easy to explain.

---

## 3. Real-time enforcement: Reserve → Stream → Settle

We don't know how many tokens a reply will use until it finishes, and several chats in the same organisation can run at once. To **never exceed a cap**:

```
on user_message(msg):
    est_input  = count_billable_tokens(msg + attachments)
    max_output = min(PER_MESSAGE_MAX_OUTPUT, remaining_allowance_after(est_input))
    if max_output < MIN_USEFUL_OUTPUT (e.g. 200):
        return block("You've reached your token limit", upgrade_options)

    reservation = reserve(org, user, workspace, est_input + max_output)   # atomic, row-locked
    try:
        reply = run_agent(msg, max_tokens=max_output, max_steps=8)       # streams to the user
        actual = billable_tokens(msg, reply)
        settle(reservation, actual)          # write usage_event(s), debit buckets, release the rest
    except OurSideError:
        release(reservation)                 # don't bill our failures
        raise
```

- **Reservations are atomic** (database transaction with row locks or Redis Lua script), so parallel chats can't overspend.
- Reservations **expire automatically** (e.g., after 5 minutes) if a request dies, so balances never get stuck.
- The agent's own internal steps (tool schemas, system prompt) follow the agreed billing policy (see [CHAT_ENGINE_SLM_PLAN.md](CHAT_ENGINE_SLM_PLAN.md) §3.2).

---

## 4. Data model

```sql
-- Plan catalogue
CREATE TABLE plans (
  plan_id               TEXT PRIMARY KEY,            -- 'free_trial', 'starter', 'pro', 'enterprise'
  monthly_included_tokens BIGINT NOT NULL DEFAULT 0,
  trial_grant_tokens    BIGINT NOT NULL DEFAULT 0,
  trial_validity_days   INT,
  overage_allowed       BOOLEAN NOT NULL DEFAULT false,
  input_rate_micros_per_1m   BIGINT,                 -- price per 1M billable input tokens
  output_rate_micros_per_1m  BIGINT,                 -- price per 1M billable output tokens
  max_output_tokens_per_msg  INT NOT NULL DEFAULT 1500,
  requests_per_minute   INT NOT NULL
);

-- Which plan an org is on, and its billing cycle
CREATE TABLE subscriptions (
  org_id            UUID PRIMARY KEY,
  plan_id           TEXT REFERENCES plans,
  cycle_anchor_day  INT NOT NULL,                    -- monthly reset day
  spend_limit_micros BIGINT,                         -- client-set hard limit for overage
  currency          CHAR(3) NOT NULL,
  status            TEXT NOT NULL                    -- active, past_due, cancelled
);

-- Token buckets (free grants, monthly allowance, top-ups)
CREATE TABLE credit_buckets (
  bucket_id      UUID PRIMARY KEY,
  org_id         UUID NOT NULL,
  source         TEXT NOT NULL,                      -- 'free_grant','plan_monthly','topup','promo','goodwill'
  tokens_granted BIGINT NOT NULL,
  tokens_used    BIGINT NOT NULL DEFAULT 0,
  valid_from     TIMESTAMPTZ NOT NULL,
  expires_at     TIMESTAMPTZ,
  unit_price_micros_per_1m BIGINT NOT NULL DEFAULT 0,  -- for revenue recognition
  priority       INT NOT NULL                        -- consumption order
);

-- Caps set by client admins
CREATE TABLE usage_caps (
  cap_id       UUID PRIMARY KEY,
  org_id       UUID NOT NULL,
  scope_type   TEXT NOT NULL,                        -- 'org','workspace','user'
  scope_id     UUID NOT NULL,
  period       TEXT NOT NULL,                        -- 'day','month'
  max_tokens   BIGINT NOT NULL,
  hard         BOOLEAN NOT NULL DEFAULT true
);

-- Short-lived holds during a request
CREATE TABLE token_reservations (
  reservation_id UUID PRIMARY KEY,
  org_id UUID NOT NULL, user_id UUID NOT NULL, workspace_id UUID,
  tokens_reserved BIGINT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at TIMESTAMPTZ NOT NULL,
  status TEXT NOT NULL                               -- 'held','settled','released','expired'
);

-- usage_events: append-only ledger (defined in CHAT_ENGINE_SLM_PLAN.md §3.6)
-- plus: bucket_id debited, workspace_id, client/brand tag for agency re-billing.
```

---

## 5. Invoicing on token consumption

### 5.1 Invoice types
| Plan type | When invoiced | What the invoice shows |
|---|---|---|
| **Prepaid top-up** | At purchase (pay first) | Pack size, price, tax. Consumption appears on the monthly **usage statement**, not a new invoice |
| **Subscription + overage** | Monthly | (a) Plan fee for the **next** month, in advance; (b) **overage tokens for the past** month, in arrears |
| **Enterprise commitment** | Annually/quarterly in advance + monthly overage | Commitment fee; drawdown statement; overage lines |
| **Free tier** | Never | Usage statement only |

### 5.2 Invoice line items (example, Pro plan, monthly)
```
AgencyOS Pro plan — December 2026 (in advance)                 1 × $ ...
Included tokens used (November): 10,000,000 of 10,000,000          $ 0.00
Overage — input tokens  (November): 3,200,000  @ $ ... / 1M        $ ...
Overage — output tokens (November): 1,100,000  @ $ ... / 1M        $ ...
  ↳ Breakdown by workspace/brand: Brand A 2.1M · Brand B 1.6M · Brand C 0.6M
Credit note: failed/errored requests auto-credited                 −$ ...
Subtotal / Tax (GST/VAT per jurisdiction) / Total
Attached: usage-ledger-2026-11.csv (every billable message, timestamp, user, tokens, amount)
```
The **per-workspace/brand breakdown** matters for agencies: it lets them pass token costs through to their own clients.

### 5.3 Pipeline from ledger to invoice
```
usage_events (append-only, per model call)
   → hourly aggregation per org / workspace / user / token type
   → push usage records to the billing provider (Stripe metered prices / Chargebee / Razorpay)
     or our own invoice generator
   → at cycle close: freeze period, compute overage = usage beyond included buckets,
     apply credits, apply tax, generate invoice + usage CSV
   → reconciliation job: invoice totals == ledger totals (must match exactly; alert if not)
```

### 5.4 Rules finance must approve
- **Rounding:** bill per 1,000 tokens (or per credit) per invoice line, not per message, to avoid rounding disputes.
- **Price changes:** new rates apply only from the next billing cycle, with notice (store a `price_book_id` on every usage event).
- **Credit expiry:** monthly allowance doesn't roll over; top-ups expire after N months; free grants expire after the trial.
- **Refunds/credits:** auto-credit our own failures; credit notes for disputes; policy for unused prepaid credits.
- **Revenue recognition:** prepaid credits and commitments are **deferred revenue**. Recognise as consumed, or at expiry.
- **Tax:** GST/VAT on digital services per client location; tax IDs on B2B invoices; e-invoicing where mandated; multi-currency price books.
- **Non-payment:** past-due → grace period → chat falls back to read-only → suspend; free trial unaffected.

---

## 6. What the client sees in the product

1. **Token counter** on each chat reply (e.g., "1,240 tokens · ≈ $0.00x"), which can be hidden by an admin.
2. **Usage dashboard:** remaining balance per bucket, burn rate, forecast date of hitting the cap, breakdown by user/workspace/brand.
3. **Cap settings (admin):** org spend limit, workspace and user caps, overage on/off, alert recipients.
4. **Top-up / upgrade** buttons right where limits are hit.
5. **Billing page:** invoices, usage statements, downloadable ledger CSV, payment methods, tax details.

---

## 7. Test cases that must pass before billing goes live
- Two parallel chats with 1,000 tokens left can't spend more than 1,000 tokens in total.
- A hard cap at zero blocks new messages and lets the currently streaming reply finish within its reservation.
- Free grant expiry removes remaining free tokens at exactly the expiry time.
- Consumption follows the bucket order (free → monthly → top-up → overage).
- Overage stops exactly at the client's spend limit.
- Errored or timed-out replies are not billed.
- A retry with the same idempotency key is not billed twice.
- Invoice total == sum of ledger events for the period, to the micro-unit.
- A mid-cycle plan upgrade prorates the plan fee and adds the new allowance immediately.
- A price change mid-month bills old usage at the old rate and new usage at the new rate.

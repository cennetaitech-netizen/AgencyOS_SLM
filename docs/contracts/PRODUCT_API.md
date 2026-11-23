# AgencyOS product API: what the assistant needs

**For:** the AgencyOS product team.
**Machine-readable contract:** [`product_api.openapi.yaml`](product_api.openapi.yaml) (OpenAPI 3.1; you can generate server stubs from it).
**Check your implementation:**
```bash
python ops/check_product_api.py --base-url https://staging.agencyos.example --token <test user token>
```
Add `--write-campaign <test campaign ID>` to also check the four writes.

The assistant calls the product as the signed-in user, to read campaign data and, after the user confirms, to make four kinds of change. Until this API exists, it runs on a mock that behaves exactly as described here (`chat_service/tools/mock_product_server.py`, the reference implementation; the checker passes 49/49 against it).

## 1. What to build

| # | Operation | Method and path (suggested) | Used for |
|---|---|---|---|
| 1 | Who am I | `GET /api/v1/me` | Signing the user in to the assistant |
| 2 | Clients | `GET /api/v1/clients` | "Which clients can I see?" |
| 3 | Campaigns | `GET /api/v1/campaigns?client_id=&status=` | Finding campaigns by client or status |
| 4 | Campaign details | `GET /api/v1/campaigns/{id}` | Current state, before any change |
| 5 | Pacing | `GET /api/v1/campaigns/{id}/pacing` | "Will it spend its budget?" |
| 6 | Performance | `GET /api/v1/performance?campaign_id=or client_id=&date_from=&date_to=&breakdown=` | Results, comparisons, trends |
| 7 | Help search | `GET /api/v1/help/search?query=` | "How do I…?" |
| 8 | Change budget or name | `PATCH /api/v1/campaigns/{id}` with `{"budget": …}` or `{"name": …}` | Confirmed changes |
| 9 | Pause | `POST /api/v1/campaigns/{id}/pause` | Confirmed changes |
| 10 | Resume | `POST /api/v1/campaigns/{id}/resume` | Confirmed changes |

**Different paths are fine.** Change the map in `chat_service/tools/product.py` (`ENDPOINTS`) and the sign-in path (`ProductAuthProvider.ME_PATH` in `chat_service/api/auth.py`). The **response shapes** matter: the assistant's prompts, grounding check and evals rely on them. A test fails if the map and the contract file drift apart.

If you already have endpoints that return the same data in a different shape, an adapter can map them. Tell us, and send an example response.

## 2. Rules

**Authentication and permissions**
- Every call carries the **signed-in user's own token** (`Authorization: Bearer …`), the same one the AgencyOS web app uses.
- The product applies that user's permissions, so the assistant can never see or change more than the user can in AgencyOS.
- Return **401** for a missing or expired token, **403** when the user may not see or change the item, and **404** for an unknown ID, or one the user can't see (your choice).
- `/me` returns `user_id`, `org_id` (UUIDs), `display_name`, and `roles`. Include `org_admin` for people who manage the organisation's assistant settings and billing.

**Data**
- Every data response says **when the figures were last refreshed** (`data_as_of`, UTC) and their **currency** (ISO code, e.g. `INR`). The assistant tells the user both.
- **Money is in major units** (`300000` = ₹3,00,000), as plain numbers. Don't format them as strings.
- Rates: `ctr_pct` is a percent (`1.93` = 1.93%), and `roas` is revenue ÷ spend (`2.74`). Use `null` when undefined (e.g. no clicks).
- Dates are `YYYY-MM-DD`, inclusive, in the org's timezone.
- Keep lists small: at most 50 performance rows and 10 help results. The assistant summarises; it doesn't page.

**Changes (writes)**
- The assistant only sends a change after the user has seen a before → after card and confirmed it. Changes above an amount also need a second approver in the assistant.
- Each write carries an **`Idempotency-Key`**. If the same key arrives again (the assistant retries after a timeout), return the first result and **don't apply the change twice**. Keep keys at least 24 hours.
- On success return `{"ok": true, "campaign": {…current state…}}`.
- **409** when the campaign's state doesn't allow it (e.g. ended). **422** when the ad platform or a product rule rejects it. Put a human-readable reason in `detail`; the assistant shows it to the user.

**Operations**
- **Speed:** aim for p95 under 1 s. The assistant gives up after 15 s.
- Log the `X-Request-ID` header: it's the assistant's request ID, so support can follow one chat message across both systems.

## 3. Switching the assistant over
1. Run the checker against staging with a test user, until every check passes.
2. Run it again with `--write-campaign` against a test campaign. It changes the campaign briefly and restores it.
3. Set the assistant's configuration:
   - `CHAT_PRODUCT_ADAPTER=http`
   - `CHAT_AUTH_PROVIDER=product`
   - `CHAT_PRODUCT_API_BASE_URL=https://…`

   The production start-up check refuses the mock and dev sign-in, so this step can't be skipped.
4. Run the assistant's eval and a pilot org in shadow mode, where usage is counted but not charged, before charging.

## 4. Open questions for the product team
- The real paths, field names and auth mechanism. If they differ, send examples and we map them.
- Role names: is there an `org_admin` equivalent, and should the assistant's chat actions be limited by role?
- Platforms covered (Google Ads, Meta, LinkedIn, …), and how fresh `data_as_of` typically is.
- Whether budget changes go to the ad platform immediately or in a sync job. This affects what "done" means on the confirmation card.

# AgencyOS Assistant widget (Phase 5)

React components that embed the assistant in the AgencyOS web app:
- `AssistantPanel`: the chat, with:
  - conversations (new, rename, delete) and streaming replies
  - tool progress
  - safe markdown and tables
  - **confirmation cards** with before → after, risk level, warnings, Confirm/Cancel and Undo
  - per-reply token count, balance header, shadow-mode label and a "What is a token?" explainer
  - low-balance and usage-alert banners
  - out-of-tokens and limit states, with sending paused and a Buy tokens button
  - 👍/👎 feedback with a reason
  - AI disclaimer, starter questions and Stop
  - keyboard and screen-reader support, dark mode, phone layout
- `BillingPage`: balance by source, token packs, plan, invoices with usage CSV, and usage limits. Admin-only parts hide for other users. Buying a pack goes to the payment provider's checkout; with manual billing it shows the payment instructions and order reference instead, and lists the org's orders (awaiting payment, paid, cancelled).
- `AdminPage`: settings for organisation admins (users without the admin role see a short "admins only" message):
  - **Approvals**: changes waiting for a second approver, with before → after, Approve and apply, or Reject. You can't approve a change you requested or confirmed.
  - **Chat actions**: switch chat actions on or off, budget safety limits (% and amount per action, second-approver threshold, daily limit), which actions are allowed, low-risk confirmation, and roles allowed per risk level.
  - **Usage limits**: token limits for the organisation (per day or month, hard stop or warning only).
  - **Plan & spend**: subscribe, upgrade (prorated now) or downgrade (from the next renewal), and the monthly overage spend limit.
  - **Billing details**: legal name, address, tax ID and billing email used on invoices.
  - **Data & privacy**: training-data consent.
- `ChatApiClient`: the API client. Streaming uses `fetch` + `ReadableStream`, because `EventSource` can't send auth headers.

## Embed
```tsx
import { AssistantPanel, ChatApiClient } from "@agencyos/assistant-widget";
import "@agencyos/assistant-widget/dist/assistant-widget.css";

const client = new ChatApiClient({
  baseUrl: "https://assistant.agencyos.example",
  getHeaders: async () => ({ Authorization: `Bearer ${await getUserToken()}` }),
});

<AssistantPanel client={client} onBuyTokens={() => navigate("/assistant/billing")} />;
```
Restyle by overriding the `--aos-*` CSS variables on `.aos-panel` / `.aos-billing` / `.aos-admin` to match the product's design system.

## Develop
Start the chat service with `CHAT_AUTH_PROVIDER=dev` on port 8090 (see the repo README), then:

```bash
npm install
```

```bash
npm run dev
```

The demo is at http://localhost:5173. `/api` is proxied to :8090, and the identity is set in the top bar.

```bash
npm test && npm run build
```

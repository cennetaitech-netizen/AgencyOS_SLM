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
- `BillingPage`: balance by source, token packs (checkout), plan, invoices with usage CSV, and usage limits. Admin-only parts hide for other users.
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
Restyle by overriding the `--aos-*` CSS variables on `.aos-panel` / `.aos-billing` to match the product's design system.

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

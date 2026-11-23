# Hosted test site (Render)

A private website where you and invited testers use the assistant:
- **Real model:** Qwen2.5-7B on Together AI.
- **Sample campaign data:** the product API isn't connected yet.
- **Free usage:** shadow billing. Tokens are counted, not charged.

Testers sign in with a personal **invite link**. Staff create links in the site's **Testers** tab.

**Cost:** about **$20 a month** on Render, plus Together usage.
- Render: web $7, jobs worker $7, Postgres $6, Redis free.
- Together usage is capped at 5M model tokens a day across all testers (`CHAT_GLOBAL_DAILY_HOST_TOKEN_CEILING`), roughly $1.50 a day at most.

## 1. Deploy (once, about 15 minutes)
You need a Render account (render.com) connected to the GitHub account that owns this repository, and your Together API key.

1. In Render, click **New → Blueprint**.
2. Pick the repository **AgencyOS_SLM** and the branch **claude/vibrant-cori-f7j44r**. Render reads `render.yaml` and lists:
   - the web service `agencyos-assistant-test`
   - the worker `agencyos-assistant-jobs`
   - the database `agencyos-assistant-db`
   - Redis (Key Value) `agencyos-assistant-redis`
3. Render asks for **CHAT_MODEL_API_KEY**. Paste your Together API key there. It stays in Render; nobody else sees it.
4. Click **Apply**. The first build takes 5–10 minutes. Database migrations run automatically before each deploy.
5. When `agencyos-assistant-test` shows **Live**, note its address, for example `https://agencyos-assistant-test.onrender.com`. Opening it shows "Open the invite link you were sent to sign in." That's expected.

Render generates the other secrets itself: the invite-link secret, the metrics token and the encryption key.

## 2. Make your own staff link
1. In Render, open `agencyos-assistant-test` → **Shell**.
2. Run:
```bash
python -m chat_service.invites create --name "Your name" --org "AgencyOS" --role staff
```
3. It prints a link. Open it in your browser. You're signed in as staff, with the tabs **Chat**, **Billing**, **Settings** and **Testers**. The browser remembers you until you click **Sign out**.

Keep this link private: anyone who has it can sign in as you.

## 3. Check the deployment (optional, 1 minute)
On your own computer, from the repository folder:
```bash
python3 ops/check_test_site.py --link "<your staff link>"
```
It checks that the page loads, that your link works and can create a tester link, and that the real model answers a message. Every line should start with `ok`.

The check adds one tester named "Site check" in an organisation of the same name. Ignore it.

## 4. Invite testers
In the site, open **Testers**, fill in the form, click **Create link**, then **Copy**, and send the link to the person privately.

| Field | What it does |
|---|---|
| Name | Shown in the app, and in the audit log and feedback |
| Organisation | Testers with the same organisation name share a token balance, settings and chat approvals. Use one name per company being tested, e.g. "Acme Media". Creating a link also switches the assistant on for that organisation |
| Role | **Org admin**: chat, billing and settings. **Planner**: chat and billing. **AgencyOS staff**: everything, plus inviting others; only for your own team |
| Valid for | Days until the link stops working (default 30) |

Links are shown only once. If someone loses theirs, make a new one. It signs them in as a new user in the same organisation, so their earlier chats stay with the old link.

**What to tell testers:** "Open this link in Chrome, Safari or Edge, on a computer or phone. Ask the assistant about campaigns, budgets, pacing, audiences or ad copy. The campaign figures are sample data. Use 👍 / 👎 under each answer, and say what was wrong when you click 👎."

## 5. Read the feedback
- Thumbs-down answers and their comments are under staff quality review: `GET /chat/admin/quality/review`. See [docs/chat/](chat/) and the staff endpoints in the README.
- Each organisation starts with the 200,000-token free trial. Top it up from the staff endpoints (goodwill grant) if a tester runs out.

## 6. Withdraw access
- **One person:** links can't be withdrawn one at a time. Make links short-lived (Valid for: 7 days) for people you're unsure about.
- **Everyone:** in Render, open `agencyos-assistant-test` → **Environment**, change **CHAT_INVITE_SECRET** to a new random value (32+ characters), and save. Every existing link stops working; make new ones as in step 2.
- **Pause the site:** suspend the web service in Render (Settings → Suspend). Nothing is charged for model usage while it's suspended.

## 7. Updating the site
Deploys are manual, so a push doesn't change what testers see mid-test. To ship the latest code, open each service in Render (`agencyos-assistant-test` and `agencyos-assistant-jobs`) and click **Manual Deploy → Deploy latest commit**.

## What's different from production
| Here | Production |
|---|---|
| Invite links (`CHAT_AUTH_PROVIDER=invite`) | Sign-in through the AgencyOS product (`product`). The app refuses `invite` in production |
| Sample campaign data (`CHAT_PRODUCT_ADAPTER=mock`) | The product API (`http`) |
| `CHAT_ENV=staging` | `production`, with the full start-up checks |
| Usage is free (shadow billing) | Shadow during the pilot, then live billing |
| The API docs page `/docs` is visible | Off |

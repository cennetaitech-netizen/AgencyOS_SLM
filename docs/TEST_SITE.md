# Hosted test site (Render)

A private website where you and invited testers use the assistant:
- **Real model:** Qwen2.5-7B on Together AI.
- **Sample campaign data:** the product API isn't connected yet.
- **Free usage:** shadow billing. Tokens are counted, not charged.

Testers sign in with a personal **invite link**. Staff create links in the site's **Testers** tab.

**Cost:** about **$20 a month** on Render, plus Together usage.
- Render: web $7, jobs worker $7, Postgres $6, Redis free.
- Together usage is capped at 5M model tokens a day across all testers (`CHAT_GLOBAL_DAILY_HOST_TOKEN_CEILING`), roughly $1.50 a day at most.

## On your own computer (Mac, Windows or Linux), no hosting account
Good for trying it yourself, and for a few testers while your computer is on.

### What gets installed
| App | Why |
|---|---|
| Xcode Command Line Tools (Mac) | `git` and `python3` |
| Homebrew (Mac) | Installs the apps below |
| Docker Desktop | Runs the site's own database (Postgres), Redis, the API and background jobs |
| cloudflared | Only for a public address for testers elsewhere |
| Ollama + one local model | Optional: free answers on your computer, no API key |

Postgres, Redis, Node and the Python packages run inside Docker, so they aren't installed on the computer. Gemini and Together need no install, only an API key.

### Steps
1. **Mac: install everything once.** In Terminal:
```bash
xcode-select --install
```
When that has finished, get the code (or `git pull` if you already have it):
```bash
git clone -b claude/vibrant-cori-f7j44r https://github.com/cennetaitech-netizen/AgencyOS_SLM.git
```
Then:
```bash
cd AgencyOS_SLM && ops/mac-setup.sh
```
It installs Homebrew, Docker Desktop, cloudflared and Ollama with one local model (`qwen2.5:7b` with 16 GB of memory or more, else `qwen2.5:3b`), skipping what's already there. Add `--no-ollama` to skip Ollama. On Windows or Linux, install Docker Desktop (docker.com) and git yourself.
2. **Start it:**
```bash
ops/test-site.sh
```
The first run builds everything, which takes a few minutes. Then it opens your **staff link** in the browser and prints it. The link is kept, so you stay the same user after restarts.

3. **Models (real answers):** open `.env.testsite` in the AgencyOS_SLM folder. You can switch on several models at once:
   - **Google Gemini:** put your key (from aistudio.google.com/apikey) after `CHAT_GEMINI_API_KEY=`. The model is `gemini-3.5-flash-lite` unless you set `CHAT_GEMINI_MODEL`.
   - **Ollama:** if Ollama was running when the file was created, its models are already filled in. Otherwise remove the `#` from the two `CHAT_OLLAMA_` lines and list your models (`ollama list`), comma-separated.
   - **Together AI:** remove the `#` from the three `CHAT_MODEL_` lines and put your Together key after `CHAT_MODEL_API_KEY=`.
   - **Any other OpenAI-compatible host:** `CHAT_MODELS` (an example is in the file).

   Then run `ops/test-site.sh` again. It prints which model answers by default: Gemini if its key is set, then Ollama, then Together. Until you add one, every answer is the same canned sentence. Keys stay on your computer.

   Staff choose the default model in the **Models** tab. To give one organisation its own model, pick it when you create its tester link (**Testers** tab).
4. **Testers elsewhere:** cloudflared is installed by `ops/mac-setup.sh` (or `brew install cloudflared`). Run:
```bash
ops/test-site.sh --public
```
It prints a public `https://….trycloudflare.com` staff link. Open it, and create tester links in the **Testers** tab there, so the links use the public address.
   - The address changes each time the tunnel restarts, so links you've sent stop working. Send new ones.
   - It only works while your computer is awake and online. On a Mac, `caffeinate -dims` in another terminal keeps it awake.
   - For a fixed address, use Render (below).
5. **Stop:** `ops/test-site.sh --stop` keeps the data. `ops/test-site.sh --reset` deletes it.

Testers on the same Wi-Fi can also use `http://<your computer's IP address>:8090`, without a tunnel.

### What the assistant answers
- **Media questions** (campaigns, planning and buying, performance, budgets and pacing, audiences, creatives and ad copy, reporting, AgencyOS how-to): answered, with figures only from the campaign data (sample data on the test site).
- **Anything else** (for example "Who is the Prime Minister of …?"): no answer. It says it only helps with media campaigns and AgencyOS, and adds links to search the question on Google, Bing, DuckDuckGo and Wikipedia. These replies aren't charged. Set `CHAT_SEARCH_LINKS_ON_REFUSAL=false` to leave the links out.

## 1. Deploy on Render (once, about 15 minutes)
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

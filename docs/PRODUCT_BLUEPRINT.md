# AgencyOS — Media Campaign SLM: Product Blueprint

*Working name: **AgencyOS**. This is a plan for building a small language model (SLM) for media campaigns and selling it online as an off-the-shelf product to agencies and advertisers.*

---

## 0. TL;DR

1. **Selling a model alone won't work. Sell a vertical AI product with an SLM inside it.** Customers buy outcomes: a media plan in 10 minutes instead of 2 days, a weekly cross-channel report written for them, budget moves that improve CPA. The SLM is the engine. The product is SLM + media-math tools + the customer's data + a workflow app.
2. **Don't train a model on day one. Run a frontier model first, then distil it.** Build the first version on a frontier LLM API with retrieval and tools. Use paying design partners to collect real tasks and expert corrections. Then fine-tune an open-weight 3–14B model on that data. The SLM gives you margin, speed, privacy and private deployment. The frontier version gives you speed to market and the training data.
3. **The model must never make up a number.** Reach, frequency, CPMs, forecasts and budget splits come from deterministic tools and data: calculators, optimisers, platform APIs. The model reasons, writes and calls tools.
4. **Your moat is data and workflow, not weights.** That means proprietary plan and performance data, expert-graded examples, connectors, and fitting into how agencies actually work.
5. **Realistic path:** about 3 months to a working prototype with design partners, about 6 months to a sellable MVP, about 9 months to self-serve online launch, and 12–18 months to enterprise-grade (SOC 2, private deployment, automation).

---

## 1. What exactly are we building?

**Definition:** A domain-specialised language model and the application around it. Together they do the core jobs of a media planner, buyer and analyst, across the whole campaign lifecycle:

```
Brief ─► Strategy ─► Media Plan ─► Creative/Copy ─► Trafficking ─► Launch QA ─► Monitor ─► Optimise ─► Report
```

| Lifecycle stage | What the product does | Who does the work |
|---|---|---|
| **Brief intake** | Parses a messy brief (PDF, email, call notes) into a structured brief: objective, KPIs, budget, flight, geo, audience, constraints. Asks about anything missing. | SLM (extraction) |
| **Strategy** | Proposes the funnel approach, channel roles and audience hypotheses, with a written rationale. | SLM + RAG over past plans and category knowledge |
| **Media plan** | Builds the channel mix, budget split, flighting, and estimated reach, frequency, impressions, clicks and conversions. Exports to Excel, Sheets or PPT. | **Optimiser + benchmark/forecast tools** produce the numbers. The SLM explains them. |
| **Creative & copy** | Writes ad copy variants within each platform's limits (for example, Google RSA headlines are capped at 30 characters and descriptions at 90). Also produces creative briefs and spec sheets. | SLM + a spec validator |
| **Trafficking** | Generates naming conventions, UTM taxonomy, campaign and ad-set structure, and pixel/tag checklists. | SLM + rules engine |
| **Launch QA** | Compares live campaign settings against the approved plan: budgets, dates, geo, targeting, tracking. | Connector + rules, with the SLM summarising |
| **Monitor & optimise** | Detects anomalies, tracks pacing, and recommends budget shifts with expected impact. Applies them only after human approval. | Stats/forecast tools + SLM recommendations |
| **Report** | Writes the cross-channel weekly or monthly report, insights and next steps, branded per client. | Connectors + metric layer + SLM narrative |

**Where an SLM is used and where it isn't:**
- **SLM:** extraction, classification, structured generation (plans as JSON), copywriting, explanations, summaries, tool orchestration.
- **Not the SLM:** arithmetic, forecasts, benchmark lookups, budget optimisation, anything that has to be auditable. Those run in code.

---

## 2. Why an SLM, and when not to use one

| Reason to build an SLM | What it means commercially |
|---|---|
| **Cost** | 10–50x cheaper per task than a frontier API at scale, so you keep SaaS gross margins (target above 75%). |
| **Latency** | Faster responses for interactive planning. |
| **Privacy / private deployment** | Agencies and brands won't send client data everywhere. A 7–8B model can run in the customer's VPC or on-prem. **This is a major enterprise selling point.** |
| **Consistency** | Fine-tuned on house formats and taxonomies, so it reliably outputs valid plan JSON. |
| **IP / defensibility** | You own the weights and the training data pipeline. |

**When not to rely on the SLM:** novel strategic reasoning, long multi-document analysis, and rare edge cases. Use a **router**: the SLM handles about 80% of routine traffic and escalates hard tasks to a frontier model. Over time that escalated traffic becomes training data that shrinks the 20%.

---

## 3. Target customers and the wedge

### Customer segments (ranked by speed to revenue)

| Segment | Pain | Buying power | Sales motion | Recommendation |
|---|---|---|---|---|
| **Independent / mid-size agencies (10–300 staff)** | Planners overloaded, reporting eats 30–40% of time, margin pressure | Medium; fast decisions | Founder-led sales, then self-serve | **Primary ICP** |
| **In-house brand marketing teams** | Thin teams, many platforms, need agency-grade planning | Medium–high | Sales-assisted | Secondary (Phase 3+) |
| **SMB advertisers / freelancers** | No expertise, wasted spend | Low per seat, high volume | Pure self-serve, app marketplaces | Low-touch tier (Phase 3+) |
| **Holding companies / large networks** | Building their own tools, but want private models | High; slow (9–18 months) | Enterprise, private deployment | Phase 4 |

### The wedge (start narrow)

Don't launch all eight lifecycle stages. Launch **two linked workflows** for **two platforms**:

1. **"Brief → Media Plan in 10 minutes"** for Google Ads + Meta. It uses the agency's own rate cards and historical performance, plus default benchmarks.
2. **"Auto cross-channel report + optimisation recommendations"** for the same platforms. It is recurring, so it drives retention, and it creates the performance data that makes plans smarter.

The plan feeds the report and the report feeds the next plan. That closed loop is your data flywheel.

---

## 4. System architecture

```
                         ┌─────────────────────────────────────────────┐
                         │           Web App (Next.js / React)          │
                         │  Briefs · Plans · Reports · Approvals · Admin │
                         └───────────────▲─────────────────────────────┘
                                         │ REST/GraphQL + SSE streaming
┌────────────────────────────────────────┴──────────────────────────────────────┐
│                        API / Orchestration Layer (Python FastAPI)               │
│  Auth & tenancy · Workflow engine (Temporal) · Agent/tool loop · Guardrails     │
│  Model router: SLM ◄──► frontier fallback · Prompt/version registry · Audit log │
└───────┬──────────────┬───────────────────┬───────────────────┬─────────────────┘
        │              │                   │                   │
┌───────▼──────┐ ┌─────▼────────┐ ┌────────▼─────────┐ ┌───────▼──────────────┐
│ Model Serving │ │  Knowledge    │ │  Media Tools      │ │  Data Platform       │
│ vLLM / SGLang │ │  (RAG)        │ │  (deterministic)  │ │                      │
│ Base SLM +    │ │ pgvector/     │ │ • Media math      │ │ Connectors: Google   │
│ per-tenant    │ │ Qdrant        │ │   (CPM/CPC/CPA,   │ │ Ads, Meta, GA4, TTD, │
│ LoRA adapters │ │ • Past plans  │ │   reach/freq)     │ │ DV360, TikTok, LinkedIn│
│ Structured    │ │ • Brand guides│ │ • Budget optimiser│ │ Amazon Ads           │
│ (JSON) output │ │ • Rate cards  │ │   (LP / response  │ │ Warehouse: Postgres →│
│               │ │ • Platform    │ │   curves / MMM)   │ │ BigQuery/Snowflake   │
│               │ │   specs/policy│ │ • Forecast APIs   │ │ Metric layer (dbt)   │
│               │ │               │ │ • Spec validator  │ │ Benchmarks DB        │
└───────────────┘ └──────────────┘ └──────────────────┘ └──────────────────────┘
        ▲
┌───────┴──────────────────────────────────────────────────────────────────────┐
│  ML Platform: data curation (Argilla/Label Studio) · training (TRL/Axolotl/    │
│  Unsloth, LoRA/QLoRA, DPO) · eval harness · experiment tracking (W&B/MLflow) · │
│  LLM tracing (Langfuse) · feedback capture → next training set                │
└──────────────────────────────────────────────────────────────────────────────┘
```

**Key design principles**
- **Structured outputs everywhere.** Plans, briefs and recommendations are JSON-schema-validated objects, not free text. Use guided decoding (vLLM/Outlines).
- **Tool-first numerics.** The model must call `calc_reach_frequency()`, `optimise_budget()` or `get_benchmark()` to get numbers.
- **Human-in-the-loop.** Every write action to an ad platform (budget change, pause, launch) needs explicit approval and is logged.
- **Multi-tenant by design.** Each customer's data, embeddings and LoRA adapter are isolated (row-level security plus tenant-scoped storage).
- **Model-agnostic router.** Swap base models as better open-weight releases appear without rewriting the app.

---

## 5. Data strategy (the real moat)

### 5.1 Data sources

| Source | Examples | Use | Rights / risk |
|---|---|---|---|
| **Design-partner historical artefacts** | Briefs, media plans, IOs, flowcharts, post-campaign reports, decks | SFT examples, RAG, eval sets | **Needs written permission. Client data usually belongs to the advertiser.** Anonymise. |
| **Platform performance data (via API)** | Spend, impressions, CTR, CPA and ROAS by channel, format, audience, geo, time | Benchmarks, forecasting, optimisation, report generation | Follow each platform's API terms on storage and use. Aggregate and anonymise for benchmarks. |
| **Public / licensed knowledge** | Platform help docs and ad specs, IAB Content and Audience Taxonomies, advertising codes (e.g., FTC, ASA, ASCI), industry reports | RAG, continued pretraining corpus | Check licences; prefer linking and retrieval over training on copyrighted text |
| **Expert-authored examples** | In-house media planners write and grade "gold" outputs | Eval sets, SFT, preference pairs | You own it. **The highest-value data you will have.** |
| **Synthetic data** | Frontier model generates briefs and plans across verticals, budgets and markets; experts filter and correct | Scale SFT coverage | Check the frontier provider's terms on using outputs to train models |
| **Product usage logs** | Edits users make to drafts, accept/reject on recommendations | DPO / preference tuning, continuous improvement | Covered by your ToS/DPA; opt-out for training |

### 5.2 Datasets to build

| Dataset | Size for v1 | Purpose |
|---|---|---|
| Instruction / SFT set | 5k–30k high-quality examples across ~15 task types | Teach tasks and output formats |
| Tool-use traces | 2k–5k multi-step trajectories | Teach when and how to call media tools |
| Preference pairs (chosen vs rejected) | 2k–10k | DPO / ORPO to align with expert taste |
| **Gold evaluation set** | 300–1,000 examples, frozen, expert-graded | Go/no-go gate for every model release |
| Domain corpus (optional) | 50M–500M tokens of media/advertising text | Continued pretraining, only if evals show a vocabulary/knowledge gap |

### 5.3 Task taxonomy (training targets)
`brief_extraction`, `brief_gap_questions`, `objective_to_kpi_mapping`, `channel_mix_rationale`, `audience_definition`, `plan_json_generation` (with tool calls), `flighting_proposal`, `ad_copy_generation` (per platform spec), `creative_brief`, `naming_convention`, `utm_builder`, `launch_qa_diff_summary`, `anomaly_explanation`, `optimisation_recommendation`, `report_narrative`, `client_email_summary`.

---

## 6. Model development pipeline

### 6.1 Base model selection
Pick a current-generation open-weight model in the **3B–14B** range. Candidate families include Qwen, Llama, Mistral, Gemma and Phi; use the latest release at build time. Selection criteria:

| Criterion | Why |
|---|---|
| **Licence** (e.g., Apache-2.0 vs custom licences with usage caps) | You are reselling, so legal must clear it |
| Function-calling and JSON reliability | The core of the product |
| Multilingual ability | If you sell beyond English-speaking markets |
| Context length (32k+) | Long briefs and reports |
| Size vs hardware | 7–8B quantised runs on a single mid-range GPU, which suits private deployment |

**Do a bake-off:** fine-tune 2–3 candidates on the same data and compare on the gold eval set.

### 6.2 Training stages
```
1. (Optional) Continued pretraining on the media corpus   → domain vocabulary
2. Supervised fine-tuning (LoRA/QLoRA) on the SFT + tool-use set → task skills
3. Preference tuning (DPO/ORPO) on expert-graded pairs     → quality and taste
4. Per-tenant LoRA adapters (agency style, rate cards, taxonomy) → personalisation
5. Quantisation (AWQ/GPTQ for GPU, GGUF for on-prem CPU/edge) → cheap serving
```
Most of the value comes from steps 2 and 3. Do step 1 only if evals show it's needed.

### 6.3 Evaluation (the release gate)

| Metric type | Examples | How it's measured |
|---|---|---|
| **Hard validity checks** | Budget sums to the total, dates inside the flight, JSON schema valid, copy within character limits, no forbidden claims | Automatic, 100% coverage, **must pass ≥ 99%** |
| **Numeric fidelity** | Every number in the output traces to a tool call | Automatic trace check |
| **Task quality** | Strategic soundness, relevance, clarity | Expert rubric (1–5) on a sample, plus LLM-as-judge calibrated to the experts |
| **Head-to-head win rate** | SLM vs frontier baseline vs the previous SLM version | Blind pairwise review |
| **Business metrics** | Edit distance from draft to final, acceptance rate, time saved | Product analytics |
| **Ops** | p95 latency, cost per task, tokens per task | Tracing |

**Rule:** a task moves from the frontier model to the SLM only when the SLM matches or beats it on that task's eval slice.

### 6.4 Retraining cadence
Retrain monthly or quarterly from fresh feedback and platform changes (new ad formats, policy updates). Version every model, dataset and prompt, and keep the ability to roll back.

---

## 7. Product and SaaS engineering

### 7.1 Recommended stack

| Layer | Choice |
|---|---|
| Frontend | Next.js + React + TypeScript, a component library, AG Grid for plan tables |
| Backend | Python FastAPI; Temporal or Celery for long-running workflows and scheduled syncs |
| Database | PostgreSQL (with row-level security for tenancy) + pgvector; Redis |
| Analytics warehouse | BigQuery or Snowflake + dbt metric layer (start with Postgres) |
| Model serving | vLLM (multi-LoRA) on GPU instances; or managed inference (Bedrock custom model import, SageMaker, Together, Fireworks, Modal, RunPod) |
| Orchestration | A thin in-house agent loop, or LangGraph; Outlines / guided decoding for JSON |
| Auth | Auth0 / Clerk / WorkOS (WorkOS for enterprise SSO/SCIM) |
| Billing | Stripe Billing (subscriptions + metered usage) |
| Exports | Excel (openpyxl), Google Sheets API, PowerPoint (python-pptx), PDF |
| Observability | Langfuse (LLM traces), Sentry, Datadog / Grafana |
| Cloud | AWS, GCP or Azure; pick based on target marketplace and credits programmes |
| Security | Encryption at rest and in transit, KMS, secrets manager, audit logs, SOC 2 controls from day one (Vanta/Drata) |

### 7.2 Multi-tenancy and private deployment
- **Shared SaaS (default):** one base model and per-tenant LoRA adapters loaded on demand. This is very cost-efficient.
- **Dedicated (Enterprise):** single-tenant cluster in your cloud.
- **Private / VPC / on-prem (Enterprise+):** ship a container bundle (Helm chart) with the quantised SLM, so the customer's data never leaves their environment. **This is where an SLM beats every API-only competitor.**

---

## 8. Integrations (start these early: approvals take weeks to months)

| Platform | API | Gating / lead time |
|---|---|---|
| Google Ads | Google Ads API | Developer token approval (Basic access needs a review); OAuth app verification |
| Meta (Facebook/Instagram) | Marketing API | App review + business verification; advanced access needed for client accounts |
| GA4 | Analytics Data API | OAuth verification |
| TikTok | TikTok API for Business | App approval |
| LinkedIn | Marketing API | Partner programme application, which is selective |
| Amazon Ads | Amazon Ads API | Registration and approval |
| The Trade Desk / DV360 | Partner APIs | Partner agreements; usually later-stage |

**Shortcut for the MVP:** use a data aggregator (e.g., Supermetrics, Funnel, Windsor.ai, Fivetran, Improvado) or accept CSV/Sheets uploads. That gets you to market while your own API approvals go through.

**Workspace integrations:** Slack/Teams (alerts, approvals), Google Drive/SharePoint (brief intake), HubSpot/Salesforce (client and deal context, later).

---

## 9. Productisation: how it's packaged and sold online

### 9.1 Packaging and pricing (illustrative; validate with design partners)

| Tier | For | Includes | Indicative price |
|---|---|---|---|
| **Starter** (self-serve) | Freelancers, SMBs | 1–2 seats, 2 ad platforms, N plans/reports per month | ~$99–199 / month |
| **Pro / Agency** (self-serve + onboarding) | Agencies of 10–100 staff | 5–25 seats, all supported platforms, white-label reports, per-client workspaces, custom templates | ~$500–2,500 / month |
| **Enterprise** (sales-led) | Large agencies, brands | SSO/SCIM, custom LoRA trained on their data, dedicated or VPC deployment, SLA, audit | $25k–250k+ / year |
| **Private Model Licence** | Holding companies, regulated brands | On-prem SLM bundle + updates + support | Annual licence |

**Pricing levers to test:** per seat, per managed ad account, usage credits (plans/reports generated), or a small percentage of managed ad spend (common in adtech, but harder for agencies to accept).

### 9.2 Where to sell online
1. **Your own website:** free trial (14 days) or freemium, Stripe checkout, in-app onboarding, a sample-data sandbox.
2. **Cloud marketplaces:** AWS Marketplace, Google Cloud Marketplace, Azure Marketplace. Enterprises can buy against existing cloud commitments, which shortens procurement.
3. **App marketplaces:** Google Workspace Marketplace (Sheets add-on), HubSpot App Marketplace, Salesforce AppExchange, Slack App Directory, Shopify App Store (for SMB advertisers).
4. **Platform partner directories:** Google Partners / Meta Business Partners (once eligible), for credibility and leads.
5. **Product-led channels:** free tools as lead magnets, such as a media plan template generator, a UTM builder, an ad copy character-limit checker, or a CPM benchmark explorer.

### 9.3 Self-serve onboarding flow
```
Sign up → Choose role (agency / brand) → Connect Google & Meta (or upload CSV) →
Auto-import last 12 months → "Your first report in 5 minutes" (aha moment) →
Upload a brief → Get a plan → Invite team → Trial converts to paid
```

---

## 10. Go-to-market

| Phase | Motion | Actions |
|---|---|---|
| **Design partners (months 0–4)** | Founder-led | 3–5 agencies, discounted or free pilot in exchange for data access, weekly feedback and a case study |
| **Early revenue (months 4–9)** | Founder-led sales + content | LinkedIn thought leadership (planners are on LinkedIn), webinars ("AI media planning live"), case studies with time saved, agency association events |
| **Launch (month ~9)** | Product-led | Self-serve trial, Product Hunt / industry press, marketplace listings, referral programme for agencies |
| **Scale (12+ months)** | Sales + partners | AE/SDR hires, reseller partnerships with martech consultancies, enterprise pilots |

**Positioning:** *"The AI media planner that works across every platform, learns your agency's way of working, and can run privately on your own infrastructure."*

**Competitive landscape to track:** the platforms' own AI (Google Performance Max / Gemini in Ads, Meta Advantage+), holding-company platforms, and AI-for-ads and reporting startups. **Your differentiation:** cross-platform neutrality (platform AI optimises for its own inventory), agency-workflow fit, explainable numbers, and private deployment.

---

## 11. Legal, compliance and trust

- **Company and IP:** company incorporation, IP assignment for all contributors, trademark check on the product name.
- **Customer contracts:** Terms of Service, Master Subscription Agreement, **Data Processing Agreement**, an explicit clause on whether customer data is used for training (default: opt-in for shared models; always isolated per-tenant adapters), SLA.
- **Privacy laws:** GDPR (EU/UK), CCPA/CPRA (California), India's DPDP Act 2023, and others depending on where you sell. Work with aggregated, non-PII campaign data wherever possible.
- **AI regulation:** EU AI Act (this product is likely limited-risk, which means transparency obligations); AI disclosure in the UI.
- **Advertising compliance:** a guardrail layer for regulated categories (health, finance, alcohol, gambling, political) and local advertising codes. Generated copy is labelled "draft, requires human review".
- **Platform terms:** comply with each ad platform's API terms (data storage, display, and use for model training). **Review these before using API data for training.**
- **Model licences:** open-weight base model licence cleared for commercial redistribution, including on-prem bundles.
- **Security certifications:** SOC 2 Type I (around month 9), then Type II (around month 15). ISO 27001 if selling to EU/APAC enterprises.

---

## 12. Team and budget

### 12.1 Core team for MVP (≈ 6–7 people)

| Role | Why |
|---|---|
| Product lead with media/agency domain expertise (could be a founder) | Defines workflows, sells to agencies |
| ML / LLM engineer (fine-tuning, eval, serving) | SLM pipeline |
| 2 × full-stack engineers | App, orchestration, exports |
| Data / integrations engineer | Connectors, warehouse, metric layer |
| Senior media planner / analyst (SME), full- or part-time | Writes gold data, grades outputs, defines "good" |
| Product designer (part-time or contract) | Planning UX is a big differentiator |

Add later: sales/AE, customer success, DevOps/security, second ML engineer.

### 12.2 Indicative non-salary costs (verify current prices)

| Item | Indicative range |
|---|---|
| Frontier LLM API during prototype and synthetic data generation | $1k–5k / month |
| Fine-tuning experiments (LoRA on 7–14B, rented H100/A100) | $2k–15k total for v1 |
| SLM inference (1–3 GPU replicas, quantised 7–8B) | ~$700–4k / month at start |
| Cloud (app, database, warehouse) | $500–3k / month at MVP scale |
| Data aggregator (if used) | $300–2k / month |
| SaaS tooling (auth, observability, Vanta/Drata, Stripe fees) | $500–2k / month + 2.9%-ish payment fees |
| SOC 2 audit | $15k–40k |
| Legal (contracts, privacy, model-licence review) | $10k–30k initial |

**Total MVP:** roughly **35–45 person-months of team effort + ~$50k–150k in infrastructure, tooling and compliance over the first 9–12 months.** Team cost dominates and depends on geography. Apply for cloud startup credit programmes (AWS Activate, Google for Startups, Microsoft for Startups), which can cover much of the GPU and cloud spend.

---

## 13. Roadmap and phase gates

### Phase 0 — Discovery & validation (weeks 0–4)
- 20+ interviews with planners, buyers, analysts and agency owners. Rank the top 10 tasks by **time spent × frequency × willingness to pay**.
- Sign 3–5 **design partners** (LOI + data-sharing agreement).
- Collect 100+ real artefacts (briefs, plans, reports) under NDA.
- **Apply for Google Ads and Meta API access now.**
- Lock in the wedge, ICP and success metrics.
- **Exit gate:** at least 3 partners committed; a clear "if you solved X, we'd pay Y" statement.

### Phase 1 — Prototype on a frontier model (months 1–3)
- Build the brief → plan → report loop on a frontier LLM API + RAG + media tools (CSV input is fine).
- Build the **gold eval set** (300+ examples) with the SME.
- Weekly pilots with design partners; log every edit and rejection.
- **Exit gate:** partners use it weekly; planning time cut by at least 50% on pilot briefs; at least one partner willing to pay.

### Phase 2 — SLM v1 + sellable MVP (months 3–6)
- Curate the SFT, tool-trace and preference datasets from logs + experts + synthetic data.
- Base-model bake-off → SFT → DPO → quantise → deploy behind the router.
- MVP SaaS: auth, multi-tenancy, billing, Google + Meta connectors, Excel/PPT export, approvals.
- **Exit gate:** SLM ≥ frontier parity on at least 70% of task slices; at least 3 paying customers; gross margin per task on target.

### Phase 3 — Online launch (months 6–9)
- Self-serve signup, trial, onboarding, pricing page, docs and help centre.
- Add TikTok, LinkedIn and GA4; white-label reports; Slack alerts.
- SOC 2 Type I; first marketplace listing.
- **Exit gate:** at least 20 paying accounts; trial-to-paid conversion ≥ 10–15%; monthly logo churn < 3%.

### Phase 4 — Scale and enterprise (months 9–18)
- Optimisation with **approved write-back** (budget shifts, pauses), MMM/incrementality integration (e.g., open-source MMM frameworks such as Meta Robyn or Google Meridian), programmatic (DV360/TTD).
- Per-tenant custom models, VPC/on-prem bundle, SSO/SCIM, SOC 2 Type II.
- Multilingual / multi-market support.
- **Exit gate:** enterprise pilots converting; net revenue retention > 110%.

---

## 14. Risks and mitigations

| Risk | Mitigation |
|---|---|
| **Hallucinated numbers destroy trust** | Tool-only numerics, trace checks, "show your work" UI with sources for every figure |
| **No rights to train on client data** | Explicit DPA clauses, anonymisation, per-tenant isolation, expert-authored and synthetic data as the core training set |
| **API access delays or revocation** | Apply in week 1; aggregator/CSV fallback; diversify platforms |
| **Ad platforms ship their own AI** | Position on cross-platform neutrality, agency workflow, private deployment |
| **The SLM underperforms the frontier model** | Router + fallback; move tasks to the SLM only when evals pass |
| **Open-weight base model licence issues** | Legal review; prefer permissive licences; keep the training pipeline model-agnostic |
| **Domain drift (new formats, policies)** | Monthly or quarterly retraining; RAG over always-fresh specs and policy docs |
| **Long agency sales cycles** | Self-serve tier, design-partner case studies, marketplace procurement |
| **Security incident / data leak** | SOC 2 controls from day one, tenant isolation tests, pen tests before enterprise |

---

## 15. KPIs to run the business

- **Value:** hours saved per plan and per report; plan acceptance rate; edit distance from draft to final; uplift in client KPIs (CPA/ROAS) on optimised campaigns.
- **Product:** time to first value (under 10 minutes), weekly active planners, plans/reports per account per month.
- **Model:** eval pass rate per task, share of traffic served by the SLM vs the frontier model, cost per task, p95 latency.
- **Business:** MRR/ARR, trial-to-paid conversion, CAC payback (< 12 months), gross margin (> 75%), logo and revenue churn, NRR.

---

## 16. First 30 days: concrete checklist

- [ ] Write a one-page problem statement and ICP; pick the wedge (default: Brief → Plan + Auto-Report, Google + Meta).
- [ ] Book 20 discovery interviews; use a fixed script (tasks, time spent, tools, budget owner, data access).
- [ ] Draft a design-partner agreement (pilot terms, data rights, feedback cadence, case-study rights).
- [ ] Apply for a Google Ads API developer token and Meta Marketing API access; register OAuth apps.
- [ ] Hire or contract a senior media planner as the SME.
- [ ] Define v1 JSON schemas: `Brief`, `MediaPlan`, `LineItem`, `Recommendation`, `Report`.
- [ ] Build the media math library (CPM/CPC/CPA/ROAS, reach and frequency approximations, pacing) with unit tests.
- [ ] Stand up the prototype: brief upload → structured brief → plan (frontier LLM + tools) → Excel export.
- [ ] Start the gold eval set (first 50 examples) and an automatic validity checker.
- [ ] Apply to cloud startup credit programmes.
- [ ] Engage a lawyer for ToS, DPA and the model-licence review.

---

## 17. Decisions needed from leadership

1. **Primary ICP:** independent agencies (recommended) vs in-house brands vs SMB self-serve.
2. **Wedge workflows and platforms:** Brief → Plan + Reporting on Google + Meta (recommended), or a different pair.
3. **Launch markets and languages:** these drive privacy law, base-model choice and benchmarks.
4. **Build vs partner for connectors:** own APIs (slower, higher margin) vs aggregator (faster).
5. **Deployment promise:** whether private/VPC deployment is in the pitch from day one (strong differentiator, more engineering).
6. **Pricing model:** per seat vs per ad account vs usage credits vs a percentage of spend.
7. **Funding path:** bootstrap via services revenue from design partners, or raise a pre-seed/seed round once Phase 1 shows traction.

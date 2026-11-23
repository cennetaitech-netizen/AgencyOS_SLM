# Model host: prefix caching

Every model call resends the same opening: the system prompt and the tool schemas (about 1,355 tokens read-only, 1,730 with chat actions), then the conversation so far. Billable text is only the user's message and the visible answer, about 70–100 tokens. Without caching, the host computes the whole prompt every time, about 25× the billable tokens.

**Prefix caching** lets the host reuse the work for any opening it has seen recently. The chat service lays prompts out for it (`chat_service/engine/prompting.py`):

1. **System prompt:** identical for every org, user and day (`system_v3`, `system_v3_actions`, `system_ft_v2` have no placeholders).
2. **Tool schemas:** identical for every org with the same tools. Validation-only keywords are left out of what the model sees, but arguments are still validated against the full schema.
3. **Conversation history:** unchanged from the previous turn.
4. **Latest user message:** starts with a `[Context]` line (date, timezone, currency, role). It isn't billed and isn't stored in history.

`tests/test_prompt_cache.py` fails if anything personal or dated moves back into parts 1–3, or if the prefix grows past its budget.

In step 4 the context leads the user's message by default. `CHAT_PROMPT_CONTEXT_PLACEMENT=system_message` puts it in its own short system message instead; that scored lower on a 3B model ([docs/chat/PROMPT_V3_LOCAL_EVAL.md](../docs/chat/PROMPT_V3_LOCAL_EVAL.md)) and some chat templates reject a second system message, so re-run the eval if you switch.

## Turn it on

**Self-hosted vLLM**
```bash
vllm serve <base-model> --enable-prefix-caching --enable-prompt-tokens-details \
  --enable-lora --lora-modules media-slm-v1=ml/runs/dpo-v1 --max-lora-rank 16
```
- `--enable-prefix-caching` reuses cached prompt blocks. It is on by default in recent vLLM versions; set it explicitly anyway.
- `--enable-prompt-tokens-details` makes vLLM report `usage.prompt_tokens_details.cached_tokens`. Without it, the dashboard shows "Host doesn't report cache hits" and the cost report can't credit the cache.
- The cache is per model and per LoRA adapter. During a canary, the new adapter's cache warms up separately; that takes seconds.
- **Several replicas:** each has its own cache. The shared system and tool prefix warms up on every replica within seconds. To also reuse conversation history, send a conversation's messages to the same replica, using a prefix-aware or session-sticky router (for example the vLLM production-stack router).

**Managed hosts** (Fireworks, Together, DeepInfra, ...): prompt caching is usually automatic. Check that the host returns `prompt_tokens_details.cached_tokens` in streamed usage, and ask for the cached-input price.

## Chosen host: Together AI
Decided 2026-11-23: **Together AI** serving **Qwen2.5-7B-Instruct** (`Qwen/Qwen2.5-7B-Instruct-Turbo`), the same family as the 1.5B and 3B models checked locally.

**Run the release gate in CI (the key stays in GitHub):**
1. In GitHub, open the repository's Settings → Secrets and variables → Actions, and add three repository secrets:
   - `EVAL_MODEL_BASE_URL` = `https://api.together.xyz/v1`
   - `EVAL_MODEL_NAME` = `Qwen/Qwen2.5-7B-Instruct-Turbo`
   - `EVAL_MODEL_API_KEY` = your Together API key
2. Open Actions → **eval** → **Run workflow**. It also runs by itself whenever prompts, tools, actions, the model client or the eval set change.
3. The job fails if the gate isn't met: 85% overall, 95% on actions, 100% grounded, nothing executed without confirmation. Download the `eval-report` artifact for per-case results, and check m04 and r13 first (see [docs/chat/PROMPT_V3_LOCAL_EVAL.md](../docs/chat/PROMPT_V3_LOCAL_EVAL.md)).

**Or run it locally:**
```bash
TOGETHER_API_KEY=... python -m chat_service.evals.engine_eval --models evals/models.together.json --database-url postgresql+asyncpg://chat:chat@localhost:5432/chat_eval --out eval-together.md
```

**Check on the first run:**
- Tool calls come back as OpenAI `tool_calls`. Together lists function calling per model; if this model doesn't support it, every tool case fails.
- Streamed usage includes `prompt_tokens_details.cached_tokens`. If not, the dashboard shows "Host doesn't report cache hits" and cost is counted without the cache discount. Ask Together how cached input is billed, and set `CHAT_HOST_PRICE_CACHED_INPUT_PER_1M_MINOR`.
- "Turbo" models are served quantised. The eval measures the quality you actually get.
- For the Phase 11 fine-tuned model, check whether Together serves your own LoRA adapter on this base model. If not, plan a dedicated endpoint or vLLM.

## Tell finance the price
```bash
CHAT_HOST_PRICE_CACHED_INPUT_PER_1M_MINOR=<price per 1M cached prompt tokens>
```
Use the managed host's cached-input price. On your own GPUs, cached tokens skip the prefill compute, so use a small fraction of the input price. Unset means "same as normal input", which overstates cost but is safe. `GET /chat/admin/economics` then reports `cache_hit_pct`, `overhead_ratio` (all host tokens) and `uncached_overhead_ratio`, the one compared with `max_overhead_ratio`.

## Check it's working
- Dashboard → **Tokens and margin**: "Prompt cache hit rate" should sit well above 50%, and "Uncached cost ratio" well under 20×.
- Alert `AosPromptCacheHitLow` (below 50% for an hour) points to one of three causes: caching off, a prompt change that broke the shared prefix, or traffic not reaching warm replicas.

## What to expect
Measured with the first v3 draft. The shipped prompt is about 50 tokens longer, which changes these figures only slightly.

Two estimates, both from this environment with the stand-in host (`ops/fake_model_host.py` models vLLM's block cache):

**Simulation:** `ops/prompt_cache_sim.py`, a 9-hour day of 2,000 users in 300 orgs, about 14,900 messages, 60% calling a data tool, cache sizes from 100k to 1M tokens.

| Layout | Cache hit | Uncached host tokens per message | Uncached ÷ billable |
|---|---|---|---|
| No caching (where we were) | 0% | about 2,600 | about 26× |
| Caching, old layout (context in system prompt) | 72–76% | 716–821 | 7.0–8.0× |
| **Caching, new layout** | **90.5–91.4%** | **282–303** | **2.8–3.0×** |

**Load test:** 75 users, 3 minutes, 6,093 answered messages, read from the database. Cache hit 94%, uncached ratio **2.3×** (28.2× counting all host tokens). Money checks passed.

These are estimates. Re-measure in staging with the real model and tokenizer: run `ops/loadtest.py` and read the dashboard, or run `ops/prompt_cache_sim.py` with your traffic assumptions.

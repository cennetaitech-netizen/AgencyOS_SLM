# Model host: prefix caching

Every model call resends the same opening: the system prompt and the tool schemas (about 1,300 tokens read-only, 1,650 with chat actions), then the conversation so far. Billable text is only the user's message and the visible answer, about 70–100 tokens. Without caching, the host computes the whole prompt every time, about 25× the billable tokens.

**Prefix caching** lets the host reuse the work for any opening it has seen recently. The chat service lays prompts out for it (`chat_service/engine/prompting.py`):

1. **System prompt:** identical for every org, user and day (`system_v3`, `system_v3_actions`, `system_ft_v2` have no placeholders).
2. **Tool schemas:** identical for every org with the same tools. Validation-only keywords are left out of what the model sees, but arguments are still validated against the full schema.
3. **Conversation history:** unchanged from the previous turn.
4. **Latest user message:** starts with a `[Context]` line (date, timezone, currency, role). It isn't billed and isn't stored in history.

`tests/test_prompt_cache.py` fails if anything personal or dated moves back into parts 1–3, or if the prefix grows past its budget.

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

## Tell finance the price
```bash
CHAT_HOST_PRICE_CACHED_INPUT_PER_1M_MINOR=<price per 1M cached prompt tokens>
```
Use the managed host's cached-input price. On your own GPUs, cached tokens skip the prefill compute, so use a small fraction of the input price. Unset means "same as normal input", which overstates cost but is safe. `GET /chat/admin/economics` then reports `cache_hit_pct`, `overhead_ratio` (all host tokens) and `uncached_overhead_ratio`, the one compared with `max_overhead_ratio`.

## Check it's working
- Dashboard → **Tokens and margin**: "Prompt cache hit rate" should sit well above 50%, and "Uncached cost ratio" well under 20×.
- Alert `AosPromptCacheHitLow` (below 50% for an hour) points to one of three causes: caching off, a prompt change that broke the shared prefix, or traffic not reaching warm replicas.

## What to expect
Two estimates, both from this environment with the stand-in host (`ops/fake_model_host.py` models vLLM's block cache):

**Simulation:** `ops/prompt_cache_sim.py`, a 9-hour day of 2,000 users in 300 orgs, about 14,900 messages, 60% calling a data tool, cache sizes from 100k to 1M tokens.

| Layout | Cache hit | Uncached host tokens per message | Uncached ÷ billable |
|---|---|---|---|
| No caching (where we were) | 0% | about 2,600 | about 26× |
| Caching, old layout (context in system prompt) | 72–76% | 716–821 | 7.0–8.0× |
| **Caching, new layout** | **90.5–91.4%** | **282–303** | **2.8–3.0×** |

**Load test:** 75 users, 3 minutes, 6,093 answered messages, read from the database. Cache hit 94%, uncached ratio **2.3×** (28.2× counting all host tokens). Money checks passed.

These are estimates. Re-measure in staging with the real model and tokenizer: run `ops/loadtest.py` and read the dashboard, or run `ops/prompt_cache_sim.py` with your traffic assumptions.

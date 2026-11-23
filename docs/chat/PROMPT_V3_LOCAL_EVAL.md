# Prompt v3: checks with real (small) models, 2026-11-23

**Why:** the v3 prompts (shorter, static, cache-friendly; see [ops/MODEL_HOST.md](../../ops/MODEL_HOST.md)) change what the model reads. Before the production model host exists, I checked whether a real model behaves worse with them.

**How:**
- The 32-case engine eval (`chat_service/evals/engine_eval.py`, `evals/chat_eval_cases.json`) ran through the real chat engine, tools, actions and billing.
- Models: Qwen2.5-1.5B-Instruct and Qwen2.5-3B-Instruct, served on CPU by `ops/local_model_server.py` with greedy decoding.
- "Old" is commit `8de273c`, with `system_v1`/`system_v2`, context in the system prompt, and the old tool descriptions.
- One run took 6–7 minutes (1.5B) or 12–18 minutes (3B) on 4 cores.

**Read these as comparisons, not pass marks.** Both models are far weaker than the 7–8B class planned for production, so every run fails the 85% gate. With 32 cases, one case is 3 points, and repeat runs differ by one or two cases. Nothing was executed without confirmation in any run.

## Results

**Qwen2.5-3B (closest to production):**

| Prompt | Overall | Actions | Math & copy | Data questions | Refusals | Grounded |
|---|---|---|---|---|---|---|
| Old `system_v1`/`v2` (2 runs) | 72% / 78% | 75% | 100% | 50% / 64% | 100% | 84–88% |
| v3, context as its own system message (3 runs, wording variants) | 69–72% | 75% | 80% | 50–57% | 100% | 81–84% |
| **v3 shipped: context on the user's message** | **75%** | 75% | 80% | **64%** | 100% | **88%** |

The first old-prompt run and the first v3 run lost cases to a test-server timeout (fixed: the local server now sends keep-alives). Those runs are not in this table.

**Qwen2.5-1.5B:**

| Prompt | Overall | Actions | Math & copy | Data questions | Refusals | Grounded |
|---|---|---|---|---|---|---|
| Old `system_v1`/`v2` (2 runs) | 56% / 53% | 75% / 62% | 40% | 64% | 20% | 81% / 78% |
| v3 first draft (context on the user's message, fewer instructions) | 47% | 62% | 80% | 43% | 0% | 75% |
| v3 with instructions restored, context as a system message (2 runs) | 62% / 62% | 75% | 80% | 43% | 80% | 81–84% |

## What changed and why
1. **First draft** (too short). Dropped instructions mattered: "use the tools to get data, don't ask the user for figures", "always use search_help_docs for how-to questions", the exact refusal sentence, and refusing requests to ignore or reveal the instructions. They're back.
2. **Context placement.**
   - On 1.5B the first draft sometimes answered or repeated the `[Context]` line. That was with fewer instructions, so it isn't a clean comparison.
   - On 3B, with identical prompt text, the context on the user's message scored 75% against 69% as a separate system message.
   - **Shipped default:** `CHAT_PROMPT_CONTEXT_PLACEMENT=user_prefix`. It also works with every chat template; some reject a second system message.
3. **Scope wording.** v3 uses `system_v1`'s scope lines word for word. The old prompt never over-refused on 3B.

**Shipped prefix size** (Qwen tokenizer): **1,355 tokens read-only** (old: 1,634, −17%) and **1,731 with chat actions** (old: 2,156, −20%). It's identical for every org, user and day, so the host's prefix cache can reuse it.

## Known issue to check on the production model
On 3B, v3 sometimes opens an in-scope answer with the off-topic sentence before answering: "write ad copy" (m04) and "How do I change a campaign budget?" (r13, which also skipped the help-docs search). The old prompt didn't do this on 3B. Neither wording changes nor the context placement fixed it, so it's probably the overall shape of the shorter prompt.

When the production model is evaluated, look at m04 and r13. If it shows the same pattern, add one line to the scope section naming ad copy and how-to questions as in scope. That line didn't help on 3B, but larger models follow such lines more reliably.

Other failures happen under both prompts and are model errors the checks catch correctly:
- wrong digits flagged by the grounding check (r03: the tool said 11,405 clicks, the model wrote 1,140)
- sums written without media_math

## Over-refusal experiment (candidate `system_v3c`)
On 3B, v3 opened two in-scope answers with the off-topic sentence: m04 (write ad copy) and r13 (how do I change a budget). I tested three variants on those plus the 5 off-topic questions and 3 ordinary ones (10 questions):

| Variant | 10-question score | m04 ad copy | r13 how-to | Off-topic refused |
|---|---|---|---|---|
| v3 (shipped) | 80% | refused | opened with the off-topic line | 5/5 |
| a: in-scope types listed first, refuse only "clearly unrelated" | 80% | refused | opened with the off-topic line | 5/5 |
| b: scope rule moved to the end | 90% | answered | opened with the off-topic line | 5/5 |
| **c: both (`system_v3c`)** | **90%** | **answered** | **answered normally** | **5/5** |

The wording alone didn't help; moving the rule to the end did. On all 32 questions, c scored **75% twice** (identical runs), the same as v3. It answered every in-scope request and still refused every off-topic one, but 2 more answers had a figure the grounding check couldn't match (r12, a05): grounded 78% vs 88%.

**Decision: v3 stays the default; `system_v3c` is kept as a candidate.** On 3B it swaps one over-refusal for one grounding miss, and grounding is a hard release gate. The eval workflow runs both prompts on the production model and reports c as "comparison only", so the first real run decides. Switching is configuration: `CHAT_PROMPT_VERSION=system_v3c` and `CHAT_PROMPT_VERSION_ACTIONS=system_v3c_actions`.

## Reproduce
```bash
.venv-ml/bin/python ops/local_model_server.py --model Qwen/Qwen2.5-3B-Instruct --port 8010
```
```bash
python -m chat_service.evals.engine_eval --models evals/models.local.json --database-url postgresql+asyncpg://chat:chat@localhost:5432/chat_eval --out eval-local.md
```
`evals/models.local.json` points at the server. Change its `model` if you serve a different one.

**The real gate:** set the `EVAL_MODEL_*` secrets so `eval.yml` runs the same 32 cases against the production candidate on every prompt, model or tool change. It must reach 85% overall, 95% on actions and 100% grounded.

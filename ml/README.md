# Media-Campaign SLM: Training and Rollout Runbook (Phase 11)

Everything here runs **outside** the chat service, in its own environment:

```bash
python -m venv .venv-ml && . .venv-ml/bin/activate
```

```bash
pip install -r ml/requirements.txt
```

**Verified:** the data builder, `train_sft.py` and `train_dpo.py` were smoke-tested end to end on CPU with a tiny Qwen-architecture model (3 SFT steps → LoRA adapter → 2 DPO steps). Real training needs a GPU.

---

## 0. Prerequisites
- **Base model** chosen at gate G2 (bake-off), with a licence that allows commercial use and serving fine-tuned versions. Put its Hugging Face ID in `ml/configs/*.json`.
- **Training consent:** only orgs whose admin opted in (`PUT /chat/settings/training-consent`) are exported.
- **Teacher model** for synthetic data. Its terms must allow using outputs to train other models.
- **GPU:** one 80 GB A100/H100, rented by the hour, is enough for LoRA on a 7–8B model.

## 1. Build a dataset snapshot
```bash
python -m chat_service.ml.build --database-url "$PROD_READ_REPLICA_URL" --out ml/data/v1 --corrections ml/corrections.jsonl --teacher evals/teacher.json --synthetic-database-url "$SANDBOX_DB_URL" --per-task 200
```
- Writes `train.jsonl`, `validation.jsonl`, `dpo.jsonl` and `report.json` (counts, drop reasons, prompt version).
- Rules applied:
  - personal data scrubbed
  - down-rated answers dropped unless an SME corrected them
  - answers flagged as ungrounded dropped
  - duplicates removed
  - **eval questions are never included**
  - the split is deterministic
- `ml/corrections.jsonl`: one `{"message_id": "...", "corrected_answer": "..."}` per line, written by the media SME from the weekly quality review (`GET /chat/admin/quality-review`).
- Prompt: records use `system_ft_v2`, the short prompt (at most half the length of `system_v3_actions`), laid out exactly as the engine sends it: a static system prompt, then history, then the latest user message starting with a `[Context]` line. The fine-tuned model learns the behaviour, so each message costs fewer hidden tokens, and the fixed prefix stays cacheable. `system_ft_v1` (context in the system prompt) still builds for comparison.
- **Version every snapshot** (`ml/data/v1`, `v2`, …) and keep it with the model it trained.

## 2. Train
Check the data rendering first (no GPU needed):
```bash
python ml/train_sft.py --data ml/data/v1 --out ml/runs/sft-v1 --dry-run
```
Supervised fine-tuning (LoRA):
```bash
python ml/train_sft.py --config ml/configs/sft_lora.json --data ml/data/v1 --out ml/runs/sft-v1
```
Preference tuning on SME corrections (DPO):
```bash
python ml/train_dpo.py --config ml/configs/dpo.json --pairs ml/data/v1/dpo.jsonl --sft-adapter ml/runs/sft-v1 --out ml/runs/dpo-v1
```
Each run writes `training_meta.json` (config, dataset, stats, loss). Fill in a model card from `ml/MODEL_CARD_TEMPLATE.md`.

## 3. Serve the adapter next to production (vLLM)
```bash
vllm serve <base-model> --enable-prefix-caching --enable-prompt-tokens-details \
  --enable-lora --lora-modules media-slm-v1=ml/runs/dpo-v1 --max-lora-rank 16
```
Prefix caching and cached-token reporting: see [ops/MODEL_HOST.md](../ops/MODEL_HOST.md). Serve the fine-tuned model with `CHAT_PROMPT_VERSION=system_ft_v2` (and the same for `CHAT_PROMPT_VERSION_ACTIONS`) so it gets the prompt it was trained on.
Then tell the chat service the new model name exists (same host, same base URL):
```bash
CHAT_EXTRA_MODEL_NAMES='["media-slm-v1"]'
```

## 4. Release gate (must pass before any client sees it)
```bash
python -m chat_service.ml.gate --production evals/prod.json --candidate evals/cand.json --database-url "$EVAL_DB_URL"
```
Pass = absolute targets met **and** equal or better than production on every category, 100% grounded, nothing executed without confirmation, overhead ratio ≤ production, p95 latency ≤ production × 1.1. Exit code 1 on failure.

## 5. Canary → promote (or roll back)
| Step | API (AgencyOS staff) |
|---|---|
| Start at 10% of orgs | `POST /chat/admin/model-rollout/canary {"model": "media-slm-v1", "percent": 10}` |
| Watch | `POST /chat/admin/model-rollout/evaluate`. The scheduled job also checks every 5 minutes and **rolls back automatically** if the canary's error rate or 👎 rate is worse than production beyond the configured margins |
| Widen | `POST /chat/admin/model-rollout/percent {"percent": 50}` |
| Promote | `POST /chat/admin/model-rollout/promote` |
| Roll back manually | `POST /chat/admin/model-rollout/rollback {"reason": "..."}` |
| Keep a client on the proven model | `PUT /chat/admin/orgs/{org_id}/settings {"model_pin": "<model>"}` |

Orgs are assigned by a stable hash, so each org sees one model consistently during a rollout.

## 6. Monthly cadence
Quality review → SME corrections → new snapshot → SFT + DPO → gate → canary → promote. Track each version's eval report, gate result, dataset report and model card together.

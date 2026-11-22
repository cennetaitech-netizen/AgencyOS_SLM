# Model card: `media-slm-vN`

| Field | Value |
|---|---|
| Base model and licence | `TODO` |
| Adapter type | LoRA (r=…, alpha=…), SFT → DPO |
| Training data snapshot | `ml/data/vN` (link `report.json`): consented examples …, synthetic …, corrections …, DPO pairs … |
| Teacher model (synthetic data) and terms check | `TODO` |
| Prompt version | `system_ft_v1` |
| Training runs | `ml/runs/sft-vN/training_meta.json`, `ml/runs/dpo-vN/training_meta.json` |
| Engine eval (frozen set) | overall …%, read …%, math/copy …%, actions …%, refusals …%, grounded …% |
| Release gate vs production | PASS / FAIL (link `GATE_RESULTS.md`) |
| Overhead ratio / p95 latency | … × / … ms (production: … × / … ms) |
| Canary | started …, widened …, promoted / rolled back … |
| Known limitations | e.g. weaker on …; languages other than English untested |
| Owner / approvals | ML lead …, media SME …, product owner … |

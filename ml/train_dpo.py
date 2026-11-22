"""Preference tuning with DPO (Phase 11, step 2): expert-corrected answers are preferred over the originals.

  python ml/train_dpo.py --config ml/configs/dpo.json --pairs ml/data/v1/dpo.jsonl \\
      --sft-adapter ml/runs/sft-v1 --out ml/runs/dpo-v1 [--dry-run]

Starts from the SFT adapter (merged into the base) and trains a new LoRA adapter on the preference pairs.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import build_dpo_examples, load_config, load_jsonl  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="ml/configs/dpo.json")
    ap.add_argument("--pairs", required=True)
    ap.add_argument("--sft-adapter", help="LoRA adapter from train_sft.py (merged before DPO)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--base-model")
    ap.add_argument("--max-steps", type=int)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cfg = load_config(args.config, {"base_model": args.base_model, "max_steps": args.max_steps})

    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(args.sft_adapter or cfg["base_model"])
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    pairs = build_dpo_examples(tokenizer, load_jsonl(args.pairs))
    print(json.dumps({"pairs": len(pairs)}))
    if not pairs:
        sys.exit("no preference pairs (DPO needs SME-corrected answers)")
    if args.dry_run:
        print(pairs[0]["prompt"][-300:], "\nCHOSEN:", pairs[0]["chosen"], "\nREJECTED:", pairs[0]["rejected"])
        return

    import torch
    from datasets import Dataset
    from peft import LoraConfig, PeftModel
    from transformers import AutoModelForCausalLM
    from trl import DPOConfig, DPOTrainer

    use_cuda = torch.cuda.is_available()
    model = AutoModelForCausalLM.from_pretrained(cfg["base_model"], dtype=torch.bfloat16 if use_cuda else torch.float32)
    if args.sft_adapter:
        model = PeftModel.from_pretrained(model, args.sft_adapter).merge_and_unload()
    dpo_args = DPOConfig(
        output_dir=args.out,
        num_train_epochs=cfg["epochs"],
        max_steps=cfg.get("max_steps") or -1,
        per_device_train_batch_size=cfg["batch_size"],
        gradient_accumulation_steps=cfg["grad_accum"],
        learning_rate=cfg["learning_rate"],
        beta=cfg["beta"],
        max_length=cfg["max_length"],
        logging_steps=cfg.get("logging_steps", 10),
        bf16=use_cuda,
        report_to=cfg.get("report_to", "none"),
        seed=cfg.get("seed", 42),
    )
    lora = LoraConfig(
        r=cfg["lora_r"],
        lora_alpha=cfg["lora_alpha"],
        lora_dropout=cfg["lora_dropout"],
        target_modules=cfg.get("target_modules", "all-linear"),
        task_type="CAUSAL_LM",
    )
    trainer = DPOTrainer(
        model=model, args=dpo_args, train_dataset=Dataset.from_list(pairs), processing_class=tokenizer, peft_config=lora
    )
    result = trainer.train()
    trainer.save_model(args.out)
    tokenizer.save_pretrained(args.out)
    meta = {"config": cfg, "pairs": len(pairs), "sft_adapter": args.sft_adapter, "train_loss": result.training_loss}
    (Path(args.out) / "training_meta.json").write_text(json.dumps(meta, indent=2))
    print(json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()

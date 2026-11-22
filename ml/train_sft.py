"""Supervised fine-tuning with LoRA (Phase 11, step 1).

  python ml/train_sft.py --config ml/configs/sft_lora.json --data ml/data/v1 --out ml/runs/sft-v1 [--dry-run]

--dry-run renders the dataset with the model's chat template and prints counts and one example (no training).
Real runs need a GPU (one 80 GB A100/H100 is enough for a 7–8B model with LoRA). The output is a LoRA adapter
that vLLM can serve next to the base model (see ml/README.md).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import build_sft_examples, load_config, load_jsonl, warmup_kwargs  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="ml/configs/sft_lora.json")
    ap.add_argument("--data", required=True, help="dir with train.jsonl / validation.jsonl")
    ap.add_argument("--out", required=True)
    ap.add_argument("--base-model")
    ap.add_argument("--max-steps", type=int)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cfg = load_config(args.config, {"base_model": args.base_model, "max_steps": args.max_steps})

    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(cfg["base_model"])
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    train, stats = build_sft_examples(tokenizer, load_jsonl(Path(args.data) / "train.jsonl"))
    val, _ = build_sft_examples(tokenizer, load_jsonl(Path(args.data) / "validation.jsonl"))
    print(json.dumps({"train": stats, "validation_examples": len(val)}, indent=2))
    if not train:
        sys.exit("no training examples")
    if args.dry_run:
        print(
            "--- example prompt (tail) ---\n",
            train[0]["prompt"][-400:],
            "\n--- completion ---\n",
            train[0]["completion"],
        )
        return

    import torch
    from datasets import Dataset
    from peft import LoraConfig
    from transformers import AutoModelForCausalLM
    from trl import SFTConfig, SFTTrainer

    use_cuda = torch.cuda.is_available()
    model = AutoModelForCausalLM.from_pretrained(cfg["base_model"], dtype=torch.bfloat16 if use_cuda else torch.float32)
    sft_args = SFTConfig(
        output_dir=args.out,
        num_train_epochs=cfg["epochs"],
        max_steps=cfg.get("max_steps") or -1,
        per_device_train_batch_size=cfg["batch_size"],
        gradient_accumulation_steps=cfg["grad_accum"],
        learning_rate=cfg["learning_rate"],
        lr_scheduler_type="cosine",
        **warmup_kwargs(SFTConfig, cfg.get("warmup_ratio", 0.03)),
        max_length=cfg["max_length"],
        completion_only_loss=True,
        logging_steps=cfg.get("logging_steps", 10),
        eval_strategy="epoch" if val else "no",
        save_strategy="epoch",
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
    trainer = SFTTrainer(
        model=model,
        args=sft_args,
        train_dataset=Dataset.from_list(train),
        eval_dataset=Dataset.from_list(val) if val else None,
        processing_class=tokenizer,
        peft_config=lora,
    )
    result = trainer.train()
    trainer.save_model(args.out)
    tokenizer.save_pretrained(args.out)
    meta = {"config": cfg, "dataset": str(args.data), "stats": stats, "train_loss": result.training_loss}
    (Path(args.out) / "training_meta.json").write_text(json.dumps(meta, indent=2))
    print(json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()

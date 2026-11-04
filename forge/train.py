"""Forge training job: QLoRA fine-tune with Unsloth, then export a GGUF for Ollama.

Usage: python train.py /forge/jobs/<id>/config.json
Writes progress to status.json next to the config so Wetworks can follow along.
"""

import glob
import json
import os
import sys
import time
import traceback

CFG_PATH = sys.argv[1]
JOB_DIR = os.path.dirname(CFG_PATH)
STATUS = os.path.join(JOB_DIR, "status.json")


def status(**kw):
    cur = {}
    if os.path.exists(STATUS):
        with open(STATUS) as f:
            cur = json.load(f)
    cur.update(kw, updated=time.time())
    tmp = STATUS + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cur, f)
    os.replace(tmp, STATUS)


def main():
    cfg = json.load(open(CFG_PATH))
    status(phase="loading", message=f"Loading {cfg['base_model']}")

    from unsloth import FastLanguageModel  # must be imported before transformers/trl

    import torch
    from datasets import load_dataset
    from transformers import TrainerCallback
    from trl import SFTConfig, SFTTrainer

    max_len = int(cfg.get("max_seq_length", 2048))
    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=cfg["base_model"], max_seq_length=max_len, dtype=None, load_in_4bit=True)
    model = FastLanguageModel.get_peft_model(
        model, r=int(cfg.get("lora_r", 16)), lora_alpha=int(cfg.get("lora_alpha", 16)), lora_dropout=0, bias="none",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
        use_gradient_checkpointing="unsloth", random_state=3407)

    ds = load_dataset("json", data_files=cfg["dataset"], split="train")
    ds = ds.map(lambda ex: {"text": tokenizer.apply_chat_template(ex["messages"], tokenize=False)},
                remove_columns=ds.column_names)

    class Progress(TrainerCallback):
        def on_log(self, args, state, control, logs=None, **kw):
            status(phase="training", step=state.global_step, total=state.max_steps,
                   loss=(logs or {}).get("loss"), message=f"Step {state.global_step}/{state.max_steps}")

    args = SFTConfig(
        output_dir=os.path.join(JOB_DIR, "checkpoints"), dataset_text_field="text", max_length=max_len,
        per_device_train_batch_size=int(cfg.get("batch_size", 2)),
        gradient_accumulation_steps=int(cfg.get("grad_accum", 4)),
        num_train_epochs=float(cfg.get("epochs", 2)), max_steps=int(cfg.get("max_steps", -1)),
        learning_rate=float(cfg.get("learning_rate", 2e-4)), warmup_steps=5, logging_steps=5,
        optim="adamw_8bit", weight_decay=0.01, lr_scheduler_type="linear", seed=3407,
        bf16=torch.cuda.is_bf16_supported(), fp16=not torch.cuda.is_bf16_supported(),
        save_strategy="no", report_to="none")
    try:  # newer TRL takes processing_class; older (and Unsloth-patched) takes tokenizer
        trainer = SFTTrainer(model=model, processing_class=tokenizer, train_dataset=ds, args=args, callbacks=[Progress()])
    except TypeError:
        trainer = SFTTrainer(model=model, tokenizer=tokenizer, train_dataset=ds, args=args, callbacks=[Progress()])
    status(phase="training", message=f"Training on {len(ds)} examples")
    result = trainer.train()

    status(phase="exporting", message="Exporting GGUF (q4_k_m)", loss=getattr(result, "training_loss", None))
    out = os.path.join(JOB_DIR, "gguf")
    model.save_pretrained_gguf(out, tokenizer, quantization_method=cfg.get("quantization", "q4_k_m"))
    ggufs = sorted(glob.glob(os.path.join(out, "**", "*.gguf"), recursive=True), key=os.path.getsize, reverse=True)
    if not ggufs:
        raise RuntimeError("Export finished but no .gguf file was written")
    status(phase="done", message="Training and export finished", gguf=ggufs[0],
           gguf_bytes=os.path.getsize(ggufs[0]), examples=len(ds))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        status(phase="failed", message=f"{type(e).__name__}: {e}", trace=traceback.format_exc()[-4000:])
        sys.exit(1)

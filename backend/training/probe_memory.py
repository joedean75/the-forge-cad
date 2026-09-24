"""Measure peak GPU memory for one training step, to extrapolate to bigger bases."""
import argparse, json, sys, time
from pathlib import Path
import torch
sys.path.insert(0, str(Path(__file__).resolve().parent))
from copilot_sft import apply_lora, loss_of, loss_of_hf, patch_attention, render

p = argparse.ArgumentParser()
p.add_argument("--weights"); p.add_argument("--package"); p.add_argument("--hf")
p.add_argument("--tokens", type=int, default=3072); p.add_argument("--batch", type=int, default=1)
a = p.parse_args()
dev = torch.device("cuda")
if a.hf:
    from transformers import AutoModelForCausalLM
    model = AutoModelForCausalLM.from_pretrained(a.hf, dtype=torch.bfloat16,
                                                attn_implementation="sdpa",
                                                low_cpu_mem_usage=True)
    model.config.use_cache = False
    if hasattr(model, "enable_input_require_grads"):
        model.enable_input_require_grads()
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    model.train()
else:
    sys.path.insert(0, str(Path(a.package).resolve()))
    from reasoning_from_scratch.qwen3 import QWEN_CONFIG_06_B, Qwen3Model
    patch_attention()
    cfg = dict(QWEN_CONFIG_06_B); cfg["dtype"] = torch.bfloat16
    model = Qwen3Model(cfg)
    model.load_state_dict(torch.load(a.weights, map_location="cpu", weights_only=True))
trainable = apply_lora(model, 16, 32)
model.to(dev)
torch.cuda.reset_peak_memory_stats()
after_load = torch.cuda.memory_allocated() / 2**30
opt = torch.optim.AdamW([q for q in model.parameters() if q.requires_grad], lr=1e-4)
row = {"prompt": list(range(a.tokens - 80)), "target": list(range(80))}
t = time.time()
for _ in range(a.batch):                      # accumulate, as a real batch would
    (loss_of_hf(model, row, dev) if a.hf else loss_of(model, row, dev)).backward()
opt.step(); opt.zero_grad(set_to_none=True)
torch.cuda.synchronize()
print(json.dumps({
    "params_b": round(sum(q.numel() for q in model.parameters()) / 1e9, 3),
    "trainable_m": round(trainable / 1e6, 1),
    "weights_gib": round(after_load, 2),
    "peak_gib": round(torch.cuda.max_memory_allocated() / 2**30, 2),
    "tokens": a.tokens, "accumulated": a.batch, "model": a.hf or "book-0.6B",
    "seconds_per_step": round(time.time() - t, 2)}, indent=1))

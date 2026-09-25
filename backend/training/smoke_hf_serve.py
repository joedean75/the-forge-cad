"""Does `serve.py --hf` actually load, generate and answer?

The HF path was written against a trained 2B that does not exist yet, so this
runs the whole of it — load, adapter, merge, tokenize, generate, stop, envelope —
on a model built from the real config on the CPU. Random weights cannot produce
valid JSON, and that is not what is being tested: the plumbing is.

The config shrink below does not take on this architecture, so what actually gets
built is the full 1.9B shape with random weights. That is left as it is: it is a
better test than a toy, because the thing being checked is that `LORA_TARGETS`
matches *these* module names and that the merge covers them — 114 modules here,
and a shrunken model with fewer layers would have quietly checked fewer.
"""
import json, sys, tempfile
from argparse import Namespace
from pathlib import Path

import torch
sys.stdout.reconfigure(errors='replace')
sys.path.insert(0, str(Path(__file__).parent))
import serve
from copilot_sft import apply_lora, lora_state
from transformers import AutoConfig, AutoModelForCausalLM, AutoTokenizer

BASE = "Qwen/Qwen3.5-2B"
tmp = Path(tempfile.mkdtemp())
cfg = AutoConfig.from_pretrained(BASE)
for attr, value in (("hidden_size", 128), ("num_hidden_layers", 2),
                    ("num_attention_heads", 4), ("num_key_value_heads", 2),
                    ("intermediate_size", 256), ("head_dim", 32)):
    if hasattr(cfg, attr):
        setattr(cfg, attr, value)
model = AutoModelForCausalLM.from_config(cfg)
model.save_pretrained(tmp)
AutoTokenizer.from_pretrained(BASE).save_pretrained(tmp)
print(f"tiny model: {sum(p.numel() for p in model.parameters()):,} parameters")

# An adapter written the way the trainer writes one, so the strict key check and
# the merge are exercised rather than skipped.
wrapped = apply_lora(model, 16, 32)
adapter = tmp / "lora.pt"
torch.save({"lora": lora_state(model), "step": 1}, adapter)
print(f"adapter: {wrapped:,} trainable parameters wrapped")
assert wrapped > 0, "LORA_TARGETS matched nothing on an HF model"

args = Namespace(hf=str(tmp), weights=None, package=None, lora=str(adapter),
                 lora_rank=16, lora_alpha=32, no_json_stop=False,
                 keep_adapters=False, device="cpu", model_name="smoke",
                 max_new_tokens=8)
serve.load(args)
assert serve._STATE["backend"] == "hf"

messages = [{"role": "system", "content": "rules"},
            {"role": "user", "content": "Goal: a 40 x 30 x 6 mm plate"}]
body = serve.completion({"messages": messages, "max_tokens": 8, "temperature": 0})
print("envelope keys:", sorted(body.keys()))
print("usage:", body["usage"])
print("finish_reason:", body["choices"][0]["finish_reason"])
print("content:", ascii(body["choices"][0]["message"]["content"])[:100])
assert body["usage"]["prompt_tokens"] > 10, "the prompt was not tokenized"
assert body["usage"]["completion_tokens"] > 0, "nothing was generated"
# A second request: the book backend needed reset_kv_cache here, and a stateful
# HF path would fail exactly the same way.
again = serve.completion({"messages": messages, "max_tokens": 8, "temperature": 0})
assert again["usage"]["completion_tokens"] > 0, "the second request produced nothing"
print("second request ok")
print("SMOKE OK")

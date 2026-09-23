"""Fine-tune Qwen3 0.6B on the copilot dataset, on one 6 GB card.

Standalone by design: it imports `torch` and the book's `reasoning_from_scratch`
package and **nothing from `app.services`**, because it runs under the Windows
CUDA interpreter where this backend is not installed. The dataset it reads is
built by `python -m datasets.build --profile finetuned`, and the prompt profile
is not optional — a model trained on the short prompt has to be *served* the
short prompt (`FORGE_COPILOT_PROMPT=finetuned`).

Four things make this fit where the book's own trainer cannot go past 2,048
tokens, and each is load-bearing rather than an optimisation:

* **Attention through `scaled_dot_product_attention`.** `qwen3.py` materialises
  the score matrix: 16 heads x n x n per layer, kept for backward across 28
  layers — 3.7 GB at 2,048 tokens, which is the whole card, and 15 GB at 4,096.
  The patched forward is the same mathematics with O(n) memory. Nothing in the
  book's package is modified; the function is replaced in this process only.
* **LoRA on the projections, base frozen in bf16.** AdamW over all 0.6B
  parameters is 4.8 GB of optimizer state before any activation — which is why
  the run this was measured beside spills to host memory and crawls. Frozen base
  plus rank-16 adapters is ~1.2 GB of weights and a few MB of state, and a
  checkpoint is the adapters, so it saves in a second rather than a minute.
* **The language-model head runs on the supervised positions only.** Logits for
  4,096 positions over a 151k vocabulary are 1.2 GB, and their gradient another
  1.2 GB. The label is the assistant's JSON — a few hundred tokens — so the head
  is applied to those positions and the rest never becomes a logit.
* **Gradient checkpointing per block**, which trades the recompute for the
  activations of 28 blocks.

And two operational rules learned from the run next door: **a checkpoint every
`--checkpoint-every` steps, not once per epoch** (that run had nothing on disk
after 32 hours), and **the first step measures itself** — the longest surviving
sample is tried first and the cap steps down on an out-of-memory error, so the
job reports `max_len_used` instead of dying at hour three.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import torch
import torch.nn as nn
import torch.nn.functional as F

#: Sequence-length buckets. The first step tries the longest sample that fits the
#: requested cap; on an OOM the cap drops to the next bucket down and the samples
#: above it are dropped and counted.
LENGTH_BUCKETS = (4096, 3072, 2048, 1536, 1024, 512)


# --------------------------------------------------------------- the prompt --

def render(messages: List[dict]) -> Tuple[str, str]:
    """A sample's messages as `(prompt, target)` text.

    The **one** place this rendering is defined: `serve.py` imports it, so what
    the server sends the model is what the trainer taught it. The book's chat
    template carries a `<think>` convention this corpus has no use for, and the
    copilot's own turn is already a system message and one user message.
    """
    system = next((m["content"] for m in messages if m["role"] == "system"), "")
    user = next((m["content"] for m in messages if m["role"] == "user"), "")
    if isinstance(user, list):
        user = "\n\n".join(p.get("text", "") for p in user if isinstance(p, dict))
    target = ""
    last = messages[-1]
    if last["role"] == "assistant":
        target = last.get("content") or ""
        if not target and last.get("tool_calls"):
            # A tool-call label in a `full`-profile sample. Rendered as the JSON
            # the prose protocol uses, so a mixed set still trains one format.
            call = last["tool_calls"][0]["function"]
            target = json.dumps({"op": call["name"],
                                 "args": json.loads(call["arguments"] or "{}")})
    prompt = (f"<|im_start|>system\n{system}<|im_end|>\n"
              f"<|im_start|>user\n{user}<|im_end|>\n"
              f"<|im_start|>assistant\n")
    return prompt, target + "<|im_end|>"


# ----------------------------------------------------------------- the model --

def patch_attention() -> None:
    """Replace the naive score matrix with `scaled_dot_product_attention`.

    Same causal attention, O(n) memory. Only the training path (no KV cache) is
    served here: generation keeps the book's own forward, because this patch is
    applied in the trainer's process and nowhere else.
    """
    from reasoning_from_scratch import qwen3 as q3

    def forward(self, x, mask, cos, sin, start_pos=0, cache=None):
        b, num_tokens, _ = x.shape
        queries = self.W_query(x).view(b, num_tokens, self.num_heads, self.head_dim).transpose(1, 2)
        keys = self.W_key(x).view(b, num_tokens, self.num_kv_groups, self.head_dim).transpose(1, 2)
        values = self.W_value(x).view(b, num_tokens, self.num_kv_groups, self.head_dim).transpose(1, 2)
        if self.q_norm:
            queries = self.q_norm(queries)
        if self.k_norm:
            keys = self.k_norm(keys)
        queries = q3.apply_rope(queries, cos, sin, offset=start_pos)
        keys = q3.apply_rope(keys, cos, sin, offset=start_pos)
        keys = keys.repeat_interleave(self.group_size, dim=1)
        values = values.repeat_interleave(self.group_size, dim=1)
        context = F.scaled_dot_product_attention(queries, keys, values, is_causal=True)
        context = context.transpose(1, 2).reshape(b, num_tokens, self.num_heads * self.head_dim)
        return self.out_proj(context), (keys, values)

    q3.GroupedQueryAttention.forward = forward


class LoRALinear(nn.Module):
    """A frozen linear layer plus a low-rank update.

    `peft` is not installed on the machine this runs on, and this is the whole of
    what is needed: the base weight never gets a gradient, so the optimizer
    carries state for the adapters alone.
    """

    def __init__(self, base: nn.Linear, rank: int = 16, alpha: int = 32):
        super().__init__()
        self.base = base
        for parameter in self.base.parameters():
            parameter.requires_grad_(False)
        self.a = nn.Parameter(torch.zeros(rank, base.in_features, dtype=torch.float32))
        self.b = nn.Parameter(torch.zeros(base.out_features, rank, dtype=torch.float32))
        nn.init.kaiming_uniform_(self.a, a=math.sqrt(5))
        self.scale = alpha / rank

    def forward(self, x):
        out = self.base(x)
        update = F.linear(F.linear(x.to(self.a.dtype), self.a), self.b)
        return out + self.scale * update.to(out.dtype)


#: Which projections get adapters. Attention and MLP both: adapting attention
#: alone teaches routing without teaching the argument values a CAD call needs.
LORA_TARGETS = ("W_query", "W_key", "W_value", "out_proj", "fc1", "fc2", "fc3")


def apply_lora(model: nn.Module, rank: int, alpha: int) -> int:
    """Wrap every target projection. Returns how many parameters will train."""
    for module in model.modules():
        for name in LORA_TARGETS:
            child = getattr(module, name, None)
            if isinstance(child, nn.Linear):
                setattr(module, name, LoRALinear(child, rank=rank, alpha=alpha))
    for name, parameter in model.named_parameters():
        parameter.requires_grad_(".a" in name.split(".")[-1] or ".b" in name.split(".")[-1]
                                 or name.endswith((".a", ".b")))
    return sum(p.numel() for p in model.parameters() if p.requires_grad)


def lora_state(model: nn.Module) -> Dict[str, torch.Tensor]:
    return {k: v.detach().cpu() for k, v in model.state_dict().items()
            if k.endswith((".a", ".b"))}


# ------------------------------------------------------------------ the data --

def load_samples(path: Path, tokenizer, max_len: int) -> Tuple[List[dict], int]:
    """Tokenised `(prompt, target)` pairs, longest first, with the over-long dropped."""
    rows: List[dict] = []
    dropped = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        prompt, target = render(json.loads(line)["messages"])
        prompt_ids = tokenizer.encode(prompt)
        target_ids = tokenizer.encode(target)
        if len(prompt_ids) + len(target_ids) > max_len:
            dropped += 1
            continue
        rows.append({"prompt": prompt_ids, "target": target_ids,
                     "length": len(prompt_ids) + len(target_ids)})
    rows.sort(key=lambda r: -r["length"])
    return rows, dropped


def loss_of(model, row: dict, device) -> torch.Tensor:
    """Cross-entropy over the target span only.

    The head is applied to the supervised positions alone — logits for a whole
    4k sequence over a 151k vocabulary would be 1.2 GB, and its gradient the
    same again, which is the single largest thing that would not fit.
    """
    ids = torch.tensor(row["prompt"] + row["target"], dtype=torch.long, device=device)[None]
    hidden = forward_hidden(model, ids)
    start = len(row["prompt"]) - 1              # predict the first target token
    end = ids.shape[1] - 1
    logits = model.out_head(hidden[:, start:end])
    labels = ids[:, start + 1:end + 1]
    return F.cross_entropy(logits.reshape(-1, logits.size(-1)).float(), labels.reshape(-1))


def forward_hidden(model, ids: torch.Tensor) -> torch.Tensor:
    """The transformer body, with each block gradient-checkpointed."""
    from torch.utils.checkpoint import checkpoint

    x = model.tok_emb(ids)
    num_tokens = ids.shape[1]
    cos, sin = model.cos[:num_tokens], model.sin[:num_tokens]
    for block in model.trf_blocks:
        if torch.is_grad_enabled():
            x = checkpoint(lambda t, b=block: b(t, None, cos, sin)[0]
                           if isinstance(b(t, None, cos, sin), tuple) else b(t, None, cos, sin),
                           x, use_reentrant=False)
        else:
            out = block(x, None, cos, sin)
            x = out[0] if isinstance(out, tuple) else out
    return model.final_norm(x)


# ------------------------------------------------------------------ the loop --

def train(args) -> int:
    sys.path.insert(0, str(Path(args.package).resolve()))
    from reasoning_from_scratch.qwen3 import (QWEN_CONFIG_06_B, Qwen3Model,
                                             Qwen3Tokenizer)

    device = torch.device(args.device if args.device != "auto" else
                          ("cuda" if torch.cuda.is_available() else "cpu"))
    print(f"device: {device}", flush=True)
    patch_attention()

    tokenizer = Qwen3Tokenizer(tokenizer_file_path=str(Path(args.weights).parent /
                                                       "tokenizer-base.json"))
    config = dict(QWEN_CONFIG_06_B)
    config["dtype"] = torch.bfloat16 if device.type == "cuda" else torch.float32
    model = Qwen3Model(config)
    state = torch.load(args.weights, map_location="cpu", weights_only=True)
    model.load_state_dict(state)
    trainable = apply_lora(model, args.lora_rank, args.lora_alpha)
    model.to(device)
    print(f"trainable parameters: {trainable:,} (LoRA rank {args.lora_rank})", flush=True)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = out_dir / "metrics.csv"
    new_file = not metrics_path.exists()
    metrics = metrics_path.open("a", newline="")
    writer = csv.writer(metrics)
    if new_file:
        writer.writerow(["step", "epoch", "train_loss", "val_loss", "max_len", "seconds"])

    optimizer = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad],
                                  lr=args.lr, weight_decay=0.0)
    step = 0
    if args.resume and (out_dir / "latest.pt").exists():
        saved = torch.load(out_dir / "latest.pt", map_location="cpu", weights_only=True)
        model.load_state_dict(saved["lora"], strict=False)
        optimizer.load_state_dict(saved["optimizer"])
        step = int(saved["step"])
        print(f"resumed from step {step}", flush=True)

    max_len = args.max_len
    while True:
        rows, dropped = load_samples(Path(args.train), tokenizer, max_len)
        if not rows:
            print("no samples fit; lower --max-len is not the problem", file=sys.stderr)
            return 2
        # **The first step measures itself**: the longest surviving sample, so an
        # out-of-memory failure happens in the first seconds rather than at hour
        # three, and the cap steps down instead of the run dying.
        try:
            probe = loss_of(model, rows[0], device)
            probe.backward()
            optimizer.zero_grad(set_to_none=True)
            print(f"max_len {max_len}: longest sample {rows[0]['length']} tokens fits "
                  f"({len(rows):,} samples, {dropped} dropped)", flush=True)
            break
        except torch.cuda.OutOfMemoryError:
            optimizer.zero_grad(set_to_none=True)
            torch.cuda.empty_cache()
            lower = [b for b in LENGTH_BUCKETS if b < max_len]
            if not lower:
                print("out of memory at the smallest bucket", file=sys.stderr)
                return 2
            max_len = lower[0]
            print(f"out of memory; dropping the cap to {max_len}", flush=True)

    # Validation comes from the dataset's own held-out file when there is one:
    # `datasets.build` splits by *source*, so no run or document straddles the
    # split. Taking it from `rows` here would take the longest samples (they are
    # sorted for the memory probe) and measure length rather than learning.
    if args.val and Path(args.val).exists():
        validation, _ = load_samples(Path(args.val), tokenizer, max_len)
        validation = validation[:args.val_size]
        training = rows
    else:
        validation = rows[:args.val_size]
        training = rows[args.val_size:]
    random.Random(args.seed).shuffle(training)
    print(f"train {len(training):,} | val {len(validation):,} | epochs {args.epochs}",
          flush=True)

    started = time.time()
    for epoch in range(1, args.epochs + 1):
        for row in training:
            if args.max_steps and step >= args.max_steps:
                break
            model.train()
            loss = loss_of(model, row, device)
            loss.backward()
            torch.nn.utils.clip_grad_norm_([p for p in model.parameters()
                                            if p.requires_grad], args.grad_clip)
            optimizer.step()
            optimizer.zero_grad(set_to_none=True)
            step += 1

            if step % args.log_every == 0:
                model.eval()
                with torch.no_grad():
                    val = (sum(loss_of(model, r, device).item() for r in validation)
                           / max(len(validation), 1)) if validation else float("nan")
                writer.writerow([step, epoch, round(loss.item(), 6), round(val, 6),
                                 max_len, round(time.time() - started, 1)])
                metrics.flush()
                print(f"[epoch {epoch} step {step}] train {loss.item():.4f} val {val:.4f}",
                      flush=True)
            if step % args.checkpoint_every == 0:
                torch.save({"lora": lora_state(model), "optimizer": optimizer.state_dict(),
                            "step": step, "epoch": epoch, "max_len": max_len,
                            "config": vars(args)}, out_dir / "latest.pt")
                torch.save({"lora": lora_state(model), "step": step},
                           out_dir / f"lora-step{step:06d}.pt")
        if args.max_steps and step >= args.max_steps:
            break

    torch.save({"lora": lora_state(model), "step": step}, out_dir / "lora-final.pt")
    summary = {"steps": step, "max_len_used": max_len, "dropped_for_length": dropped,
               "train_samples": len(training), "val_samples": len(validation),
               "trainable_parameters": trainable, "seconds": round(time.time() - started, 1)}
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2), flush=True)
    metrics.close()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--train", required=True, help="train.jsonl from datasets.build")
    parser.add_argument("--val", default=None, help="val.jsonl from datasets.build")
    parser.add_argument("--out", required=True, help="directory for checkpoints and metrics")
    parser.add_argument("--weights", required=True, help="qwen3-0.6B-base.pth")
    parser.add_argument("--package", required=True,
                        help="directory containing the reasoning_from_scratch package")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--max-len", type=int, default=4096, dest="max_len")
    parser.add_argument("--epochs", type=int, default=2)
    parser.add_argument("--lr", type=float, default=1e-4)
    parser.add_argument("--grad-clip", type=float, default=1.0, dest="grad_clip")
    parser.add_argument("--lora-rank", type=int, default=16, dest="lora_rank")
    parser.add_argument("--lora-alpha", type=int, default=32, dest="lora_alpha")
    parser.add_argument("--val-size", type=int, default=32, dest="val_size")
    parser.add_argument("--log-every", type=int, default=25, dest="log_every")
    parser.add_argument("--checkpoint-every", type=int, default=100, dest="checkpoint_every")
    parser.add_argument("--max-steps", type=int, default=0, dest="max_steps",
                        help="stop after this many steps (0 = the full run)")
    parser.add_argument("--seed", type=int, default=123)
    parser.add_argument("--resume", action="store_true")
    return train(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())

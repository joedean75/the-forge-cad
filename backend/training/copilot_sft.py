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
#:
#: The rungs are close together near the top because that is where the corpus
#: lives and the card runs out. Measured with the tokenizer on the copilot set,
#: 3,072 keeps 67% of the samples and 2,048 keeps 29%; on the previous corpus,
#: whose system prompt was 406 tokens longer, the same two rungs were 48% and
#: **2%**. So the original ladder's one step down from 3,072 was a fall, and a
#: run that took it would have trained on a twentieth of the set, checkpointed,
#: and reported a clean finish. `MIN_SURVIVING_FRACTION` is what makes that a
#: refusal instead, and 2,816 (58%) is a rung rather than a cliff.
LENGTH_BUCKETS = (4096, 3584, 3072, 2816, 2560, 2048, 1536, 1024, 512)

#: How much of the corpus a step-down may cost before the run refuses to start.
MIN_SURVIVING_FRACTION = 0.75


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
#: Two naming conventions, because the same list has to find them in the book's
#: Qwen3 and in a transformers model — with the book's names only, `--hf` wrapped
#: nothing, froze everything, and the first backward had no graph to walk.
LORA_TARGETS = ("W_query", "W_key", "W_value", "out_proj", "fc1", "fc2", "fc3",
                "q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj")


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


def merge_lora(model: nn.Module) -> int:
    """Fold every adapter into the weight it adapts, and unwrap it.

    For serving. `LoRALinear` costs two extra matmuls per layer per token and
    runs them in fp32 to keep training stable — 5 tokens/second on a 3050, which
    is past the planner's 60 s timeout for any real answer. `W + scale * (B @ A)`
    is the same function of the input, computed once, after which generation is
    plain bf16 through the original layers. Returns how many were merged.
    """
    merged = 0
    for module in model.modules():
        for name, child in list(module.named_children()):
            if isinstance(child, LoRALinear):
                delta = (child.b @ child.a) * child.scale
                base = child.base
                with torch.no_grad():
                    base.weight.add_(delta.to(base.weight.dtype))
                setattr(module, name, base)
                merged += 1
    return merged


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


def loss_of_hf(model, row: dict, device) -> torch.Tensor:
    """The same masked cross-entropy through a transformers model.

    The head runs on the supervised positions only, here as in the book path,
    because it decides whether a 2B fits at all: logits for 3,000 positions over
    a 151k vocabulary are 0.9 GiB and their gradient another 0.9 GiB, against
    ~1.5 GiB of headroom on a 6 GB card. `model.model` (the body) and
    `model.lm_head` are the standard shape of a causal LM in transformers; a
    model that does not have them falls back to the whole-sequence loss, which is
    correct and merely heavier.
    """
    ids = torch.tensor(row["prompt"] + row["target"], dtype=torch.long, device=device)[None]
    body, head = getattr(model, "model", None), getattr(model, "lm_head", None)
    if body is None or head is None:
        labels = ids.clone()
        labels[:, :len(row["prompt"])] = -100      # supervise the answer only
        return model(input_ids=ids, labels=labels).loss

    hidden = body(input_ids=ids).last_hidden_state
    start, end = len(row["prompt"]) - 1, ids.shape[1] - 1
    logits = head(hidden[:, start:end])
    labels = ids[:, start + 1:end + 1]
    return F.cross_entropy(logits.reshape(-1, logits.size(-1)).float(), labels.reshape(-1))


def step_loss(model, row: dict, device, args):
    """Whichever loss this backend uses."""
    return loss_of_hf(model, row, device) if args.hf else loss_of(model, row, device)


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

def _load_book_model(args, device):
    """Qwen3 0.6B from the book's own `.pth` and tokenizer."""
    sys.path.insert(0, str(Path(args.package).resolve()))
    from reasoning_from_scratch.qwen3 import (QWEN_CONFIG_06_B, Qwen3Model,
                                             Qwen3Tokenizer)

    patch_attention()
    tokenizer = Qwen3Tokenizer(tokenizer_file_path=str(Path(args.weights).parent /
                                                       "tokenizer-base.json"))
    config = dict(QWEN_CONFIG_06_B)
    config["dtype"] = torch.bfloat16 if device.type == "cuda" else torch.float32
    model = Qwen3Model(config)
    model.load_state_dict(torch.load(args.weights, map_location="cpu", weights_only=True))
    return model, tokenizer


def _load_hf_model(args, device):
    """Any Hugging Face causal LM — how a base larger than 0.6B is reached.

    The book's package ships 0.6B and nothing else, so `--hf` is the door to
    Qwen3.5-2B (measured elsewhere at ~5 GB for bf16 LoRA, which fits this card)
    and to 4B/9B on a bigger one. `LoRALinear` wraps `nn.Linear` whatever built
    it, so the adapters, the merge and the checkpoints are unchanged; what
    differs is that attention comes from transformers (already memory-efficient,
    so no patch) and the tokenizer from the model's own files.
    """
    from transformers import AutoModelForCausalLM, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(args.hf)
    model = AutoModelForCausalLM.from_pretrained(
        args.hf, dtype=torch.bfloat16 if device.type == "cuda" else torch.float32,
        attn_implementation="sdpa", low_cpu_mem_usage=True)
    model.config.use_cache = False
    # **Checkpointing needs the model training and the inputs requiring grad.**
    # Measured without this, Qwen3.5-2B peaked at 15.5 GiB for a 1,536-token
    # sample — every activation kept — which on a 6 GiB card meant Windows
    # spilling to host memory at 62 s/step against 4.8 s for the 0.6B. With a
    # frozen base under LoRA, the embedding output is the first tensor that has
    # to carry grad, or every block's recompute is skipped as pointless.
    if hasattr(model, "enable_input_require_grads"):
        model.enable_input_require_grads()
    if hasattr(model, "gradient_checkpointing_enable"):
        model.gradient_checkpointing_enable(
            gradient_checkpointing_kwargs={"use_reentrant": False})
    model.train()
    return model, tokenizer


def train(args) -> int:
    device = torch.device(args.device if args.device != "auto" else
                          ("cuda" if torch.cuda.is_available() else "cpu"))
    print(f"device: {device}", flush=True)

    model, tokenizer = (_load_hf_model(args, device) if args.hf
                        else _load_book_model(args, device))
    trainable = apply_lora(model, args.lora_rank, args.lora_alpha)
    if not trainable:
        print("no LoRA adapters were attached: this model's linear layers are named "
              f"none of {LORA_TARGETS}", file=sys.stderr)
        return 2
    model.to(device)
    print(f"trainable parameters: {trainable:,} (LoRA rank {args.lora_rank})", flush=True)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    # `summary.json` means "a run finished here", and `serve_when_trained.ps1`
    # reads it as exactly that. Left in place from a previous run it is a lie the
    # moment this one starts: a crash would leave the watcher serving the *old*
    # adapter and scoring it as the new result — measuring the wrong model and
    # reporting it confidently, which is worse than measuring nothing.
    (out_dir / "summary.json").unlink(missing_ok=True)
    metrics_path = out_dir / "metrics.csv"
    new_file = not metrics_path.exists()
    metrics = metrics_path.open("a", newline="")
    writer = csv.writer(metrics)
    if new_file:
        writer.writerow(["step", "epoch", "train_loss", "val_loss", "max_len", "seconds"])

    optimizer = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad],
                                  lr=args.lr, weight_decay=0.0)

    updates_done = 0
    step = 0
    if args.resume and (out_dir / "latest.pt").exists():
        saved = torch.load(out_dir / "latest.pt", map_location="cpu", weights_only=True)
        model.load_state_dict(saved["lora"], strict=False)
        optimizer.load_state_dict(saved["optimizer"])
        step = int(saved["step"])
        print(f"resumed from step {step}", flush=True)

    max_len = args.max_len
    requested_fit = None
    while True:
        rows, dropped = load_samples(Path(args.train), tokenizer, max_len)
        if not rows:
            print("no samples fit; lower --max-len is not the problem", file=sys.stderr)
            return 2
        if requested_fit is None:
            requested_fit = len(rows)
        elif len(rows) < MIN_SURVIVING_FRACTION * requested_fit:
            # A cap is a budget; a corpus is the thing being learned. Stepping
            # down far enough to delete most of it produces a run that finishes,
            # checkpoints and scores — against a set that no longer contains the
            # behaviour it was assembled to teach. The longest samples are the ones
            # with the most steps behind them, so what goes first is exactly the
            # recovery and finishing turns. Refuse and name the numbers.
            print(f"refusing: at --max-len {args.max_len} the set is "
                  f"{requested_fit:,} samples, and this card only reaches "
                  f"{max_len}, where it is {len(rows):,} "
                  f"({100 * len(rows) // requested_fit}%). Shorten the prompt "
                  f"profile or train a smaller base rather than a gutted set.",
                  file=sys.stderr)
            return 2
        # **The first step measures itself**: the longest surviving sample, so an
        # out-of-memory failure happens in the first seconds rather than at hour
        # three, and the cap steps down instead of the run dying.
        try:
            probe = step_loss(model, rows[0], device, args)
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
    pending = 0
    # **A resumed run continues where it stopped, it does not start again.**
    # `step` counts samples, so a run cut short by `--max-steps` and then resumed
    # with a higher one iterated `training` from row 0 again: it would re-train
    # the 2,800 samples it had already seen and never reach the 1,355 it had not,
    # while the step counter climbed and the log looked like progress. The order
    # is `seed`-shuffled and therefore the same on both runs, so an offset is all
    # it takes.
    # **Why a schedule is an option at all.** The first 2B run was measured twice:
    # at 2,800 of 4,155 samples it scored 0.900 on the goal suite, and finishing
    # the epoch took it to 0.867 with the finishing turns halved and every task
    # taking two steps more — while validation loss *fell* 0.025 -> 0.0094 and
    # train loss reached 3.8e-05. What degrades at a constant rate once the loss
    # is that low is knowing when to stop. Decaying the rate is the standard
    # answer and it is untested here, so it is a flag rather than the default:
    # one variable, against a baseline that exists.
    planned = args.max_steps or (len(training) * args.epochs)
    updates = max(1, planned // max(1, args.accumulate))

    def lr_at(update: int) -> float:
        if args.lr_schedule == "constant":
            return args.lr
        warm = max(1, int(0.03 * updates))
        if update < warm:
            return args.lr * (update + 1) / warm
        progress = min(1.0, (update - warm) / max(1, updates - warm))
        # To a tenth rather than to zero: the last samples should still teach.
        return args.lr * (0.1 + 0.9 * 0.5 * (1.0 + math.cos(math.pi * progress)))

    resume_at = step % len(training) if step else 0
    if resume_at:
        print(f"resuming {resume_at} samples into the shuffled order", flush=True)
    for epoch in range(1, args.epochs + 1):
        for row in training[resume_at:]:
            if args.max_steps and step >= args.max_steps:
                break
            model.train()
            # **Accumulated, not batched.** Measured on a 6 GB card at 3,072
            # tokens: one sample peaks at 2.01 GiB and four accumulated at 2.05,
            # because the graph is freed after each backward — a real batch of
            # four would multiply the activations instead. So the effective batch
            # costs nothing here, and a step per sample (what this did first) is
            # a needlessly noisy gradient.
            loss = step_loss(model, row, device, args) / args.accumulate
            loss.backward()
            pending += 1
            if pending >= args.accumulate:
                torch.nn.utils.clip_grad_norm_([p for p in model.parameters()
                                                if p.requires_grad], args.grad_clip)
                rate = lr_at(updates_done)
                for group in optimizer.param_groups:
                    group["lr"] = rate
                optimizer.step()
                updates_done += 1
                optimizer.zero_grad(set_to_none=True)
                pending = 0
            step += 1

            if step % args.log_every == 0:
                model.eval()
                with torch.no_grad():
                    val = (sum(step_loss(model, r, device, args).item() for r in validation)
                           / max(len(validation), 1)) if validation else float("nan")
                writer.writerow([step, epoch, round(loss.item() * args.accumulate, 6), round(val, 6),
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
        resume_at = 0            # only the epoch that was interrupted is offset
        if args.max_steps and step >= args.max_steps:
            break

    torch.save({"lora": lora_state(model), "step": step}, out_dir / "lora-final.pt")
    summary = {"steps": step, "max_len_used": max_len,
               "lr_schedule": args.lr_schedule, "optimizer_updates": updates_done, "dropped_for_length": dropped,
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
    parser.add_argument("--weights", help="qwen3-0.6B-base.pth (the book's backend)")
    parser.add_argument("--package",
                        help="directory containing the reasoning_from_scratch package")
    parser.add_argument("--hf", help="a Hugging Face model id or path to train instead "
                                     "of the book's 0.6B, e.g. Qwen/Qwen3.5-2B")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--max-len", type=int, default=4096, dest="max_len")
    # One, measured: on the first real run the validation loss went 0.463 ->
    # 0.103 by step 1,625 and then moved ±0.003 for the remaining 2,300 steps, so
    # the second epoch cost three hours and bought nothing.
    parser.add_argument("--epochs", type=int, default=1)
    parser.add_argument("--lr", type=float, default=1e-4)
    parser.add_argument("--lr-schedule", default="constant",
                        choices=("constant", "cosine"), dest="lr_schedule",
                        help="cosine warms up 3%% of updates then decays to a "
                             "tenth of --lr; constant is what the measured runs used")
    parser.add_argument("--accumulate", type=int, default=8,
                        help="samples per optimizer step. Free in memory (the graph is "
                             "freed after each backward), so this is how to get an "
                             "effective batch on a small card")
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
    args = parser.parse_args(argv)
    if not args.hf and not (args.weights and args.package):
        parser.error("give --hf <model>, or --weights and --package for the book's 0.6B")
    return train(args)


if __name__ == "__main__":
    raise SystemExit(main())

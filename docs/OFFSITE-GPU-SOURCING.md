# Renting a GPU for the copilot fine-tune

What it costs to train a base this workstation cannot hold, and where the real
cost sits. Written after measuring the local card rather than before, because
every published comparison is in dollars per hour and the question is dollars
per **epoch of this corpus**.

## The workload, stated once

| | |
|---|---|
| corpus | 6,177 turn samples, mean 2,742 tokens (tokenizer-counted, `finetuned` prompt) |
| one epoch, uncapped | **~17M tokens** — what a 24 GB card would train |
| one epoch, capped at 3,072 | 4,155 samples, mean 2,231 → ~9.3M tokens — what 6 GB reaches |
| method | rank-16 LoRA on the attention and MLP projections, base frozen, bf16 |
| label | the assistant's JSON, a few hundred tokens — the head runs on those positions only |

That is a small job, and the uncapped figure is the one to price: the whole
reason to rent is that a 24 GB card does not need the sequence cap a 6 GB card
imposes, and the cap is not a setting but a third of the corpus — measured, 4,155
of 6,177 samples fit 3,072 tokens, and the ones that do not are the turns with
the most steps behind them.

## The local baseline, measured

An RTX 3050 6GB **Laptop** GPU (5.94 GiB free), `training/probe_memory.py`:

| base | weights | peak @3,072 tok | s/sample | tok/s |
|---|---|---|---|---|
| Qwen3-0.6B | 1.48 GiB | 2.01 GiB | 4.8 | ~640 |
| Qwen3.5-2B | 3.55 GiB | 5.19 GiB | 13.2 | ~233 |
| Qwen3.5-4B | ~7.5 GiB | — | — | weights alone exceed the card |
| Qwen3.5-9B | ~17 GiB | — | — | no |

So the 2B trains here in about six hours an epoch and the 4B does not train
here at all. Two routes exist for the 4B: 4-bit, which needs `bitsandbytes`
(absent) and which Unsloth advises against for Qwen3.5 specifically, or renting
a card with more memory. Spilling to host RAM is not a third route — measured,
it is 62 s/step against 4.8, a factor of 13.

## What an epoch costs offsite

**These rows are an extrapolation, not a measurement.** A 4090 has roughly 8×
the usable bf16 throughput of this mobile 3050 and four times the memory, which
also buys turning gradient checkpointing *off* (~1.3× again); a 4B costs about
twice the 2B per token and a 9B about 2.3× the 4B. Verify against the first
hundred steps before believing a total.

| base | card | ~epoch (17M tokens) | at $0.31–0.55/hr | at $0.69–0.89/hr (secure) |
|---|---|---|---|---|
| 4B | 4090 24 GB | 4–6 h | **$1.25–3.30** | $2.75–5.35 |
| 9B | 4090 24 GB (tight; keep checkpointing on) | 9–14 h | **$2.80–7.70** | $6.20–12.50 |

An A100 80 GB at $1.07–1.59/hr is roughly twice a 4090 and three times the
price, so for a LoRA of this size it buys wall-clock, not money. It earns its
keep only for a *full* fine-tune, or a 9B with room to raise the sequence cap.
Lambda's $2.06–2.99/hr is an SLA this job does not need: a run that dies is
re-run for a dollar.

## Where the cost hides

- **Storage, if you let it.** RunPod network volumes are $0.07/GB-month under
  1 TB and the volume disk keeps billing while the pod is *stopped*; Vast bills
  allocated disk on a paused instance too. This corpus is 30 MB and an adapter
  is a few hundred — so keep nothing: download the base fresh, push the adapter
  out, destroy the pod. A 50 GB volume kept "for next time" costs more per month
  than the training run it was meant to save.
- **Interruption is already handled, but only halfway.** `--checkpoint-every`
  writes every N samples, which is what makes spot capacity usable at this size;
  it writes to the pod's own disk, so a reclaim still loses the run unless that
  path is a network volume or the checkpoint is copied out. Spot is worth it
  (30–50% off) for a job that checkpoints and is measured in hours, not days.
- **Egress**, where a provider charges it, is nothing here — the adapter is the
  only thing coming back.

## The decision

Train the 2B locally, because it fits and costs nothing but a night. Rent only
to answer the question the local card cannot: whether 4B or 9B clears the bar
the 2B does not. At $1–3 an epoch for a 4B that is comparable to a single
Opus assembly run, which is the comparison that matters — the fine-tune exists
to stop paying that per build. It also buys the thing the local card cannot
give at any runtime: the full corpus, uncapped.

## Sources

Prices read September 2026; they move, and the cheap tiers move fastest.

- [Cloud GPU Rental Prices: Compare 84 Providers (2026) — getdeploying](https://getdeploying.com/gpus)
- [Cloud GPU Pricing: Compare 31 Providers Live (Sep 2026) — gpuperhour](https://gpuperhour.com/)
- [GPU Cloud Pricing Comparison 2026 — Spheron](https://www.spheron.network/blog/gpu-cloud-pricing-comparison-2026/)
- [Cloud GPU Rental Guide 2026: RunPod vs Lambda vs Vast.ai — PromptQuorum](https://www.promptquorum.com/power-local-llm/cloud-gpu-rental-guide-2026)
- [GPU Rental Prices: RunPod vs Vast.ai, updated daily — madebyagents](https://www.madebyagents.com/hardware/gpu-rental-prices)
- [RunPod pricing 2026: GPU Pods, Serverless, and storage — Hivenet](https://www.hivenet.com/post/runpod-pricing-complete-guide-to-gpu-cloud-costs)
- [RunPod storage pricing explained — theneuralbase](https://theneuralbase.com/runpod/qna/runpod-storage-pricing/)
- [Runpod vs Vast.ai: which is cheaper in 2026 — Spheron](https://www.spheron.network/blog/runpod-vs-vastai-2026/)

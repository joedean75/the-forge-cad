# Training and serving the copilot model

Three pieces, in the order they run. All of them are **standalone**: they import
`torch` and the book's `reasoning_from_scratch` and nothing from `app.services`,
because they run under the Windows CUDA interpreter where this backend is not
installed. The source of truth is here; `stage.sh` copies them out.

| | |
|---|---|
| `copilot_sft.py` | LoRA fine-tune of Qwen3 0.6B on the copilot dataset |
| `serve.py` | the trained model behind an OpenAI-compatible endpoint |
| `start_when_free.ps1` | a Windows scheduled task that starts training when the GPU is free |

## 1. Build the dataset in the profile you will serve

```bash
python -m datasets.build --out data/ft --profile finetuned \
    --sources recorded,template,catalog,library --variations 3
```

`--profile finetuned` drops the tool schemas and the operation directory from
the prompt: 11.7k tokens median becomes 2.8k, which is what makes a 6 GB card
possible at all. **The profile is a contract.** A model trained on the short
prompt has to be served the short prompt — `FORGE_COPILOT_PROMPT=finetuned` —
and the label is the prose JSON `action_space._DONE_PROTOCOL` describes, so the
backend serves it with `FORGE_LLM_TOOLS=0`.

## 2. Train

```bash
python copilot_sft.py --train dataset/train.jsonl --val dataset/val.jsonl \
    --out runs/sft --weights .../qwen3/qwen3-0.6B-base.pth \
    --package .../reasoning-from-scratch --max-len 4096 --epochs 2
```

Start from **`qwen3-0.6B-base.pth`**, not the reasoning checkpoint: its
`<think>` convention would wrap every label. What makes 4k tokens fit where the
book's own trainer stops at 2,048 is in the module docstring — attention through
`scaled_dot_product_attention`, a frozen bf16 base under rank-16 LoRA, the
language-model head on the supervised positions only, and per-block gradient
checkpointing. It checkpoints every `--checkpoint-every` steps and `--resume`s,
because a trainer that saves once per epoch can lose a day.

## 3. Serve, and point the backend at it

```bash
python serve.py --weights .../qwen3-0.6B-base.pth --lora runs/sft/lora-final.pt \
    --package .../reasoning-from-scratch --port 8099
```

```bash
LLM_BASE_URL=http://127.0.0.1:8099/v1
LLM_MODEL=forge-copilot-qwen3-0.6b
LLM_API_KEY=local            # anything; the server does not check it
FORGE_LLM_TOOLS=0            # this model answers in prose JSON
FORGE_COPILOT_PROMPT=finetuned
```

Then `evals/agent.py` scores it against the same reward function every other run
is scored by, and `GET /api/llm/config` lists it like any other model.

## What fits on 6 GB, measured

`probe_memory.py` runs one forward/backward and reports the peak, which is the
only honest way to answer "will this base train here". On an RTX 3050 6 GB
(5.94 GiB free), rank-16 LoRA, gradient checkpointing, head on the supervised
positions:

| base | weights | peak @3,072 tok | s/sample | verdict |
|---|---|---|---|---|
| Qwen3-0.6B (book `.pth`) | 1.48 GiB | 2.01 GiB | 4.8 | comfortable |
| Qwen3.5-2B (`--hf`) | 3.55 GiB | 5.19 GiB | 13.2 | fits at a 3,072 cap |
| Qwen3.5-2B @4,096 | 3.55 GiB | 6.23 GiB | 23.9 | spills to host RAM |
| Qwen3.5-4B | ~7.5 GiB | — | — | weights alone exceed the card |
| Qwen3.5-9B | ~17 GiB | — | — | no |

Three things that decide those numbers, each of which was wrong first:

* **Accumulate, never batch.** Four samples accumulated peak at 2.05 GiB against
  2.01 for one, because the graph is freed after each backward — a real batch of
  four multiplies the activations. `--accumulate` is therefore how to get an
  effective batch here, and it is free.
* **Checkpointing needs `model.train()` and `enable_input_require_grads()`.**
  Without them the 2B kept every activation: 15.5 GiB for a *1,536*-token sample,
  which the driver served by spilling to system memory at 62 s/step against
  4.8 s for the 0.6B. That 13x is what "use a CPU/GPU combination" actually buys.
* **4-bit is the only route to 4B on this card** (weights ~2.4 GiB), and it needs
  `bitsandbytes`, which is not installed — and Unsloth advises against QLoRA for
  Qwen3.5 specifically, so a 4-bit run wants a base like Qwen3-4B or Gemma-3-4B.
  For 4B or 9B in bf16, an hour on a rented 24 GB GPU costs a dollar or two and
  the dataset is only ~7 M tokens an epoch.

## Two flags that decide the wall clock

`step` counts **samples**, not optimizer updates, and two defaults are read
against it:

* `--log-every 25` with `--val-size 32` runs 32 forward passes every 25 samples.
  On the 0.6B that is cheap; on a 2B at ~2,250 tokens it is **40% of the run's
  wall clock**, which turns a six-hour epoch into eight and a half. `--log-every
  100 --val-size 16` is ~5% and still gives a couple of dozen loss points.
* `--accumulate` divides the update count, not the sample count. At 8 over 2,200
  samples that is 275 updates an epoch, where the 0.6B run that converged took
  ~1,600. Accumulation is free in memory, so the number to pick is about gradient
  noise against update count, not about fitting.

Serving follows the same split: `--hf` on `serve.py` loads what `--hf` on the
trainer trained, merges the adapter and stops on `<|im_end|>` through a
`StoppingCriteria` wrapping the same `JsonStop`. A base that trains and cannot
be served is a base nobody can evaluate.

## What is measured, and what is not

* The wrapper works: consecutive completions return the OpenAI shape with usage,
  and `tests/test_training_wrapper.py` pins the prompt rendering and the
  answer-shape translation.
* **A real turn on CPU took 564 seconds** (a 2.8k-token prefill), which is past
  the planner's own timeout — the copilot reports a planner error rather than
  hanging. This is a GPU workload; on CPU only the plumbing is testable.
* Nothing here says the trained model is any good. 2,004 samples of a 0.6B base,
  skewed towards `add_primitive`, is a working pipeline and not a usable copilot.
  `evals/` is where that question gets an answer.

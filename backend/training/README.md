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

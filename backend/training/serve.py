"""Serve the fine-tuned copilot model behind an OpenAI-compatible endpoint.

The copilot talks to one thing: a chat-completions URL. A LoRA `.pth` is not
that, so nothing could evaluate a trained model against the real API — this is
the missing piece. Point the backend at it:

    python serve.py --weights qwen3-0.6B-base.pth --lora runs/sft/lora-final.pt \
        --package .../reasoning-from-scratch --port 8099

    LLM_BASE_URL=http://localhost:8099/v1
    LLM_MODEL=forge-copilot-qwen3-0.6b
    FORGE_LLM_TOOLS=0              # this model answers in prose JSON
    FORGE_COPILOT_PROMPT=finetuned # the prompt it was trained on

Standalone like the trainer, and for the same reason — it runs under the Windows
CUDA interpreter, so it imports `torch` and `reasoning_from_scratch` and nothing
from `app.services`. The HTTP layer is `http.server` rather than FastAPI because
that venv has no FastAPI and this needs two routes.

Four things are load-bearing:

* **The prompt is rendered by `copilot_sft.render`**, the function that built the
  training targets. A server with its own copy of that formatting is a model
  served a prompt it never saw, which is the whole failure the single-generator
  rule exists to prevent.
* **Training's attention patch is deliberately not applied.** It passes
  `is_causal=True`, which is correct when query and key lengths match and wrong
  for a cached decode step, where one query attends to every cached key. The
  book's own forward handles the cache, and inference has no backward pass to
  economise for.
* **A tool-call request still gets a tool-call answer.** The finetuned profile
  sends no schemas and expects prose JSON, but if a caller offers `tools`, the
  parsed `{"op", "args"}` is returned as a `tool_calls` message, so the same
  server works for either path rather than silently failing the strong one.
* **Usage is reported**, so the copilot's cost ledger counts tokens. No `cost`
  is sent — a local run has none — which leaves `cost_known` false rather than
  claiming a spend of zero.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import torch

try:                                     # in this repository
    from training.copilot_sft import LoRALinear, apply_lora, merge_lora, render
except ImportError:                      # staged beside the trainer, no package
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from copilot_sft import LoRALinear, apply_lora, merge_lora, render  # noqa: E402

#: One model on one GPU: requests are serialised rather than interleaved, which
#: would multiply the KV cache by the number of callers and out-of-memory a 6 GB
#: card. The copilot makes one call per turn, so this costs nothing real.
_LOCK = threading.Lock()
_STATE: Dict[str, Any] = {}


def load(args) -> None:
    if args.hf:
        return _load_hf(args)
    sys.path.insert(0, str(Path(args.package).resolve()))
    from reasoning_from_scratch.qwen3 import (QWEN_CONFIG_06_B, Qwen3Model,
                                             Qwen3Tokenizer)

    device = torch.device(args.device if args.device != "auto" else
                          ("cuda" if torch.cuda.is_available() else "cpu"))
    config = dict(QWEN_CONFIG_06_B)
    config["dtype"] = torch.bfloat16 if device.type == "cuda" else torch.float32
    model = Qwen3Model(config)
    model.load_state_dict(torch.load(args.weights, map_location="cpu", weights_only=True))
    if args.lora:
        # The adapters are wrapped first so their keys exist, then filled. A
        # missing key here means the checkpoint came from different targets or a
        # different rank, and loading it loosely would serve the base model while
        # reporting success — so this is strict about what it does not know.
        apply_lora(model, args.lora_rank, args.lora_alpha)
        saved = torch.load(args.lora, map_location="cpu", weights_only=True)
        adapters = saved.get("lora", saved)
        missing, unexpected = model.load_state_dict(adapters, strict=False)
        if unexpected:
            raise SystemExit(f"adapter has keys this model does not: {unexpected[:3]}")
        if not any(k.endswith((".a", ".b")) for k in adapters):
            raise SystemExit("adapter file holds no LoRA tensors")
        print(f"loaded {len(adapters)} adapter tensors from {args.lora}", flush=True)
        if not args.keep_adapters:
            print(f"merged {merge_lora(model)} adapters into the base weights", flush=True)
    model.to(device).eval()
    for parameter in model.parameters():
        parameter.requires_grad_(False)

    tokenizer = Qwen3Tokenizer(tokenizer_file_path=str(Path(args.weights).parent /
                                                       "tokenizer-base.json"))
    _STATE.update({"model": model, "tokenizer": tokenizer, "device": device,
                   "name": args.model_name, "max_new": args.max_new_tokens,
                   "end_id": tokenizer.encode("<|im_end|>")[0],
                   "json_stop": not args.no_json_stop,
                   "backend": "book",
                   "context": config["context_length"]})
    print(f"serving {args.model_name} on {device}", flush=True)


def _load_hf(args) -> None:
    """Serve a Hugging Face causal LM — the same adapter contract, other weights.

    The trainer grew `--hf` because the book's package ships 0.6B and nothing
    else, and a base that can be trained and not served is a base nobody can
    evaluate. `LoRALinear` wraps `nn.Linear` whatever built it and `LORA_TARGETS`
    carries both naming schemes, so the adapters, the strictness about unexpected
    keys and the merge are the ones above rather than a second implementation.
    """
    from transformers import AutoModelForCausalLM, AutoTokenizer

    device = torch.device(args.device if args.device != "auto" else
                          ("cuda" if torch.cuda.is_available() else "cpu"))
    dtype = torch.bfloat16 if device.type == "cuda" else torch.float32
    tokenizer = AutoTokenizer.from_pretrained(args.hf)
    model = AutoModelForCausalLM.from_pretrained(args.hf, dtype=dtype)
    if args.lora:
        apply_lora(model, args.lora_rank, args.lora_alpha)
        saved = torch.load(args.lora, map_location="cpu", weights_only=True)
        adapters = saved.get("lora", saved)
        _, unexpected = model.load_state_dict(adapters, strict=False)
        if unexpected:
            raise SystemExit(f"adapter has keys this model does not: {unexpected[:3]}")
        if not any(k.endswith((".a", ".b")) for k in adapters):
            raise SystemExit("adapter file holds no LoRA tensors")
        print(f"loaded {len(adapters)} adapter tensors from {args.lora}", flush=True)
        if not args.keep_adapters:
            print(f"merged {merge_lora(model)} adapters into the base weights", flush=True)
    model.to(device).eval()
    for parameter in model.parameters():
        parameter.requires_grad_(False)

    # `<|im_end|>` is what `render` writes and what the labels end with, so it is
    # the stop token whatever the tokenizer nominates as its own eos.
    end_id = tokenizer.convert_tokens_to_ids("<|im_end|>")
    if end_id is None or end_id == tokenizer.unk_token_id:
        raise SystemExit("this tokenizer has no <|im_end|>; the trained format "
                         "would have no stop token")
    _STATE.update({"model": model, "tokenizer": tokenizer, "device": device,
                   "name": args.model_name, "max_new": args.max_new_tokens,
                   "end_id": end_id, "json_stop": not args.no_json_stop,
                   "backend": "hf",
                   "context": getattr(model.config, "max_position_embeddings", 32768)})
    print(f"serving {args.model_name} ({args.hf}) on {device}", flush=True)


#: What a caller is told once the CUDA context has died. It names the restart
#: because that is the only fix: the process cannot rebuild a poisoned context.
_POISONED = ("the CUDA context is unrecoverable after an out-of-memory fault; "
             "restart this server process")


def _is_cuda_fault(exc: BaseException) -> bool:
    """Whether a `RuntimeError` is a driver-level CUDA failure.

    Matched on the message because the driver reports these as a plain
    `RuntimeError`, with no distinct type to catch.
    """
    text = str(exc).lower()
    return "cuda error" in text or "cublas" in text or "device-side assert" in text


class _JsonStopCriteria:
    """`JsonStop` as a transformers stopping criterion.

    The book loop can ask the watcher about each token as it decodes; HF's
    `generate` owns its loop, so the watcher is fed the *delta* of the decoded
    continuation on each call. Decoding the whole tail and feeding it again
    would re-count braces and stop at the first one.
    """

    def __init__(self, tokenizer, prompt_len: int) -> None:
        self.tokenizer, self.prompt_len = tokenizer, prompt_len
        self.watcher, self.fed, self.hit = JsonStop(), 0, False

    def __call__(self, input_ids, scores, **kwargs) -> bool:
        text = self.tokenizer.decode(input_ids[0][self.prompt_len:],
                                     skip_special_tokens=True)
        delta, self.fed = text[self.fed:], len(text)
        if delta and self.watcher.feed(delta):
            self.hit = True
        return self.hit


def _generate_hf(prompt: str, max_new: int, temperature: float,
                 top_p: float, json_stop: bool) -> Tuple[str, int, int, str]:
    from transformers import StoppingCriteriaList

    model, tokenizer = _STATE["model"], _STATE["tokenizer"]
    # `add_special_tokens=False`: the prompt `render` produced already carries
    # its own `<|im_start|>` markers, and a tokenizer that prepends its own puts
    # the model one token off the format it was trained on.
    ids = tokenizer(prompt, return_tensors="pt", add_special_tokens=False)
    if ids["input_ids"].shape[1] >= _STATE["context"]:
        raise ValueError(f"prompt is {ids['input_ids'].shape[1]} tokens; the "
                         f"model's context is {_STATE['context']}")
    ids = {k: v.to(_STATE["device"]) for k, v in ids.items()}
    prompt_len = ids["input_ids"].shape[1]
    criteria = _JsonStopCriteria(tokenizer, prompt_len) if json_stop else None
    with torch.no_grad():
        out = model.generate(
            **ids, max_new_tokens=max_new, eos_token_id=_STATE["end_id"],
            pad_token_id=tokenizer.pad_token_id or _STATE["end_id"],
            do_sample=bool(temperature and temperature > 0),
            **({"temperature": temperature, "top_p": top_p}
               if temperature and temperature > 0 else {}),
            stopping_criteria=(StoppingCriteriaList([criteria]) if criteria else None))
    produced = out[0][prompt_len:]
    text = tokenizer.decode(produced, skip_special_tokens=True)
    stop = "length"
    if criteria is not None and criteria.hit:
        stop = "stop"
    elif len(produced) and int(produced[-1]) == _STATE["end_id"]:
        stop = "stop"
    return text, prompt_len, len(produced), stop


def generate(prompt: str, max_new: int, temperature: float,
             top_p: float, json_stop: bool = True) -> Tuple[str, int, int, str]:
    """Greedy or sampled continuation. Returns `(text, prompt_tokens, new_tokens, stop)`."""
    if _STATE.get("backend") == "hf":
        return _generate_hf(prompt, max_new, temperature, top_p, json_stop)
    from reasoning_from_scratch.qwen3 import KVCache

    model, tokenizer = _STATE["model"], _STATE["tokenizer"]
    device, end_id = _STATE["device"], _STATE["end_id"]
    ids = tokenizer.encode(prompt)
    if len(ids) >= _STATE["context"]:
        raise ValueError(f"prompt is {len(ids)} tokens; the model's context is "
                         f"{_STATE['context']}")
    tokens = torch.tensor([ids], dtype=torch.long, device=device)

    # **The model carries `current_pos` across calls.** It is reset per request:
    # without this the second completion starts where the first stopped and the
    # attention mask is built for the wrong span ("size of tensor a (105) must
    # match tensor b (23)"), so the first request of a process succeeds and every
    # one after it fails.
    model.reset_kv_cache()
    cache = KVCache(n_layers=len(model.trf_blocks))
    produced: List[int] = []
    stop = "length"
    watcher = JsonStop() if json_stop else None
    with torch.no_grad():
        logits = model(tokens, cache=cache)[:, -1]
        for _ in range(max_new):
            if temperature and temperature > 0:
                scaled = logits.float() / max(temperature, 1e-5)
                probabilities = torch.softmax(scaled, dim=-1)
                if top_p and 0 < top_p < 1:
                    ordered, order = torch.sort(probabilities, descending=True, dim=-1)
                    keep = torch.cumsum(ordered, dim=-1) - ordered <= top_p
                    ordered = ordered * keep
                    ordered = ordered / ordered.sum(dim=-1, keepdim=True)
                    choice = torch.multinomial(ordered, num_samples=1)
                    nxt = order.gather(-1, choice)
                else:
                    nxt = torch.multinomial(probabilities, num_samples=1)
            else:
                nxt = logits.argmax(dim=-1, keepdim=True)
            token = int(nxt.item())
            if token == end_id:
                stop = "stop"
                break
            produced.append(token)
            if watcher is not None and watcher.feed(tokenizer.decode([token])):
                stop = "stop"
                break
            logits = model(nxt, cache=cache)[:, -1]
    return tokenizer.decode(produced), len(ids), len(produced), stop


class JsonStop:
    """Tracks whether one complete JSON object has been produced.

    The protocol's answer is exactly one object, so a closing brace at depth zero
    is an *exact* stop condition — not a heuristic. It is needed because the
    fine-tuned model emits a well-formed action and then keeps going instead of
    its stop token: the token is a single one out of ~80 in each label, so two
    epochs left it under-learned while the averaged loss looked healthy, and the
    client correctly rejects a truncated reply. Braces inside strings are
    ignored, or an argument containing "{" would stop generation early.
    """

    def __init__(self) -> None:
        self.depth = 0
        self.opened = False
        self.in_string = False
        self.escaped = False

    def feed(self, text: str) -> bool:
        for character in text:
            if self.in_string:
                if self.escaped:
                    self.escaped = False
                elif character == "\\":
                    self.escaped = True
                elif character == '"':
                    self.in_string = False
                continue
            if character == '"':
                self.in_string = True
            elif character == "{":
                self.depth += 1
                self.opened = True
            elif character == "}":
                self.depth -= 1
                if self.opened and self.depth <= 0:
                    return True
        return False


def _first_json(text: str) -> Optional[dict]:
    """The first JSON object in a reply, for a caller that offered tools.

    The finetuned profile's answer *is* a JSON object, so this is usually the
    whole reply; a model that wandered before it is still readable.
    """
    depth = 0
    start = -1
    for index, character in enumerate(text):
        if character == "{":
            if depth == 0:
                start = index
            depth += 1
        elif character == "}" and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    body = json.loads(text[start:index + 1])
                except json.JSONDecodeError:
                    start = -1
                    continue
                return body if isinstance(body, dict) else None
    return None


def message_for(text: str, wants_tools: bool) -> Dict[str, Any]:
    """The assistant message, in the form this caller can read.

    A finetuned-profile turn answers in prose JSON, which is what the model was
    trained to emit. A caller that offered `tools` cannot parse that, so the
    parsed object is returned as a tool call instead — one model serving both
    paths rather than silently failing the strong one. A reply with no JSON in it
    is passed through as content: the copilot treats text as an answer, and
    inventing a tool call from prose would be worse than saying what came back.
    """
    body = _first_json(text) if wants_tools else None
    if body and body.get("op"):
        return {"role": "assistant", "content": body.get("rationale") or "",
                "tool_calls": [{"id": f"call_{uuid.uuid4().hex[:8]}", "type": "function",
                                "function": {"name": body["op"],
                                             "arguments": json.dumps(body.get("args") or {})}}]}
    if body and body.get("done"):
        return {"role": "assistant", "content": body.get("summary") or ""}
    return {"role": "assistant", "content": text}


def completion(payload: dict) -> dict:
    """One chat completion, in the shape `llm_client._post_chat` reads."""
    messages = payload.get("messages") or []
    prompt, _ = render([*messages, {"role": "assistant", "content": ""}])
    # The caller's ceiling, bounded by this server's own. The copilot asks for
    # 2,400 tokens because a hosted provider answers that in seconds; at ~5
    # tokens/s here that is eight minutes and every call would pass the
    # planner's 60 s timeout. An action is a short JSON object, so the
    # configured cap is the real one.
    max_new = min(int(payload.get("max_tokens") or _STATE["max_new"]), _STATE["max_new"])
    started = time.time()
    with _LOCK:
        text, prompt_tokens, new_tokens, stop = generate(
            prompt, max_new, float(payload.get("temperature") or 0.0),
            float(payload.get("top_p") or 0.0), json_stop=_STATE["json_stop"])

    message = message_for(text, bool(payload.get("tools")))
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(started),
        "model": payload.get("model") or _STATE["name"],
        "choices": [{"index": 0, "message": message,
                     "finish_reason": stop if "tool_calls" not in message else "tool_calls"}],
        # No `cost`: a local model has none, and reporting zero would tell the
        # copilot's ledger it knows the spend when it does not.
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": new_tokens,
                  "total_tokens": prompt_tokens + new_tokens},
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802 — http.server's interface
        if self.path.rstrip("/").endswith("/models"):
            self._send(200, {"object": "list", "data": [
                {"id": _STATE["name"], "object": "model", "owned_by": "local"}]})
        elif self.path.rstrip("/").endswith("/health"):
            self._send(200, {"ok": True, "model": _STATE["name"]})
        else:
            self._send(404, {"error": {"message": f"no route {self.path}"}})

    def do_POST(self) -> None:  # noqa: N802
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._send(404, {"error": {"message": f"no route {self.path}"}})
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            self._send(400, {"error": {"message": f"body is not JSON: {exc}"}})
            return
        # A context that has already died stays dead, so say so at once rather
        # than spending a generation's wall clock to fail the same way.
        if _STATE.get("poisoned"):
            self._send(503, {"error": {"message": _POISONED, "type": "server_error"}})
            return
        try:
            self._send(200, completion(payload))
        except ValueError as exc:            # a prompt longer than the context
            self._send(400, {"error": {"message": str(exc), "type": "invalid_request_error"}})
        except torch.cuda.OutOfMemoryError:
            # PyTorch's own allocator refused: the context is still good, so the
            # next request with a shorter prompt can succeed.
            torch.cuda.empty_cache()
            self._send(503, {"error": {"message": "out of GPU memory; retry with a "
                                                  "shorter prompt", "type": "server_error"}})
        except RuntimeError as exc:
            # `RuntimeError: CUDA error: out of memory` is *not*
            # `torch.cuda.OutOfMemoryError`. The first comes from the allocator and
            # is recoverable; this one comes from a failed kernel launch inside the
            # driver, is reported asynchronously, and leaves the CUDA context
            # unusable — every later call fails the same way no matter how short
            # the prompt is. It used to fall to the generic handler below, so the
            # server answered 500 forever while `/v1/models` kept returning 200,
            # and a caller could only read that as the model being unable to
            # answer. There is no in-process recovery, so it is latched and named.
            if not _is_cuda_fault(exc):
                raise
            _STATE["poisoned"] = True
            try:
                torch.cuda.empty_cache()
            except Exception:                # noqa: BLE001 — already broken
                pass
            self._send(503, {"error": {"message": f"{_POISONED} ({exc.__class__.__name__}: "
                                                  f"{str(exc).splitlines()[0]})",
                                       "type": "server_error"}})
        except Exception as exc:             # noqa: BLE001 — a server must answer
            self._send(500, {"error": {"message": f"{type(exc).__name__}: {exc}"}})

    def log_message(self, fmt, *args) -> None:
        print(f"{self.address_string()} {fmt % args}", flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--weights", help="qwen3-0.6B-base.pth (the book backend)")
    parser.add_argument("--hf", help="a Hugging Face model id or path to serve "
                                     "instead of the book's .pth")
    parser.add_argument("--package", required=False,
                        help="directory containing the reasoning_from_scratch package")
    parser.add_argument("--lora", default=None, help="LoRA checkpoint from copilot_sft")
    parser.add_argument("--lora-rank", type=int, default=16, dest="lora_rank")
    parser.add_argument("--lora-alpha", type=int, default=32, dest="lora_alpha")
    parser.add_argument("--no-json-stop", action="store_true", dest="no_json_stop",
                        help="do not stop at the end of the first JSON object; for "
                             "measuring whether the model emits its own stop token")
    parser.add_argument("--keep-adapters", action="store_true", dest="keep_adapters",
                        help="serve with the adapters as separate layers instead of "
                             "merging them; slower, and only useful for checking that "
                             "the merge changed nothing")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--max-new-tokens", type=int, default=512, dest="max_new_tokens")
    parser.add_argument("--model-name", default="forge-copilot-qwen3-0.6b",
                        dest="model_name")
    args = parser.parse_args(argv)
    # argparse cannot say "one of these two": a `--weights` left required would
    # refuse an HF run, and both left optional would reach `load` with nothing to
    # load and fail somewhere less clear.
    if not args.hf and not (args.weights and args.package):
        parser.error("give --hf MODEL, or both --weights and --package")
    if args.hf and args.weights:
        parser.error("--hf and --weights name two different models to serve")

    load(args)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"listening on http://{args.host}:{args.port}/v1 "
          f"(LLM_BASE_URL=http://{args.host}:{args.port}/v1)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopping", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

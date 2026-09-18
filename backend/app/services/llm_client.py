"""Optional LLM client for interpreting natural-language CAD prompts.

The client is provider-agnostic and speaks the OpenAI-style chat-completion
protocol used by Kimi/Moonshot and OpenRouter. Configuration is opt-in via
environment variables; text-to-CAD, text-to-assembly and the Copilot loop all
require a key and raise `TextToCadUnavailable` / decline to run without one —
there is no rule-based fallback.

Supported environment variables (in order of precedence):
  LLM_API_KEY / KIMI_API_KEY
  LLM_BASE_URL / KIMI_BASE_URL   (default: https://api.moonshot.cn/v1)
  LLM_MODEL / KIMI_MODEL         (default: kimi-k2-turbo-preview)

For OpenRouter, set LLM_BASE_URL=https://openrouter.ai/api/v1 and
LLM_MODEL to a routed model id, e.g. anthropic/claude-sonnet-5.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from contextvars import ContextVar
from typing import Any, Optional

import httpx
from app.services.llm_errors import LLMFailure

CALL_CONTEXT = ContextVar("forge_llm_call_context", default=None)

_LOGGER = logging.getLogger(__name__)

LLM_API_KEY = os.getenv("LLM_API_KEY") or os.getenv("KIMI_API_KEY")
LLM_BASE_URL = os.getenv("LLM_BASE_URL") or os.getenv("KIMI_BASE_URL", "https://api.moonshot.cn/v1")
LLM_MODEL = os.getenv("LLM_MODEL") or os.getenv("KIMI_MODEL", "kimi-k2-turbo-preview")

# Curated model lists by provider — current flagships first, then fast/cheap
# tiers, then legacy fallbacks. OpenRouter routes many more; the active model is
# always injected at the top of the list if it isn't already present.
# Curated frontier allowlist for OpenRouter, in display order. Every id here is a
# real OpenRouter slug. Two kinds of slug are left out on purpose: `:batch`
# variants, which are priced for asynchronous batch work rather than a copilot
# waiting on each step, and `~...-latest` aliases, which move to a different model
# without notice and so make two runs of one goal incomparable; `available_models()` intersects this with OpenRouter's live
# catalog so the picker never offers a slug the router has retired.
_PROVIDER_MODELS: dict[str, list[str]] = {
    "openrouter": [
        "anthropic/claude-fable-5.1",
        "anthropic/claude-opus-5",
        "anthropic/claude-sonnet-5",
        "anthropic/claude-opus-4.8",
        "anthropic/claude-haiku-4.5",
        "openai/gpt-6-astra",
        "openai/gpt-6-astra-pro",
        "openai/gpt-5.6-sol",
        "openai/gpt-5.6-sol-pro",
        "openai/gpt-5.6-terra",
        "openai/gpt-5.6-terra-pro",
        "openai/gpt-5.6-luna",
        "openai/gpt-5.6-luna-pro",
        "openai/gpt-5.1",
        "openai/gpt-5-mini",
        "google/gemini-3.1-pro-preview",
        "google/gemini-3.8-flash",
        "x-ai/grok-4.6",
        "moonshotai/kimi-k3",
        "qwen/qwen3.8-max-0902",
        "mistralai/mistral-large-2512",
        "deepseek/deepseek-v4-pro-0813",
        "deepseek/deepseek-v4-flash-0731",
        "meta-llama/llama-4-maverick",
    ],
    "moonshot": [
        "kimi-k2-thinking",
        "kimi-k2-turbo-preview",
        "kimi-k2-0905-preview",
        "kimi-latest",
        "moonshot-v1-32k",
    ],
    "openai": [
        "gpt-5.1",
        "gpt-5",
        "gpt-5-mini",
        "gpt-4.1",
        "gpt-4o",
    ],
    "anthropic": [
        "claude-sonnet-5",
        "claude-opus-4-8",
        "claude-haiku-4-5-20251001",
    ],
}


def _provider_name(base_url: str) -> str:
    if "openrouter" in base_url:
        return "openrouter"
    if "moonshot" in base_url:
        return "moonshot"
    if "anthropic" in base_url:
        return "anthropic"
    if "openai" in base_url:
        return "openai"
    return "custom"


def set_model(model: str) -> None:
    """Set the active chat-completion model at runtime."""
    global LLM_MODEL
    LLM_MODEL = model


def set_base_url(base_url: str) -> None:
    """Set the active API base URL at runtime."""
    global LLM_BASE_URL, _OPENROUTER_CATALOG, _OPENROUTER_FETCHED_AT
    LLM_BASE_URL = base_url
    # Provider may have changed; drop the cached catalog so it re-fetches.
    _OPENROUTER_CATALOG = None
    _OPENROUTER_FETCHED_AT = 0.0


# Cache OpenRouter's live model catalog so `available_models()` can filter the
# curated allowlist without a network round-trip on every config read. Each id
# maps to its input modalities, which is what says whether a model can be sent
# an image.
_OPENROUTER_CATALOG: Optional[dict[str, frozenset[str]]] = None
_OPENROUTER_FETCHED_AT: float = 0.0
_OPENROUTER_TTL = 3600.0  # seconds


def _openrouter_catalog() -> Optional[dict[str, frozenset[str]]]:
    """Best-effort fetch of OpenRouter's public catalog: id -> input modalities.

    Cached for ``_OPENROUTER_TTL`` seconds. Returns ``None`` on any failure so
    callers fall back to the static curated list. The ``/models`` endpoint is
    public and needs no API key.
    """
    global _OPENROUTER_CATALOG, _OPENROUTER_FETCHED_AT
    now = time.monotonic()
    if _OPENROUTER_CATALOG is not None and now - _OPENROUTER_FETCHED_AT < _OPENROUTER_TTL:
        return _OPENROUTER_CATALOG
    url = f"{LLM_BASE_URL.rstrip('/')}/models"
    try:
        with httpx.Client(timeout=4.0) as client:
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.json()
        ids = {
            m["id"]: frozenset(((m.get("architecture") or {}).get("input_modalities")) or ())
            for m in data.get("data", []) if isinstance(m, dict) and m.get("id")
        }
        if ids:
            _OPENROUTER_CATALOG = ids
            _OPENROUTER_FETCHED_AT = now
            return ids
    except Exception as exc:  # network, JSON, or shape errors — non-fatal
        _LOGGER.debug("OpenRouter catalog fetch failed: %s", exc)
    return None


def available_models() -> list[str]:
    """Return a list of recommended models for the current provider.

    For OpenRouter, the curated frontier allowlist is intersected with the live
    catalog (when reachable) so retired slugs are dropped; the active model is
    always injected at the front if it isn't already present.
    """
    provider = _provider_name(LLM_BASE_URL)
    models = list(_PROVIDER_MODELS.get(provider, []))
    if provider == "openrouter":
        live = _openrouter_catalog()
        if live:
            filtered = [m for m in models if m in live]
            if len(filtered) >= 3:  # ignore an implausibly small/partial catalog
                models = filtered
    if LLM_MODEL not in models:
        models.insert(0, LLM_MODEL)
    return models


def accepts_images(model: Optional[str] = None) -> Optional[bool]:
    """Whether ``model`` (default: the active one) takes image input.

    ``None`` means unknown: only OpenRouter publishes modalities, and a
    provider that does not say is not a reason to refuse. A model the catalog
    lists with no modalities at all is also unknown rather than text-only.
    """
    model = model or LLM_MODEL
    if _provider_name(LLM_BASE_URL) != "openrouter":
        return None
    live = _openrouter_catalog()
    if not live or model not in live or not live[model]:
        return None
    return "image" in live[model]


def vision_models() -> Optional[list[str]]:
    """The offered models that take an image, or ``None`` when nobody knows."""
    answers = {m: accepts_images(m) for m in available_models()}
    if all(a is None for a in answers.values()):
        return None
    return [m for m, a in answers.items() if a is not False]


def current_config() -> dict[str, Any]:
    """Return non-sensitive runtime LLM configuration."""
    return {
        "configured": is_configured(),
        "provider": _provider_name(LLM_BASE_URL),
        "base_url": LLM_BASE_URL,
        "model": LLM_MODEL,
        "available_models": available_models(),
        "vision_models": vision_models(),
    }


_DESIGN_INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"enum": ["part", "assembly"]},
        "reasoning": {"type": "string"},
        "part": {
            "type": "object",
            "properties": {
                "shape": {
                    "enum": ["box", "cube", "cylinder", "sphere", "pipe", "other"]
                },
                "dimensions": {
                    "type": "object",
                    "properties": {
                        "x": {"type": "number"},
                        "y": {"type": "number"},
                        "z": {"type": "number"},
                    },
                },
                "unit": {"enum": ["mm", "cm", "m", "inch", None]},
                "modifiers": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
        },
        "assembly": {
            "type": "object",
            "properties": {
                "kind": {"enum": ["truss", "frame", "tower", "bridge", "space_frame", "portal", "radial", "gantry", "crane", "scissor", "platform", "arch", "cantilever", "custom"]},
                "description": {"type": "string"},
                "span": {"type": "number"},
                "height": {"type": "number"},
                "width": {"type": "number"},
                "bays": {"type": "integer"},
                "levels": {"type": "integer"},
                "complexity": {"enum": [1, 2, 3]},
                "size": {"type": "number"},
                "unit": {"enum": ["mm", "cm", "m", "inch", None]},
            },
        },
    },
    "required": ["type"],
}

_SYSTEM_PROMPT = f"""You are a CAD design assistant. Given a user's description, decide whether they want a single part or an assembly of parts and extract the key parameters.

Respond ONLY with a JSON object that matches this schema:
{json.dumps(_DESIGN_INTENT_SCHEMA, indent=2)}

Rules:
- "type" must be "part" or "assembly".
- For a "part", fill the "part" object. Dimensions should be in the requested unit; include "unit" explicitly.
- Shapes:
  - box/cube: dimensions x=width, y=depth, z=height.
  - cylinder: dimensions x=radius, y=height, z=ignored.
  - sphere: dimensions x=radius.
- Modifiers are free-text hints like ["two mounting holes", "rounded corners"].
- For an "assembly", fill the "assembly" object.
  - kind may be truss, frame, tower, bridge, space_frame, portal, radial, gantry, crane, scissor, platform, arch, cantilever, or custom.
  - Do not default to "truss" unless the description is clearly a triangular truss. Choose the kind that best matches the user's words.
  - include span, height, width (in meters), bays, levels, and complexity (1=simple, 2=normal, 3=heavy/detailed) when inferable.
  - "description" should be a concise, generator-friendly summary (e.g. "truss span 2 meters height 0.4 meters 5 bays complexity 2").
- If dimensions are missing, set them to null rather than guessing.
- Keep reasoning concise.
"""


# ---------------------------------------------------------------------------
# Feature-tree generation: the LLM emits real kernel geometry, not a shape enum.
# ---------------------------------------------------------------------------

_FEATURE_TREE_SYSTEM_PROMPT = """You are a solid-modeling CAD engine. Convert the user's description into a Forge feature tree (JSON) that an SDF kernel evaluates into a real 3D solid.

Output ONLY a JSON object of the form: {"version": "brep-v1", "root": NODE, "dimensions": [DIM, ...]}
"dimensions" is OPTIONAL: 2-4 callouts of the part's most important dimensions (overall extents, key hole spacing). Each DIM is {"a":{"x":n,"y":n,"z":n},"b":{...},"label":str} where a and b are the two metre-space endpoints the dimension spans (e.g. the two ends of the overall width). Anchor them on the bounding box or a face.

NODE is one of these (ALL lengths in METERS; coordinates are absolute in a frame centered near the origin):
- {"type":"BoxFeature","name":str,"params":{"size":{"x":n,"y":n,"z":n},"center":{"x":n,"y":n,"z":n}}}
- {"type":"SphereFeature","name":str,"params":{"radius":n,"center":{...}}}
- {"type":"CylinderFeature","name":str,"params":{"radius":n,"height":n,"center":{...},"axis":{"x":0,"y":0,"z":1}}}
- {"type":"ConeFeature","name":str,"params":{"base_radius":n,"height":n,"center":{...},"axis":{...}}}
- {"type":"BooleanFeature","name":str,"params":{"operation":"union|subtract|intersect","children":[NODE,...]}}
- {"type":"FilletFeature","name":str,"params":{"radius":n,"operation":"union","children":[NODE,...]}}  (smooth/rounded blend between children)
- {"type":"ShellFeature","name":str,"params":{"thickness":n,"children":[NODE]}}  (hollow the child)
- {"type":"HoleFeature","name":str,"params":{"radius":n,"depth":n,"center":{...},"axis":{...},"children":[NODE]}}
- {"type":"PatternFeature","name":str,"params":{"kind":"linear|circular","count":int,"spacing":n,"axis":{...},"center":{...},"children":[NODE]}}
- {"type":"TransformFeature","name":str,"params":{"translate":{...},"rotate":{...},"scale":n,"children":[NODE]}}

Rules:
- Build REAL geometry with multiple features. Use BooleanFeature subtract (or HoleFeature) for holes/slots/pockets, union to add bosses/ribs/flanges, PatternFeature for repeated features, FilletFeature to round edges.
- Convert the user's units to meters (100 mm = 0.1, 5 cm = 0.05, 1 in = 0.0254).
- Center the part near the origin; give every feature an explicit center.
- Cutting tools (holes/slots) MUST fully pierce: make their length/height ~2x the wall so they cut all the way through.
- Prefer a handful of well-placed, correctly-sized features over many tiny ones. Keep depth under ~6 levels.
- If unsure of a dimension, pick a sensible engineering value.

Example — "a 100x60x20 mm mounting plate with a 12mm hole in each corner and rounded top edges":
{"version":"brep-v1","root":{"type":"BooleanFeature","name":"Plate","params":{"operation":"subtract","children":[
 {"type":"BoxFeature","name":"Body","params":{"size":{"x":0.1,"y":0.06,"z":0.02},"center":{"x":0,"y":0,"z":0}}},
 {"type":"CylinderFeature","name":"Hole +X+Y","params":{"radius":0.006,"height":0.05,"center":{"x":0.038,"y":0.018,"z":0},"axis":{"x":0,"y":0,"z":1}}},
 {"type":"CylinderFeature","name":"Hole -X+Y","params":{"radius":0.006,"height":0.05,"center":{"x":-0.038,"y":0.018,"z":0},"axis":{"x":0,"y":0,"z":1}}},
 {"type":"CylinderFeature","name":"Hole -X-Y","params":{"radius":0.006,"height":0.05,"center":{"x":-0.038,"y":-0.018,"z":0},"axis":{"x":0,"y":0,"z":1}}},
 {"type":"CylinderFeature","name":"Hole +X-Y","params":{"radius":0.006,"height":0.05,"center":{"x":0.038,"y":-0.018,"z":0},"axis":{"x":0,"y":0,"z":1}}}
]}},"dimensions":[
 {"a":{"x":-0.05,"y":-0.03,"z":0.01},"b":{"x":0.05,"y":-0.03,"z":0.01},"label":"Width 100"},
 {"a":{"x":0.05,"y":-0.03,"z":0.01},"b":{"x":0.05,"y":0.03,"z":0.01},"label":"Depth 60"},
 {"a":{"x":-0.038,"y":0.018,"z":0},"b":{"x":0.038,"y":0.018,"z":0},"label":"Hole spacing 76"}
]}
"""


def generate_feature_tree(prompt: str) -> Optional[dict[str, Any]]:
    """Ask the LLM to emit a full kernel feature tree. Returns None on any failure."""
    if not is_configured():
        return None

    messages = [
        {"role": "system", "content": _FEATURE_TREE_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    try:
        content = _call_llm(messages, timeout=60.0, max_tokens=3000)
    except Exception as exc:
        _LOGGER.warning("LLM feature-tree request failed: %s", exc)
        return None
    return _parse_tree_response(content)


def _parse_tree_response(content: str) -> Optional[dict[str, Any]]:
    json_text = _extract_json(content)
    if json_text is None:
        _LOGGER.warning("LLM feature-tree response had no JSON: %s", content[:200])
        return None
    try:
        tree = json.loads(json_text)
    except json.JSONDecodeError as exc:
        _LOGGER.warning("LLM feature-tree JSON parse failed: %s", exc)
        return None
    if not isinstance(tree, dict) or "root" not in tree:
        _LOGGER.warning("LLM feature-tree missing 'root': %s", str(tree)[:200])
        return None
    tree.setdefault("version", "brep-v1")
    return tree


def edit_feature_tree(instruction: str, current: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Apply a natural-language edit to an existing feature tree; return the new tree."""
    if not is_configured():
        return None

    system = (
        _FEATURE_TREE_SYSTEM_PROMPT
        + "\nYou are EDITING an existing feature tree. Preserve features the instruction "
        "does not mention; change only what is asked. Return the COMPLETE modified tree."
    )
    user = (
        f"Current feature tree:\n{json.dumps(current)}\n\n"
        f"Apply this change and return the full modified tree as JSON only:\n{instruction}"
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    try:
        content = _call_llm(messages, timeout=60.0, max_tokens=3000)
    except LLMFailure:
        raise
    except Exception as exc:
        _LOGGER.warning("LLM edit request failed: %s", exc)
        return None
    return _parse_tree_response(content)


def repair_feature_tree(prompt: str, previous: dict[str, Any], error: str) -> Optional[dict[str, Any]]:
    """One correction pass: feed the failed tree + error back for a fix."""
    if not is_configured():
        return None

    user = (
        f"Your previous feature tree failed to build.\n\n"
        f"Request: {prompt}\n\n"
        f"Previous JSON:\n{json.dumps(previous)}\n\n"
        f"Error: {error}\n\n"
        "Return a CORRECTED feature tree as JSON only. Common fixes: every "
        "BooleanFeature/FilletFeature/ShellFeature/HoleFeature/PatternFeature/"
        "TransformFeature must have a non-empty 'children' list; a subtract/hole "
        "tool must physically overlap the body it cuts; all lengths are in metres; "
        "keep nesting shallow."
    )
    messages = [
        {"role": "system", "content": _FEATURE_TREE_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]
    try:
        content = _call_llm(messages, timeout=60.0, max_tokens=3000)
    except Exception as exc:
        _LOGGER.warning("LLM repair request failed: %s", exc)
        return None
    return _parse_tree_response(content)


def is_configured() -> bool:
    """Return True when an LLM API key is available."""
    return bool(LLM_API_KEY)


def _extract_json(text: str) -> Optional[str]:
    """Pull a JSON object out of a markdown-fenced or raw response."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        return fenced.group(1)
    if text.startswith("{") and text.endswith("}"):
        return text
    match = re.search(r"\{.*\}", text, re.DOTALL)
    return match.group(0) if match else None


def _headers() -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
    }
    # OpenRouter recommends these headers for ranking and stats.
    if "openrouter.ai" in LLM_BASE_URL:
        headers["HTTP-Referer"] = os.getenv("OPENROUTER_REFERER", "http://localhost:8001")
        headers["X-Title"] = os.getenv("OPENROUTER_TITLE", "Forge CAD")
    return headers


#: Words that mark a 403 as a spending cap rather than a permission problem.
_BUDGET_WORDS = ("budget", "credit", "quota", "limit exceeded", "insufficient")


def _provider_message(resp) -> str:
    """What the provider said went wrong, or "" when it said nothing usable.

    Without this a spend cap reads as a bare "Client error '403 Forbidden'",
    which names neither the cause nor the account it belongs to. Never raises:
    an error body is exactly where a provider is least likely to send the JSON
    it promised, and the failure being reported matters more than this line.
    """
    try:
        body = resp.json()
    except Exception:
        body = None
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            text = error.get("message") or error.get("code") or ""
        elif isinstance(error, str):
            text = error
        else:
            text = body.get("message") or ""
        if str(text).strip():
            return str(text).strip()[:300]
    try:
        return (resp.text or "").strip()[:300]
    except Exception:
        return ""


def _post_chat(messages: list[dict[str, Any]], *, timeout: float = 60.0,
               max_tokens: Optional[int] = None,
               tools: Optional[list[dict[str, Any]]] = None,
               model: Optional[str] = None) -> dict[str, Any]:
    """One chat-completion round trip, returning the whole assistant message.

    Separate from `_call_llm` because a tool call has no ``content`` — the model
    answers in ``tool_calls`` — and the older helper's "empty content is an
    error" rule would reject exactly the responses the strong path wants.
    """
    if not LLM_API_KEY:
        raise RuntimeError("LLM_API_KEY is not set")

    url = f"{LLM_BASE_URL.rstrip('/')}/chat/completions"
    payload: dict[str, Any] = {
        # Per call, falling back to the configured default. `set_model` is
        # process-global and shared by every tenant; comparing two models on one
        # task must not mean changing what everybody else's next run uses.
        "model": model or LLM_MODEL,
        "messages": messages,
        "temperature": 0.2,
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
        # One call per turn. Nothing here executes a batch — the copilot plans
        # each step against the state the previous one left — and left to
        # default, a current model will happily return `set_spec` and
        # `add_primitive` together, which is two sensible actions and was a run
        # that stopped before doing either.
        payload["parallel_tool_calls"] = False

    from app.services import trajectories
    context = CALL_CONTEXT.get() or {}
    payload["model"] = model or context.get("model") or LLM_MODEL
    deadline = context.get("deadline", time.monotonic() + timeout * 2)
    for attempt in range(2):
        if context.get("cancelled") and context["cancelled"]():
            raise LLMFailure("CANCELLED", "Stopped before another provider call.", "Continue when ready.")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LLMFailure("TIMEOUT", "The run's time budget expired.", "Continue from the saved plan.")
        started = time.monotonic()
        try:
            with httpx.Client(timeout=min(timeout, remaining)) as client:
                resp = client.post(url, headers=_headers(), json=payload)
            status = resp.status_code
            if status >= 400:
                detail = _provider_message(resp)
                code, remedy = {
                    401: ("PROVIDER_ACCESS", "Check the provider API key."),
                    402: ("PROVIDER_BILLING", "Check your provider balance and billing limits, then continue."),
                    403: ("PROVIDER_ACCESS", "Check provider account and model access, then continue."),
                    429: ("PROVIDER_THROTTLED", "Wait briefly, then continue."),
                }.get(status, ("PROVIDER_ERROR", "Check provider availability and model configuration."))
                # A spend cap arrives as 403 rather than 402, and "check model
                # access" then sends the reader to the wrong settings page. The
                # provider's own words are what say which it is.
                if status == 403 and any(word in detail.lower() for word in _BUDGET_WORDS):
                    code = "PROVIDER_BILLING"
                    remedy = ("Raise the provider's spending limit or wait for it to "
                              "reset, then continue.")
                raise LLMFailure(code,
                                 f"Provider request failed (HTTP {status}): {detail}"
                                 if detail else f"Provider request failed (HTTP {status}).",
                                 remedy, status=status,
                                 retryable=status == 429 or status >= 500)
            data = resp.json()
            choices = data.get("choices") or []
            message = (choices[0].get("message") or {}) if choices else {}
            finish = choices[0].get("finish_reason") if choices else None
            metadata = {"model": payload["model"], "finish_reason": finish,
                        "request_id": str(resp.headers.get("x-request-id") or data.get("id") or "")[:160],
                        "usage": {k: v for k, v in (data.get("usage") or {}).items()
                                  if k in ("prompt_tokens", "completion_tokens", "total_tokens") and isinstance(v, (int, float))},
                        "latency_ms": round((time.monotonic() - started) * 1000), "attempt": attempt + 1}
            trajectories.event("provider_response", **metadata)
            if message.get("refusal") or finish == "content_filter":
                raise LLMFailure("PROVIDER_REFUSAL", "The provider declined this request.", "Review the request before continuing.")
            if finish == "length":
                raise LLMFailure("PROVIDER_TRUNCATED", "The provider response was incomplete.", "Simplify the next step and continue.", retryable=True)
            if not message.get("tool_calls") and not str(message.get("content") or "").strip():
                raise LLMFailure("PROVIDER_EMPTY", "The provider returned an empty response.", "Continue to retry the next planning step.", retryable=True)
            message["_provider"] = metadata
            return message
        # **First, because `LLMFailure` is a `ValueError`.** It subclasses
        # `OperationError`, which subclasses `ValueError`, so the broad clause
        # below caught every typed failure raised inside this try — a 403 spend
        # cap, a refusal, an empty answer — and rewrote it as "invalid
        # response", dropping the code, the status and the remedy. Clauses are
        # tried in order, so this one has to come first.
        except LLMFailure as exc:
            failure = exc
        except (httpx.TimeoutException, httpx.TransportError):
            failure = LLMFailure("PROVIDER_TIMEOUT", "The provider connection timed out or failed.",
                                 "Reconnect and continue from the saved plan.", retryable=True)
        except (ValueError, TypeError, AttributeError, KeyError, IndexError):
            failure = LLMFailure("PROVIDER_PROTOCOL", "The provider returned an invalid response.",
                                 "Retry the next planning step.", retryable=True)
        trajectories.event("provider_error", **failure.to_dict(), model=payload["model"], attempt=attempt + 1)
        if not failure.retryable or attempt == 1:
            raise failure
        # Short, cooperative backoff; another attempt still shares the deadline.
        for _ in range(5):
            if time.monotonic() >= deadline or (context.get("cancelled") and context["cancelled"]()):
                break
            time.sleep(0.1)


def supports_tools() -> bool:
    """Whether to hand the provider real tool schemas.

    Opt-out rather than opt-in: the OpenAI-style ``tools`` field is what every
    provider this client targets speaks, and a provider that ignores it returns
    ordinary content, which the caller already handles. `FORGE_LLM_TOOLS=0`
    forces the prose path for one that rejects the field outright.
    """
    return os.getenv("FORGE_LLM_TOOLS", "1") not in ("0", "false", "no")


def _call_llm(messages: list[dict[str, str]], timeout: float = 60.0, max_tokens: Optional[int] = None) -> str:
    """Send a chat-completion request and return its text content."""
    content = _post_chat(messages, timeout=timeout, max_tokens=max_tokens).get("content", "")
    if not content:
        raise LLMFailure("PROVIDER_EMPTY", "The provider returned no text.", "Retry with a smaller editing step.")
    return content


def interpret_prompt(prompt: str) -> Optional[dict[str, Any]]:
    """Ask the LLM to classify the prompt and extract structured design intent.

    Returns None if no LLM is configured or the response cannot be parsed.
    """
    if not is_configured():
        return None

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]

    try:
        content = _call_llm(messages)
    except Exception as exc:
        _LOGGER.warning("LLM request failed: %s", exc)
        return None

    json_text = _extract_json(content)
    if json_text is None:
        _LOGGER.warning("LLM response did not contain valid JSON: %s", content)
        return None

    try:
        intent = json.loads(json_text)
    except json.JSONDecodeError as exc:
        _LOGGER.warning("LLM response JSON parsing failed: %s", exc)
        return None

    if not isinstance(intent, dict) or intent.get("type") not in ("part", "assembly"):
        _LOGGER.warning("LLM intent missing required fields: %s", intent)
        return None

    return intent

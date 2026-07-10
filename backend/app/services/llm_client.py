"""Optional LLM client for interpreting natural-language CAD prompts.

The client is provider-agnostic and speaks the OpenAI-style chat-completion
protocol used by Kimi/Moonshot and OpenRouter. Configuration is opt-in via
environment variables; when no key is configured the app falls back to the
rule-based parser.

Supported environment variables (in order of precedence):
  LLM_API_KEY / KIMI_API_KEY
  LLM_BASE_URL / KIMI_BASE_URL   (default: https://api.moonshot.cn/v1)
  LLM_MODEL / KIMI_MODEL         (default: moonshot-v1-8k)

For OpenRouter, set LLM_BASE_URL=https://openrouter.ai/api/v1 and
LLM_MODEL to a routed model id, e.g. openai/gpt-3.5-turbo.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Optional

import httpx

_LOGGER = logging.getLogger(__name__)

LLM_API_KEY = os.getenv("LLM_API_KEY") or os.getenv("KIMI_API_KEY")
LLM_BASE_URL = os.getenv("LLM_BASE_URL") or os.getenv("KIMI_BASE_URL", "https://api.moonshot.cn/v1")
LLM_MODEL = os.getenv("LLM_MODEL") or os.getenv("KIMI_MODEL", "moonshot-v1-8k")

# Curated model lists by provider. OpenRouter supports many more; these are common, reliable defaults.
_PROVIDER_MODELS: dict[str, list[str]] = {
    "openrouter": [
        "openai/gpt-4o-mini",
        "openai/gpt-3.5-turbo",
        "openai/gpt-4o",
        "anthropic/claude-3.5-sonnet",
        "anthropic/claude-3-haiku",
        "google/gemini-flash-1.5",
        "meta-llama/llama-3.1-70b-instruct",
    ],
    "moonshot": [
        "moonshot-v1-8k",
        "moonshot-v1-32k",
        "moonshot-v1-128k",
    ],
    "openai": [
        "gpt-4o-mini",
        "gpt-3.5-turbo",
        "gpt-4o",
    ],
}


def _provider_name(base_url: str) -> str:
    if "openrouter" in base_url:
        return "openrouter"
    if "moonshot" in base_url:
        return "moonshot"
    if "openai" in base_url:
        return "openai"
    return "custom"


def set_model(model: str) -> None:
    """Set the active chat-completion model at runtime."""
    global LLM_MODEL
    LLM_MODEL = model


def set_base_url(base_url: str) -> None:
    """Set the active API base URL at runtime."""
    global LLM_BASE_URL
    LLM_BASE_URL = base_url


def available_models() -> list[str]:
    """Return a list of recommended models for the current provider."""
    provider = _provider_name(LLM_BASE_URL)
    models = list(_PROVIDER_MODELS.get(provider, []))
    if LLM_MODEL not in models:
        models.insert(0, LLM_MODEL)
    return models


def current_config() -> dict[str, Any]:
    """Return non-sensitive runtime LLM configuration."""
    return {
        "configured": is_configured(),
        "provider": _provider_name(LLM_BASE_URL),
        "base_url": LLM_BASE_URL,
        "model": LLM_MODEL,
        "available_models": available_models(),
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
                    "enum": ["box", "cube", "cylinder", "sphere", "chassis", "pipe", "other"]
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
  - chassis: dimensions x=build_width, y=build_depth, z=build_height (build volume).
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

_FEATURE_TREE_SYSTEM_PROMPT = """You are a solid-modeling CAD engine. Convert the user's description into a DAPS feature tree (JSON) that an SDF kernel evaluates into a real 3D solid.

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
        headers["X-Title"] = os.getenv("OPENROUTER_TITLE", "DAPS CAD")
    return headers


def _call_llm(messages: list[dict[str, str]], timeout: float = 60.0, max_tokens: Optional[int] = None) -> str:
    """Send a chat-completion request to the configured LLM provider."""
    if not LLM_API_KEY:
        raise RuntimeError("LLM_API_KEY is not set")

    url = f"{LLM_BASE_URL.rstrip('/')}/chat/completions"
    payload: dict[str, Any] = {
        "model": LLM_MODEL,
        "messages": messages,
        "temperature": 0.2,
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens

    with httpx.Client(timeout=timeout) as client:
        resp = client.post(url, headers=_headers(), json=payload)
        resp.raise_for_status()
        data = resp.json()

    choices = data.get("choices", [])
    if not choices:
        raise RuntimeError("LLM returned no choices")
    content = choices[0].get("message", {}).get("content", "")
    if not content:
        raise RuntimeError("LLM returned empty content")
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

"""Text-to-assembly: one prompt → several generated parts placed as an assembly.

The LLM acts as an *assembly planner*, not a geometry engine: it decomposes the
prompt into distinct parts — each described by its own self-contained
text-to-CAD prompt — plus placements (a part appears once, instances place it
many times, so "a cart with four wheels" is two parts and five placements).
Every part then runs through the existing validated text-to-CAD pipeline
(LLM-first with evaluation + repair, rule parser fallback), and the placements
become ordinary ``AssemblyComponent`` transforms.

With no LLM configured this degrades to a single-part assembly from the rule
parser, per the project's credential-free policy. The planner is injectable for
tests, like the Copilot planner.
"""

from __future__ import annotations

import json
import logging
from typing import Callable, Dict, List, Optional

from app.models.schemas import AssemblyDoc, ComponentTransform, Vector3
from app.services import assembly_service, document_service, llm_client

_LOGGER = logging.getLogger(__name__)

MAX_PARTS = 6
MAX_COMPONENTS = 16

Planner = Callable[[str], dict]

_SYSTEM_PROMPT = """You are a mechanical design planner. Decompose the user's request into an assembly of parts.

Respond with EXACTLY ONE JSON object, nothing else:
{
  "name": "<assembly name>",
  "parts": [
    {"key": "<short_id>", "name": "<part name>", "prompt": "<self-contained text-to-CAD description with explicit dimensions in mm>"}
  ],
  "components": [
    {"part": "<key>", "translate_mm": {"x": 0, "y": 0, "z": 0}, "rotate_deg": {"x": 0, "y": 0, "z": 0}, "scale": 1.0}
  ]
}

Rules:
- Each part appears ONCE in "parts"; repeated hardware (wheels, bolts, feet) is one part with several "components" placements.
- Every part prompt must stand alone: name the shape and give real millimetre dimensions consistent with the other parts.
- Placements are in assembly space, millimetres; arrange parts in their assembled positions without overlapping solid volumes.
- Use at most %d parts and %d placements. Prefer the fewest parts that satisfy the request.
- Strings must be valid JSON: never use an inch mark (") inside a string — write dimensions as mm only.
""" % (MAX_PARTS, MAX_COMPONENTS)


def _ask_planner(messages: List[dict]) -> dict:
    content = llm_client._call_llm(messages, timeout=90.0, max_tokens=3000)
    raw = llm_client._extract_json(content)
    if raw is None:
        raise ValueError(f"assembly planner returned no JSON: {content[:200]}")
    return json.loads(raw)


def _plan(prompt: str) -> dict:
    """Ask the configured LLM for an assembly plan, with one self-repair retry."""
    messages = [{"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt}]
    try:
        return _ask_planner(messages)
    except (ValueError, json.JSONDecodeError) as exc:
        _LOGGER.info("assembly plan was invalid JSON (%s); retrying once", exc)
        retry = messages + [{
            "role": "user",
            "content": f"Your previous reply was not valid JSON ({exc}). "
                       "Reply again with ONLY the JSON object — no prose, no markdown, "
                       "and no unescaped quote characters inside strings.",
        }]
        return _ask_planner(retry)


def _fallback_plan(prompt: str) -> dict:
    """No LLM: a single-part assembly through the rule parser."""
    stripped = prompt.strip()
    name = stripped[:57] + "..." if len(stripped) > 60 else stripped
    return {
        "name": name,
        "parts": [{"key": "part", "name": name, "prompt": prompt}],
        "components": [{"part": "part"}],
    }


def _vec_mm(v: Optional[dict]) -> Vector3:
    v = v or {}
    return Vector3(x=float(v.get("x", 0)) / 1000.0,
                   y=float(v.get("y", 0)) / 1000.0,
                   z=float(v.get("z", 0)) / 1000.0)


def _vec_deg(v: Optional[dict]) -> Vector3:
    v = v or {}
    return Vector3(x=float(v.get("x", 0)), y=float(v.get("y", 0)), z=float(v.get("z", 0)))


def create_assembly_from_prompt(
    prompt: str,
    owner_id: Optional[str] = None,
    resolution: int = 96,
    planner: Optional[Planner] = None,
    on_progress: Optional[Callable[[str], None]] = None,
) -> AssemblyDoc:
    """Generate parts + assembly from one prompt. Raises ValueError on a bad plan."""

    def progress(msg: str) -> None:
        if on_progress is not None:
            try:
                on_progress(msg)
            except Exception:
                pass

    if planner is None:
        planner = _plan if llm_client.is_configured() else _fallback_plan

    progress("planning the assembly")
    plan = planner(prompt)
    parts: List[dict] = list(plan.get("parts") or [])[:MAX_PARTS]
    components: List[dict] = list(plan.get("components") or [])[:MAX_COMPONENTS]
    if not parts:
        raise ValueError("the planner produced no parts")
    if not components:  # place each part once if the planner omitted placements
        components = [{"part": p.get("key")} for p in parts]

    # Generate each part through the normal validated text-to-CAD pipeline.
    part_ids: Dict[str, str] = {}
    for i, p in enumerate(parts):
        key = str(p.get("key") or f"part{i}")
        part_prompt = (p.get("prompt") or "").strip()
        if not part_prompt:
            continue
        progress(f"generating part {i + 1}/{len(parts)}: {p.get('name') or key}")
        doc = document_service.create_document(
            name=p.get("name"), prompt=part_prompt, resolution=resolution, owner_id=owner_id
        )
        if doc.derived.error:
            _LOGGER.warning("text-to-assembly part '%s' evaluated with error: %s", key, doc.derived.error)
        part_ids[key] = doc.id
    if not part_ids:
        raise ValueError("no part in the plan produced valid geometry")

    progress("placing components")
    asm = assembly_service.create_assembly(name=plan.get("name") or prompt.strip()[:60], owner_id=owner_id)
    placed = 0
    for comp in components:
        pid = part_ids.get(str(comp.get("part")))
        if pid is None:
            continue
        updated = assembly_service.add_component(asm.id, pid)
        if updated is None:
            continue
        comp_id = updated.components[-1].id
        assembly_service.update_component(
            asm.id, comp_id,
            ComponentTransform(
                translate=_vec_mm(comp.get("translate_mm")),
                rotate_deg=_vec_deg(comp.get("rotate_deg")),
                scale=float(comp.get("scale") or 1.0),
            ),
        )
        placed += 1
    if placed == 0:
        raise ValueError("no component could be placed — the plan referenced unknown parts")

    result = assembly_service.get_assembly(asm.id)
    assert result is not None
    return result

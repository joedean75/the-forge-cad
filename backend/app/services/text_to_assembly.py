"""Text-to-assembly: one prompt → several generated parts placed as an assembly.

The LLM acts as an *assembly planner*, not a geometry engine: it decomposes the
prompt into distinct parts — each described by its own self-contained
text-to-CAD prompt — plus placements (a part appears once, instances place it
many times, so "a cart with four wheels" is two parts and five placements).
Every part then runs through the existing validated text-to-CAD pipeline
(LLM-first with evaluation + repair, rule parser fallback), and the placements
become ordinary ``AssemblyComponent`` transforms.

With no LLM configured this degrades to a single-part assembly from the rule
planner is injectable for
tests, like the Copilot planner.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List, Optional

from app.models.schemas import AssemblyDoc, ComponentTransform, Vector3
from app.services import assembly_service, document_service, llm_client

_LOGGER = logging.getLogger(__name__)

# How large an assembly the planner may describe.
#
# Six was a placeholder and it showed: the marquee demo could not describe a
# gearbox. The cost that bounds this is one text-to-CAD call per *part*, which
# is why parts and placements are capped separately — sixteen bolts placed from
# one part cost one call, and sixteen different parts cost sixteen.
#
# Exceeding either is **reported, not silently truncated**. The old code sliced
# the list and said nothing, so an assembly quietly arrived missing its last
# four parts and looked like the planner's fault.
MAX_PARTS = 16
MAX_COMPONENTS = 48

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


def _build_part(*, name, prompt, resolution, owner_id):
    """One part, through the normal validated text-to-CAD pipeline."""
    return document_service.create_document(
        name=name, prompt=prompt, resolution=resolution, owner_id=owner_id)


def _no_planner(prompt: str) -> dict:
    """No LLM: refuse, rather than plan an assembly nothing can build.

    This used to return a one-part plan and hand the prompt to the rule-based
    parser. That parser is gone — it answered any description with a box — so
    the "fallback" would now plan a part that cannot be built, and the failure
    would surface deep inside the job instead of at the request. Text-to-CAD
    raises the same condition for the same reason.
    """
    from app.services.text_to_cad_engine import TextToCadUnavailable

    raise TextToCadUnavailable(
        "text-to-assembly needs a language model and none is configured",
        remedy="set LLM_API_KEY in backend/.env, or create an assembly and add "
               "parts to it — insert_standard_part places purchased parts with "
               "no model involved")


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
    build_part: Optional[Callable[..., Any]] = None,
    on_progress: Optional[Callable[[str], None]] = None,
) -> AssemblyDoc:
    """Generate parts + assembly from one prompt. Raises ValueError on a bad plan.

    Both halves are injectable, and for the same reason. `planner` decides what
    parts there are; `build_part` turns one part's description into a document.
    By default both go through the LLM, and neither works without it — but the
    logic *between* them (one document per distinct part, instances sharing it,
    placement, mates) is ordinary code that should be testable on its own. It
    was not: with the planner injected the parts were still built by
    text-to-CAD, so a key was needed to exercise assembly composition at all.
    """

    def progress(msg: str) -> None:
        if on_progress is not None:
            try:
                on_progress(msg)
            except Exception:
                pass

    if planner is None:
        planner = _plan if llm_client.is_configured() else _no_planner

    progress("planning the assembly")
    plan = planner(prompt)
    requested = list(plan.get("parts") or [])
    parts: List[dict] = requested[:MAX_PARTS]
    dropped_parts = [str(p.get("name") or p.get("key") or "?") for p in requested[MAX_PARTS:]]
    requested_components = list(plan.get("components") or [])
    components: List[dict] = requested_components[:MAX_COMPONENTS]
    dropped_components = len(requested_components) - len(components)
    # Reported through `progress`, which the job forwards to the caller, and not
    # only to the log. A silent slice is how an assembly arrives missing its
    # last four parts and looks like the planner's fault.
    if dropped_parts:
        message = (f"the plan asked for {len(requested)} parts; this build is "
                   f"capped at {MAX_PARTS}, so these were left out: "
                   f"{', '.join(dropped_parts)}")
        _LOGGER.warning("%s", message)
        progress(message)
    if dropped_components:
        message = (f"the plan asked for {len(requested_components)} placements; "
                   f"capped at {MAX_COMPONENTS}, so {dropped_components} were "
                   f"left out")
        _LOGGER.warning("%s", message)
        progress(message)
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
        make = build_part or _build_part
        doc = make(name=p.get("name"), prompt=part_prompt,
                   resolution=resolution, owner_id=owner_id)
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

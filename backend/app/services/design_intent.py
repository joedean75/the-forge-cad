"""Unified design generation from a free-form prompt.

This module decides whether a prompt describes a single part or an assembly,
optionally asks Kimi for a structured interpretation, and then routes to the
existing deterministic geometry generators.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from app.cad_kernel.core.feature_tree import FeatureTree
from app.models.schemas import AssemblyResult, Candidate
from app.services import llm_client as llm
from app.services.assembly_generator import generate_assembly
from app.services.mesh_generator import candidate_from_feature_tree
from app.services.text_to_cad_geometry import parse_geometry_prompt

_LOGGER = logging.getLogger(__name__)

_ASSEMBLY_KEYWORDS = [
    "assembly",
    "assemblies",
    "truss",
    "frame",
    "tower",
    "mast",
    "strut",
    "struts",
    "structure",
    "multiple parts",
    "collection of parts",
    "nodes and struts",
    "space frame",
    "linked parts",
    "bridge",
    "portal",
    "gantry",
    "crane",
    "jib",
    "scissor",
    "lift",
    "platform",
    "shelf",
    "shelves",
    "rack",
    "table",
    "stand",
    "dome",
    "radial",
    "canopy",
    "awning",
    "roof",
    "arch",
    "cantilever",
    "custom",
]


def _to_meters(value: float | int | None, unit: str | None) -> float | None:
    """Convert a numeric value in the given unit to meters."""
    if value is None:
        return None
    unit = (unit or "m").lower()
    if unit == "mm":
        return float(value) / 1000.0
    if unit == "cm":
        return float(value) / 100.0
    if unit in ("in", "inch"):
        return float(value) * 0.0254
    return float(value)


def _looks_like_assembly(prompt: str) -> bool:
    lower = prompt.lower()
    return any(keyword in lower for keyword in _ASSEMBLY_KEYWORDS)


def _canonical_assembly_prompt(intent: dict[str, Any]) -> Optional[str]:
    """Turn a Kimi assembly intent into a parser-friendly prompt string."""
    assembly = intent.get("assembly", {}) or {}
    if not assembly:
        return None
    kind = assembly.get("kind") or "truss"
    unit = assembly.get("unit")
    parts = []
    if kind and kind != "custom":
        parts.append(kind)
    span = _to_meters(assembly.get("span"), unit)
    height = _to_meters(assembly.get("height"), unit)
    width = _to_meters(assembly.get("width"), unit)
    bays = assembly.get("bays")
    levels = assembly.get("levels")
    complexity = assembly.get("complexity")

    if span is not None:
        parts.append(f"span {span:g} m")
    if height is not None:
        parts.append(f"height {height:g} m")
    if width is not None:
        parts.append(f"width {width:g} m")
    if bays is not None:
        parts.append(f"{bays} bays")
    if levels is not None:
        parts.append(f"{levels} levels")
    if complexity is not None:
        parts.append(f"complexity {complexity}")

    return " ".join(parts)


def _canonical_part_prompt(intent: dict[str, Any]) -> str:
    """Turn a Kimi part intent into a parser-friendly prompt string."""
    part = intent.get("part", {}) or {}
    shape = (part.get("shape") or "box").lower()
    dims = part.get("dimensions") or {}
    unit = part.get("unit")

    x = _to_meters(dims.get("x"), unit)
    y = _to_meters(dims.get("y"), unit)
    z = _to_meters(dims.get("z"), unit)

    if shape in ("box", "cube"):
        if x is not None and y is not None and z is not None:
            prompt = f"box {x:g}x{y:g}x{z:g} m"
        elif x is not None and shape == "cube":
            prompt = f"cube {x:g} m"
        else:
            prompt = "box 0.1x0.05x0.03 m"
    elif shape == "cylinder":
        radius = x if x is not None else 0.05
        height = y if y is not None else 0.1
        prompt = f"cylinder radius {radius:g} m height {height:g} m"
    elif shape == "sphere":
        radius = x if x is not None else 0.05
        prompt = f"sphere radius {radius:g} m"
    elif shape == "chassis":
        if x is not None and y is not None and z is not None:
            prompt = f"chassis on a {x:g}x{y:g}x{z:g} m build plate"
        else:
            prompt = "chassis on a 0.2x0.2x0.2 m build plate"
    elif shape == "pipe":
        radius = x if x is not None else 0.02
        height = y if y is not None else 0.1
        prompt = f"pipe radius {radius:g} m height {height:g} m"
    else:
        prompt = "box 0.1x0.05x0.03 m"

    modifiers = part.get("modifiers")
    if modifiers:
        prompt += " with " + ", ".join(str(m) for m in modifiers)

    return prompt


def _generate_part(prompt: str, resolution: int) -> Candidate:
    """Generate a Candidate from a prompt, using the rule-based kernel parser."""
    tree = parse_geometry_prompt(prompt)
    name = prompt.strip()
    if len(name) > 60:
        name = name[:57] + "..."
    return candidate_from_feature_tree(tree, name=name or "Generated Part", resolution=resolution)


def classify_and_generate(
    prompt: str, resolution: int = 128
) -> dict[Literal["kind"], Literal["part", "assembly"]] | dict[str, Any]:
    """Classify a prompt and generate either a part or an assembly.

    Returns a dict with ``kind`` and either ``part`` or ``assembly`` populated.
    """
    prompt = prompt.strip()
    if not prompt:
        raise ValueError("Prompt is required")

    intent: dict[str, Any] | None = None
    if llm.is_configured():
        intent = llm.interpret_prompt(prompt)
        if intent:
            _LOGGER.info("Kimi intent: %s", intent.get("reasoning"))

    # Decide part vs assembly.
    is_assembly = (
        intent is not None and intent.get("type") == "assembly"
    ) or _looks_like_assembly(prompt)

    if is_assembly:
        assembly_prompt = prompt
        if intent and intent.get("type") == "assembly":
            canonical = _canonical_assembly_prompt(intent)
            if canonical:
                assembly_prompt = canonical
                _LOGGER.info("Routing Kimi assembly intent to parser prompt: %s", assembly_prompt)
            elif intent.get("assembly", {}).get("description"):
                assembly_prompt = intent["assembly"]["description"]
        assembly = generate_assembly(assembly_prompt, resolution=resolution)
        return {"kind": "assembly", "assembly": assembly}

    # Generate a single part.
    part_prompt = prompt
    if intent is not None and intent.get("type") == "part":
        part_prompt = _canonical_part_prompt(intent)
        _LOGGER.info("Routing Kimi part intent to parser prompt: %s", part_prompt)

    part = _generate_part(part_prompt, resolution)
    return {"kind": "part", "part": part}

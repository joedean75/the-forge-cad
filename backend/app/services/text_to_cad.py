"""
Text-to-CAD prompt interpreter.

Parses a natural-language description and produces a DesignSpec that can be
fed into the SIMP topology optimizer.  The parser is intentionally simple and
rule-based so it works without external LLM credentials, while still handling
common design phrases.
"""

import re
import math
from typing import Optional, Tuple

from app.models.schemas import (
    DesignSpec,
    DesignSpace,
    Vector3,
    Objective,
    PreserveRegion,
    ObstacleRegion,
    LoadCase,
    Constraint,
)


_MATERIAL_ALIASES = {
    "al-7075": ["7075", "al 7075", "aluminum 7075"],
    "al-6061": ["6061", "al 6061", "aluminum 6061", "aluminium", "aluminum", "al"],
    "ti-6al-4v": ["ti-6al-4v", "ti6al4v", "titanium", "ti"],
    "steel-4140": ["4140", "steel 4140", "steel", "stainless"],
}


def _detect_material(prompt: str, current_id: str = "al-6061") -> str:
    lower = prompt.lower()
    for mat_id, aliases in _MATERIAL_ALIASES.items():
        for alias in aliases:
            if re.search(r"\b" + re.escape(alias) + r"\b", lower):
                return mat_id
    return current_id


def _detect_objective(prompt: str, current: str = "maximize_stiffness") -> str:
    lower = prompt.lower()
    if any(p in lower for p in ["minimize mass", "minimise mass", "lightweight", "light weight", "least mass"]):
        return "minimize_mass"
    if any(p in lower for p in ["minimize displacement", "minimise displacement", "least deflection"]):
        return "minimize_displacement"
    if any(p in lower for p in ["maximize stiffness", "maximise stiffness", "stiff", "stiffness"]):
        return "maximize_stiffness"
    return current


def _parse_number_with_unit(text: str, default_unit: str = "m") -> Optional[float]:
    """Return a length in meters, or None."""
    text = text.strip().replace(" ", "")
    m = re.match(r"^(\d+\.?\d*)\s*(mm|cm|m|in|inch)?$", text)
    if not m:
        return None
    value = float(m.group(1))
    unit = (m.group(2) or default_unit).lower()
    if unit in ("mm",):
        return value / 1000.0
    if unit in ("cm",):
        return value / 100.0
    if unit in ("in", "inch"):
        return value * 0.0254
    return value


def _detect_default_unit(prompt: str) -> str:
    """Infer the default length unit from printer/build-plate context."""
    lower = prompt.lower()
    if any(
        k in lower
        for k in [
            "mm",
            "millimeter",
            "millimeters",
            "3d printer",
            "build plate",
            "build volume",
            "printer",
            "nozzle",
        ]
    ):
        return "mm"
    return "m"


def _detect_dimensions(prompt: str, default_unit: str = "m") -> Optional[Tuple[float, float, float]]:
    lower = prompt.lower()

    # "0.2 x 0.1 x 0.1 m" or "100mm x 50mm x 30mm"
    dim3 = re.search(
        r"(\d+\.?\d*)\s*(mm|cm|m|in|inch)?\s*x\s*(\d+\.?\d*)\s*(mm|cm|m|in|inch)?\s*x\s*(\d+\.?\d*)\s*(mm|cm|m|in|inch)?",
        lower,
    )
    if dim3:
        vals = [
            _parse_number_with_unit(f"{dim3.group(1)}{dim3.group(2) or ''}", default_unit),
            _parse_number_with_unit(f"{dim3.group(3)}{dim3.group(4) or ''}", default_unit),
            _parse_number_with_unit(f"{dim3.group(5)}{dim3.group(6) or ''}", default_unit),
        ]
        if all(v is not None for v in vals):
            return tuple(vals)  # type: ignore

    # "2m long, 0.2m x 0.2m cross-section"
    long2 = re.search(
        r"(\d+\.?\d*)\s*(mm|cm|m|in|inch)?\s*(?:long|length).*?(\d+\.?\d*)\s*(mm|cm|m|in|inch)?\s*x\s*(\d+\.?\d*)\s*(mm|cm|m|in|inch)?",
        lower,
    )
    if long2:
        length = _parse_number_with_unit(f"{long2.group(1)}{long2.group(2) or ''}", default_unit)
        width = _parse_number_with_unit(f"{long2.group(3)}{long2.group(4) or ''}", default_unit)
        height = _parse_number_with_unit(f"{long2.group(5)}{long2.group(6) or ''}", default_unit)
        if all(v is not None for v in (length, width, height)):
            return (length, width, height)  # type: ignore

    # "2m long with 0.2m x 0.2m section"
    long_section = re.search(
        r"(\d+\.?\d*)\s*(mm|cm|m|in|inch)?\s*(?:long|length).*?(\d+\.?\d*)\s*(mm|cm|m|in|inch)?\s*(?:by|x)\s*(\d+\.?\d*)\s*(mm|cm|m|in|inch)?",
        lower,
    )
    if long_section:
        length = _parse_number_with_unit(f"{long_section.group(1)}{long_section.group(2) or ''}", default_unit)
        width = _parse_number_with_unit(f"{long_section.group(3)}{long_section.group(4) or ''}", default_unit)
        height = _parse_number_with_unit(f"{long_section.group(5)}{long_section.group(6) or ''}", default_unit)
        if all(v is not None for v in (length, width, height)):
            return (length, width, height)  # type: ignore

    return None


def _parse_force(prompt: str) -> Optional[Tuple[float, str]]:
    """Parse a force magnitude and rough direction hint."""
    lower = prompt.lower()
    m = re.search(r"(\d+\.?\d*)\s*(kn|n)\b", lower)
    if not m:
        return None
    value = float(m.group(1))
    unit = m.group(2).lower()
    if unit == "kn":
        value *= 1000.0

    # direction
    dir_hint = "down"
    if any(w in lower for w in ["upward", "up"]):
        dir_hint = "up"
    elif any(w in lower for w in ["sideways", "side", "lateral", "horizontal"]):
        dir_hint = "side"
    elif "x-axis" in lower or " along x" in lower:
        dir_hint = "x"
    elif "y-axis" in lower or " along y" in lower:
        dir_hint = "y"
    elif "z-axis" in lower or " along z" in lower:
        dir_hint = "z"

    return value, dir_hint


def _location_from_prompt(prompt: str, default: str = "top") -> str:
    lower = prompt.lower()
    if "top" in lower:
        return "top"
    if "bottom" in lower:
        return "bottom"
    if "center" in lower:
        return "center"
    if "side" in lower or "edge" in lower:
        return "side"
    return default


def _point_from_location(location: str, sx: float, sy: float, sz: float) -> Vector3:
    hx, hy, hz = sx / 2.0, sy / 2.0, sz / 2.0
    if location == "top":
        return Vector3(x=0, y=hy * 0.3, z=hz)
    if location == "bottom":
        return Vector3(x=0, y=0, z=-hz)
    if location == "center":
        return Vector3(x=0, y=0, z=0)
    if location == "side":
        return Vector3(x=hx, y=0, z=0)
    return Vector3(x=0, y=hy * 0.3, z=hz)


def _force_vector(value: float, hint: str) -> Vector3:
    if hint == "down":
        return Vector3(x=0, y=0, z=-value)
    if hint == "up":
        return Vector3(x=0, y=0, z=value)
    if hint == "side":
        return Vector3(x=value, y=0, z=0)
    if hint == "x":
        return Vector3(x=value, y=0, z=0)
    if hint == "y":
        return Vector3(x=0, y=value, z=0)
    if hint == "z":
        return Vector3(x=0, y=0, z=value)
    return Vector3(x=0, y=0, z=-value)


def _detect_obstacles(prompt: str, sx: float, sy: float, sz: float):
    lower = prompt.lower()
    obstacles = []
    # "hole(s) in the center"
    if "hole" in lower or "holes" in lower:
        count = 1
        m = re.search(r"(\d+)\s*hole", lower)
        if m:
            count = int(m.group(1))
        radius = min(sx, sy, sz) * 0.12
        if count == 1:
            obstacles.append(ObstacleRegion(
                id="hole-center",
                name="Center hole",
                type="sphere",
                center=Vector3(x=0, y=0, z=0),
                radius=radius,
            ))
        else:
            for i in range(count):
                x_off = sx * 0.25 * (i - (count - 1) / 2.0)
                obstacles.append(ObstacleRegion(
                    id=f"hole-{i}",
                    name=f"Hole {i+1}",
                    type="sphere",
                    center=Vector3(x=x_off, y=0, z=0),
                    radius=radius,
                ))
    return obstacles


def _detect_preserves(prompt: str, sx: float, sy: float, sz: float):
    lower = prompt.lower()
    preserves = []
    if any(w in lower for w in ["mount", "mounting", "interface", "pad", "boss"]):
        count = 2
        m = re.search(r"(\d+)\s*(?:mount|mounting|interface)", lower)
        if m:
            count = int(m.group(1))
        radius = min(sx, sy, sz) * 0.12
        for i in range(count):
            x_off = sx * 0.35 * (i - (count - 1) / 2.0)
            preserves.append(PreserveRegion(
                id=f"mount-{i}",
                name=f"Mount {i+1}",
                type="sphere",
                center=Vector3(x=x_off, y=0, z=sz * 0.25),
                radius=radius,
            ))
    return preserves


def _detect_constraints(prompt: str, sx: float, sy: float, sz: float):
    lower = prompt.lower()
    constraints = []
    if any(w in lower for w in ["fixed at the bottom", "clamped at the bottom", "grounded at the bottom"]):
        constraints.append(Constraint(
            id="fixed-bottom",
            name="Fixed bottom",
            type="fixed",
            point=Vector3(x=0, y=0, z=-sz / 2.0),
        ))
    if any(w in lower for w in ["fixed at corners", "clamped at corners"]):
        hx, hy = sx / 2.0, sy / 2.0
        for idx, (x, y) in enumerate([(-hx, -hy), (hx, -hy), (hx, hy), (-hx, hy)]):
            constraints.append(Constraint(
                id=f"fixed-corner-{idx}",
                name=f"Fixed corner {idx}",
                type="fixed",
                point=Vector3(x=x, y=y, z=-sz / 2.0),
            ))
    if "simply supported" in lower or "supported at ends" in lower:
        constraints.append(Constraint(
            id="support-1",
            name="Support 1",
            type="fixed",
            point=Vector3(x=-sx / 2.0, y=0, z=-sz / 2.0),
        ))
        constraints.append(Constraint(
            id="support-2",
            name="Support 2",
            type="fixed",
            point=Vector3(x=sx / 2.0, y=0, z=-sz / 2.0),
        ))
    if not constraints:
        # default: fixed bottom
        constraints.append(Constraint(
            id="fixed-bottom",
            name="Fixed bottom",
            type="fixed",
            point=Vector3(x=0, y=0, z=-sz / 2.0),
        ))
    return constraints


def parse_prompt(prompt: str, current: DesignSpec) -> DesignSpec:
    """Convert a text prompt into a new DesignSpec."""
    import copy
    spec = copy.deepcopy(current)
    lower = prompt.lower()

    material_id = _detect_material(prompt, current.material_id)
    objective = _detect_objective(prompt, current.objective.target)
    default_unit = _detect_default_unit(prompt)
    dims = _detect_dimensions(prompt, default_unit)

    if dims:
        sx, sy, sz = dims
        spec.design_space = DesignSpace(
            size=Vector3(x=sx, y=sy, z=sz),
            center=Vector3(x=0, y=0, z=0),
        )
    else:
        sx = spec.design_space.size.x
        sy = spec.design_space.size.y
        sz = spec.design_space.size.z

    spec.material_id = material_id
    spec.objective = Objective(target=objective, weight=1.0)

    # Load
    force = _parse_force(prompt)
    if force:
        value, hint = force
        location = _location_from_prompt(prompt, default="top")
        point = _point_from_location(location, sx, sy, sz)
        spec.load_cases = [LoadCase(
            id="prompt-load",
            name="Prompt load",
            point=point,
            force=_force_vector(value, hint),
            load_type="static",
        )]

    # Obstacles
    obstacles = _detect_obstacles(prompt, sx, sy, sz)
    if obstacles:
        spec.obstacle_regions = obstacles

    # Preserves
    preserves = _detect_preserves(prompt, sx, sy, sz)
    if preserves:
        spec.preserve_regions = preserves

    # Constraints
    constraints = _detect_constraints(prompt, sx, sy, sz)
    if constraints:
        spec.constraints = constraints

    return spec

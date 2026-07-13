"""Prompt-driven geometry parser for the Forge CAD kernel.

Turns natural-language shape descriptions into a kernel FeatureTree that can be
evaluated by the SDF evaluator.  The parser is intentionally rule-based so it
works without external LLM credentials.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

from app.cad_kernel.core.feature_tree import FeatureTree
from app.cad_kernel.core.types import Point3, Vector3
from app.cad_kernel.ops.booleans import BooleanFeature
from app.cad_kernel.ops.primitives import BoxFeature, CylinderFeature, SphereFeature
from app.services.text_to_cad import _detect_default_unit, _parse_number_with_unit


# ---------------------------------------------------------------------------
# Dimension parsing
# ---------------------------------------------------------------------------


def _number_unit_regex() -> str:
    return r"(\d+\.?\d*)\s*(mm|cm|m|in|inch)?"


def _parse_single(text: str, default_unit: str = "m") -> Optional[float]:
    return _parse_number_with_unit(text, default_unit)


def _parse_triplet(text: str, default_unit: str = "m") -> Optional[Tuple[float, float, float]]:
    """Parse '100x50x30 mm' or '0.1 x 0.05 x 0.03 m' into meters.

    If a unit appears on any value it is applied to all values.  Per-value
    units are also supported.
    """
    pattern = re.compile(
        r"(\d+\.?\d*)\s*(mm|cm|m|in|inch)?\s*[x,]\s*"
        r"(\d+\.?\d*)\s*(mm|cm|m|in|inch)?\s*[x,]\s*"
        r"(\d+\.?\d*)\s*(mm|cm|m|in|inch)?",
        re.IGNORECASE,
    )
    m = pattern.search(text)
    if not m:
        return None

    # Determine the dominant unit.  If any value has an explicit unit, use the
    # first one found; otherwise fall back to the prompt-inferred unit.
    unit = m.group(2) or m.group(4) or m.group(6) or default_unit

    def convert(value: str, value_unit: Optional[str]) -> Optional[float]:
        return _parse_single(f"{value}{value_unit or unit}", default_unit)

    vals = [
        convert(m.group(1), m.group(2)),
        convert(m.group(3), m.group(4)),
        convert(m.group(5), m.group(6)),
    ]
    if any(v is None for v in vals):
        return None
    return vals[0], vals[1], vals[2]  # type: ignore


# ---------------------------------------------------------------------------
# Clause helpers
# ---------------------------------------------------------------------------


def _word_count(text: str) -> int:
    numbers = {
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
    }
    lower = text.lower()
    for word, value in numbers.items():
        if re.search(r"\b" + word + r"\b", lower):
            return value
    m = re.search(r"\b(\d+)\s*(?:x\s*)?(?:mount|boss|hole|cylinder|sphere|pad)", lower)
    if m:
        return int(m.group(1))
    return 1


def _location_from_clause(text: str) -> str:
    lower = text.lower()
    if "top" in lower or "upper" in lower:
        return "top"
    if "bottom" in lower or "lower" in lower:
        return "bottom"
    if "front" in lower:
        return "front"
    if "back" in lower or "rear" in lower:
        return "back"
    if "left" in lower:
        return "left"
    if "right" in lower:
        return "right"
    if "center" in lower or "middle" in lower:
        return "center"
    return "center"


def _operation_from_clause(text: str, default: str = "union") -> str:
    lower = text.lower()
    subtract_words = ["subtract", "minus", "cut", "without", "remove", "hole", "holes", "drill"]
    if any(w in lower for w in subtract_words):
        return "subtract"
    if "intersect" in lower:
        return "intersect"
    union_words = ["with", "and", "plus", "add", "attach", "mount"]
    if any(w in lower for w in union_words):
        return "union"
    return default


# ---------------------------------------------------------------------------
# Primitive parsing
# ---------------------------------------------------------------------------


def _default_unit_for_text(text: str) -> str:
    return _detect_default_unit(text)


def _parse_box(text: str, default_unit: str = "m") -> Optional[BoxFeature]:
    dims = _parse_triplet(text, default_unit)
    if dims is None:
        return None
    return BoxFeature(size=Vector3(x=dims[0], y=dims[1], z=dims[2]), name="Box")


def _parse_sphere(text: str, default_unit: str = "m") -> Optional[SphereFeature]:
    pattern = re.compile(r"sphere\s+(?:radius\s+)?" + _number_unit_regex(), re.IGNORECASE)
    m = pattern.search(text)
    if m:
        radius = _parse_single(f"{m.group(1)}{m.group(2) or ''}", default_unit)
        if radius is not None:
            return SphereFeature(radius=radius, name="Sphere")
    return None


def _parse_cylinder(text: str, default_unit: str = "m") -> Optional[CylinderFeature]:
    lower = text.lower()
    # "cylinder radius 10 mm height 40 mm"
    pattern = re.compile(
        r"cylinder(?:\s+radius)?\s+" + _number_unit_regex() + r"(?:\s+(?:height|h)\s+" + _number_unit_regex() + r")?",
        re.IGNORECASE,
    )
    m = pattern.search(text)
    if m:
        radius = _parse_single(f"{m.group(1)}{m.group(2) or ''}", default_unit)
        height = _parse_single(f"{m.group(3)}{m.group(4) or ''}", default_unit) if m.group(3) else None
        if radius is not None:
            return CylinderFeature(
                radius=radius,
                height=height if height is not None else radius * 2.0,
                name="Cylinder",
            )
    # "10 mm hole" -> cylinder used as hole
    if "hole" in lower:
        pattern2 = re.compile(r"(\d+\.?\d*)\s*(mm|cm|m|in|inch)?\s*hole", re.IGNORECASE)
        m2 = pattern2.search(text)
        if m2:
            radius = _parse_single(f"{m2.group(1)}{m2.group(2) or ''}", default_unit)
            if radius is not None:
                return CylinderFeature(radius=radius, height=1.0, name="Hole")
    return None


def _parse_cube(text: str, default_unit: str = "m") -> Optional[BoxFeature]:
    lower = text.lower()
    if "cube" not in lower and "rectangular prism" not in lower:
        return None
    dims = _parse_triplet(text, default_unit)
    if dims:
        return BoxFeature(size=Vector3(x=dims[0], y=dims[1], z=dims[2]), name="Box")
    pattern = re.compile(r"cube(?:\s+of?)?\s+" + _number_unit_regex(), re.IGNORECASE)
    m = pattern.search(text)
    if m:
        side = _parse_single(f"{m.group(1)}{m.group(2) or ''}", default_unit)
        if side is not None:
            return BoxFeature(size=Vector3(x=side, y=side, z=side), name="Cube")
    return None


def _parse_pipe(text: str, default_unit: str = "m") -> Optional[BooleanFeature]:
    lower = text.lower()
    if "pipe" not in lower and "tube" not in lower:
        return None
    # "pipe radius 20 mm wall 3 mm [height 100 mm]"
    outer_pattern = re.compile(
        r"(?:radius|r)\s+" + _number_unit_regex() + r"(?:\s+wall\s+" + _number_unit_regex() + r")?",
        re.IGNORECASE,
    )
    m = outer_pattern.search(text)
    if not m:
        return None
    outer = _parse_single(f"{m.group(1)}{m.group(2) or ''}", default_unit)
    wall = _parse_single(f"{m.group(3)}{m.group(4) or ''}", default_unit) if m.group(3) else None
    if outer is None:
        return None
    inner = max(0.0, outer - (wall or outer * 0.2))

    height_pattern = re.compile(r"(?:height|length|h)\s+" + _number_unit_regex(), re.IGNORECASE)
    hm = height_pattern.search(text)
    height = _parse_single(f"{hm.group(1)}{hm.group(2) or ''}", default_unit) if hm else 0.1

    outer_cyl = CylinderFeature(radius=outer, height=height, name="Pipe outer")
    inner_cyl = CylinderFeature(radius=inner, height=height * 1.1, name="Pipe inner")
    return BooleanFeature(operation="subtract", children=[outer_cyl, inner_cyl], name="Pipe")


def _parse_primitive(text: str, default_unit: str = "m") -> Optional[object]:
    lower = text.lower()
    if "cube" in lower or "rectangular prism" in lower:
        cube = _parse_cube(text, default_unit)
        if cube is not None:
            return cube
    if "pipe" in lower or "tube" in lower:
        pipe = _parse_pipe(text, default_unit)
        if pipe is not None:
            return pipe
    # Box or any bare dimension triplet.
    if "box" in lower or re.search(r"\d+\.?\d*\s*(mm|cm|m|in|inch)?\s*[x,]\s*\d+\.?\d*\s*(mm|cm|m|in|inch)?\s*[x,]\s*\d+\.?\d*", text):
        box = _parse_box(text, default_unit)
        if box is not None:
            return box
    if "cylinder" in lower:
        cyl = _parse_cylinder(text, default_unit)
        if cyl is not None:
            return cyl
    if "sphere" in lower:
        sphere = _parse_sphere(text, default_unit)
        if sphere is not None:
            return sphere
    return None


def _extract_build_volume(text: str, default_unit: str = "m") -> Optional[Tuple[float, float, float]]:
    """Look for an explicit build-plate/build-volume dimension triplet."""
    lower = text.lower()
    # "... on a 185x185x185 mm 3D printer build plate"
    dim_group = r"(\d+\.?\d*\s*(?:mm|cm|m|in|inch)?\s*[x,]\s*\d+\.?\d*\s*(?:mm|cm|m|in|inch)?\s*[x,]\s*\d+\.?\d*\s*(?:mm|cm|m|in|inch)?)"
    m = re.search(dim_group + r"\s*(?:build\s+plate|build\s+volume|3d\s+printer|printer)", lower, re.IGNORECASE)
    if m:
        return _parse_triplet(m.group(1), default_unit)
    m = re.search(r"(?:build\s+plate|build\s+volume|3d\s+printer|printer)\s*(?:of\s+|at\s+|size\s+)?" + dim_group, lower, re.IGNORECASE)
    if m:
        return _parse_triplet(m.group(1), default_unit)
    return None


def _build_chassis(build_x: float, build_y: float, build_z: float) -> FeatureTree:
    """Generate a flat RC-style chassis deck that fits inside the build volume."""
    thickness = max(build_z * 0.05, 0.003)
    base_x = build_x * 0.9
    base_y = build_y * 0.9
    base = BoxFeature(
        size=Vector3(x=base_x, y=base_y, z=thickness),
        center=Point3(x=0, y=0, z=0),
        name="Chassis deck",
    )

    wheel_radius = min(build_x, build_y) * 0.12
    cut_height = thickness * 3.0
    half_x = base_x / 2.0
    half_y = base_y / 2.0
    wheel_positions = [
        (half_x * 0.8, half_y * 0.8),
        (-half_x * 0.8, half_y * 0.8),
        (-half_x * 0.8, -half_y * 0.8),
        (half_x * 0.8, -half_y * 0.8),
    ]

    cuts: List[Feature] = []
    for i, (wx, wy) in enumerate(wheel_positions):
        cuts.append(
            CylinderFeature(
                radius=wheel_radius,
                height=cut_height,
                center=Point3(x=wx, y=wy, z=0),
                name=f"Wheel cut {i + 1}",
            )
        )

    # Central + corner mounting holes.
    mount_radius = max(thickness * 0.3, 0.0015)
    for i, (mx, my) in enumerate([(0, 0)] + wheel_positions):
        cuts.append(
            CylinderFeature(
                radius=mount_radius,
                height=cut_height,
                center=Point3(x=mx, y=my, z=0),
                name=f"Mount hole {i + 1}",
            )
        )

    root = BooleanFeature(operation="subtract", children=[base] + cuts, name="RC chassis")
    return FeatureTree(root)


def _parse_chassis(prompt: str) -> Optional[FeatureTree]:
    """If the prompt describes an RC/rally chassis, build one sized to the build plate."""
    lower = prompt.lower()
    if "chassis" not in lower and "rc" not in lower and "rally" not in lower and "buggy" not in lower:
        return None
    default_unit = _default_unit_for_text(prompt)
    dims = _extract_build_volume(prompt, default_unit)
    if dims is None:
        dims = _parse_triplet(prompt, default_unit)
    if dims is None:
        dims = (0.2, 0.2, 0.2)
    return _build_chassis(*dims)


def _default_boss(base: object) -> CylinderFeature:
    bx, by, _ = _half_size(base)
    radius = min(bx, by) * 0.18
    return CylinderFeature(radius=radius, height=radius * 1.5, name="Boss")


def _default_hole(base: object) -> CylinderFeature:
    bx, by, bz = _half_size(base)
    radius = min(bx, by) * 0.15
    return CylinderFeature(radius=radius, height=bz * 2.6, name="Hole")


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------


def _full_size(feature) -> Tuple[float, float, float]:
    if isinstance(feature, BoxFeature):
        return feature.size.x, feature.size.y, feature.size.z
    if isinstance(feature, SphereFeature):
        return feature.radius * 2, feature.radius * 2, feature.radius * 2
    if isinstance(feature, CylinderFeature):
        if abs(feature.axis.z) > 0.9:
            return feature.radius * 2, feature.radius * 2, feature.height
        if abs(feature.axis.y) > 0.9:
            return feature.radius * 2, feature.height, feature.radius * 2
        return feature.height, feature.radius * 2, feature.radius * 2
    return 0.2, 0.2, 0.2


def _half_size(feature) -> Tuple[float, float, float]:
    fx, fy, fz = _full_size(feature)
    return fx / 2.0, fy / 2.0, fz / 2.0


def _clone_primitive(feature) -> object:
    if isinstance(feature, BoxFeature):
        return BoxFeature(size=feature.size, center=feature.center, name=feature.name)
    if isinstance(feature, SphereFeature):
        return SphereFeature(radius=feature.radius, center=feature.center, name=feature.name)
    if isinstance(feature, CylinderFeature):
        return CylinderFeature(
            radius=feature.radius,
            height=feature.height,
            center=feature.center,
            axis=feature.axis,
            name=feature.name,
        )
    return feature


def _place_relative(base: object, modifier: object, location: str, operation: str) -> object:
    """Return a copy of the modifier feature positioned against the base."""
    bx, by, bz = _half_size(base)
    mx, my, mz = _half_size(modifier)

    placed = _clone_primitive(modifier)
    if not isinstance(placed, (BoxFeature, SphereFeature, CylinderFeature)):
        return placed

    cx, cy, cz = base.center.x, base.center.y, base.center.z

    if operation == "subtract" or "hole" in location:
        # Center the cutting tool in the base by default.
        if isinstance(placed, CylinderFeature):
            # Make sure the cylinder pierces through the base.
            fx, fy, fz = _full_size(base)
            if abs(placed.axis.z) > 0.9:
                placed.height = fz * 2.5
            elif abs(placed.axis.y) > 0.9:
                placed.height = fy * 2.5
            else:
                placed.height = fx * 2.5
        placed.center = Point3(x=cx, y=cy, z=cz)
        return placed

    # Place against the requested face of the base.
    if location == "top":
        cz += bz + mz
    elif location == "bottom":
        cz -= bz + mz
    elif location == "front":
        cy += by + my
    elif location == "back":
        cy -= by + my
    elif location == "left":
        cx -= bx + mx
    elif location == "right":
        cx += bx + mx
    # "center" keeps the base center

    placed.center = Point3(x=cx, y=cy, z=cz)
    return placed


def _distribute(base: object, modifier: object, location: str, count: int) -> List[object]:
    """Place multiple copies of a modifier along the requested face."""
    if count <= 1:
        return [_place_relative(base, modifier, location, "union")]

    bx, by, _ = _half_size(base)
    placed = _place_relative(base, modifier, location, "union")
    results = []

    # Choose the span direction.  For top/bottom/front/back we span along X;
    # for left/right we span along Y.
    span_axis = "y" if location in ("left", "right") else "x"
    span = bx * 1.2 if span_axis == "x" else by * 1.2

    for i in range(count):
        t = (i - (count - 1) / 2.0) / max(1.0, count - 1)
        offset = t * span * 2.0
        copy = _clone_primitive(placed)
        if span_axis == "x":
            copy.center = Point3(x=placed.center.x + offset, y=placed.center.y, z=placed.center.z)
        else:
            copy.center = Point3(x=placed.center.x, y=placed.center.y + offset, z=placed.center.z)
        results.append(copy)
    return results


# ---------------------------------------------------------------------------
# Main parser
# ---------------------------------------------------------------------------


def parse_geometry_prompt(prompt: str) -> FeatureTree:
    """Convert a natural-language geometry prompt into a kernel FeatureTree."""
    prompt = prompt.strip()
    if not prompt:
        return FeatureTree(BoxFeature(name="Default Box"))

    # High-level shape shortcuts take precedence over generic clause parsing.
    chassis_tree = _parse_chassis(prompt)
    if chassis_tree is not None:
        return chassis_tree

    default_unit = _default_unit_for_text(prompt)

    # Split the prompt into a base clause and modifier clauses.
    split_pattern = re.compile(
        r"\s+(?=(?:with|and|plus|cut|subtract|minus|without|remove|intersect|drill)\s+)",
        re.IGNORECASE,
    )
    clauses = [c.strip() for c in split_pattern.split(prompt) if c.strip()]
    if not clauses:
        clauses = [prompt]

    base_clause = clauses[0]
    base = _parse_primitive(base_clause, default_unit)
    if base is None:
        dims = _parse_triplet(base_clause, default_unit)
        if dims:
            base = BoxFeature(size=Vector3(x=dims[0], y=dims[1], z=dims[2]), name="Box")
        else:
            base = BoxFeature(size=Vector3(x=0.1, y=0.05, z=0.03), name="Box")

    collected = [base]

    for clause in clauses[1:]:
        operation = _operation_from_clause(clause)
        location = _location_from_clause(clause)
        count = _word_count(clause)
        lower = clause.lower()

        modifier = _parse_primitive(clause, default_unit)

        # Implicit features: mounting bosses / pads / holes without explicit dims.
        if modifier is None:
            if any(w in lower for w in ["boss", "pad", "mount"]):
                modifier = _default_boss(base)
            elif "hole" in lower:
                modifier = _default_hole(base)
            else:
                continue

        # Convert spherical bosses/pads into cylinders for a more CAD-like shape.
        if isinstance(modifier, SphereFeature) and any(w in lower for w in ["boss", "pad", "mount"]):
            modifier = CylinderFeature(
                radius=modifier.radius,
                height=modifier.radius * 1.5,
                name="Boss",
            )

        if operation == "union" and count > 1:
            placed_modifiers = _distribute(base, modifier, location, count)
            collected.append(BooleanFeature(operation="union", children=placed_modifiers, name="Group"))
        else:
            placed = _place_relative(base, modifier, location, operation)
            collected.append(placed)

    # Build a left-associative boolean tree.
    if len(collected) == 1:
        return FeatureTree(collected[0])

    root = collected[0]
    for clause, mod in zip(clauses[1:], collected[1:]):
        operation = _operation_from_clause(clause)
        root = BooleanFeature(operation=operation, children=[root, mod])

    return FeatureTree(root)

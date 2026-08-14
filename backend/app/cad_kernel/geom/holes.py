"""The cutting tool a hole is, described once for both kernels.

A hole is not a cylinder. A counterbored one is a cylinder plus a wider
cylinder at the mouth; a countersunk one is a cylinder plus a cone; a drilled
blind one has a conical point below the shoulder. `HoleFeature` used to be a
single `CylinderSDF` on one side and a single `BRepPrimAPI_MakeCylinder` on the
other, which was fine while there was one shape to agree on.

There are now four, so the shapes are enumerated *here* and both
`evaluator/brep._hole` and `evaluator/sdf._sdf_hole` consume the same list. Two
transcriptions of this arithmetic would agree the day they were written and
disagree after the first edit, and the differential tests would then be
comparing two different parts.

Three decisions are load-bearing:

**`depth` is to the shoulder, never to the point.** A drill leaves a cone below
the full-diameter shoulder, and if `depth` measured to the tip then adding a
`tip_angle` to an existing feature would silently move the flat bottom of every
blind hole already stored. The tip is added *below* the stated depth.

**The mouth stays put when the hole goes through.** `through` lengthens the far
end only. The recess — counterbore or countersink — is measured from the mouth,
so extending both ends would move the recess relative to the material it is
supposed to sit in. `THROUGH_REACH` is a metre because that is longer than any
part this system quotes for; the feature has no access to the body, so the
operation layer is where a mouth gets placed against a real bounding box.

**A countersink is cut with the whole cone, not the frustum.** Below the point
where the cone narrows past the bore radius, the bore has already removed the
material, so the two are the same subtraction — and the full cone is
`ConeSDF`/`BRepPrimAPI_MakeCone` exactly as both kernels already build them,
where a frustum would be a fifth shape to keep in step.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, NamedTuple, Sequence, Tuple

import numpy as np

#: How far past the mouth a `through` hole reaches. Longer than any part in
#: `services/machines.py` can hold, and irrelevant to the SDF's grid bounds
#: because a subtraction is bounded by the body it cuts.
THROUGH_REACH = 1.0

#: Recognised recess shapes. `simple` is a plain bore, and is what every hole
#: stored before this module existed deserialises to.
HOLE_TYPES = ("simple", "counterbore", "countersink")


class Cyl(NamedTuple):
    radius: float
    height: float
    center: Tuple[float, float, float]
    axis: Tuple[float, float, float]


class Cone(NamedTuple):
    """Apex at +height/2 along `axis`, base radius at -height/2.

    The same convention `ConeSDF` and `evaluator.brep._cone` use, so neither
    consumer has to flip anything.
    """
    base_radius: float
    height: float
    center: Tuple[float, float, float]
    axis: Tuple[float, float, float]


def _unit(axis: Sequence[float]) -> np.ndarray:
    v = np.asarray([float(axis[0]), float(axis[1]), float(axis[2])], dtype=float)
    n = float(np.linalg.norm(v))
    if n < 1e-12:
        return np.array([0.0, 0.0, 1.0])
    return v / n


def _t(v: np.ndarray) -> Tuple[float, float, float]:
    return (float(v[0]), float(v[1]), float(v[2]))


def hole_tools(*, radius: float, depth: float,
               center: Sequence[float], axis: Sequence[float],
               hole_type: str = "simple",
               recess_radius: float = 0.0,
               recess_depth: float = 0.0,
               countersink_angle: float = 90.0,
               tip_angle: float = 0.0,
               through: bool = False) -> List[Any]:
    """The solids whose union is subtracted from the body.

    With every optional argument at its default this returns exactly one
    cylinder of `radius` and `depth` centred on `center` — which is what
    `HoleFeature` has always cut, so no stored document changes meaning.
    """
    radius = float(radius)
    depth = float(depth)
    n = _unit(axis)
    c = np.asarray([float(center[0]), float(center[1]), float(center[2])], dtype=float)

    mouth = c + n * (depth / 2.0)
    length = depth + THROUGH_REACH if through else depth
    tools: List[Any] = [Cyl(radius, length, _t(mouth - n * (length / 2.0)), _t(n))]

    if hole_type == "counterbore" and recess_radius > radius and recess_depth > 0:
        tools.append(Cyl(recess_radius, recess_depth,
                         _t(mouth - n * (recess_depth / 2.0)), _t(n)))

    elif hole_type == "countersink" and recess_radius > radius:
        # To the apex, not to the shoulder — see the module docstring.
        if recess_depth > 0:
            # Drafted as "8 dia x 3 deep": the stated depth is where the cone
            # meets the bore, so the apex is proportionally further on.
            height = recess_depth * recess_radius / (recess_radius - radius)
        else:
            half = math.radians(max(float(countersink_angle), 1.0) / 2.0)
            height = recess_radius / max(math.tan(half), 1e-9)
        tools.append(Cone(recess_radius, height, _t(mouth - n * (height / 2.0)), _t(-n)))

    if tip_angle > 0 and not through:
        # Below the *stated* depth, so `depth` keeps meaning the shoulder.
        shoulder = c - n * (depth / 2.0)
        half = math.radians(min(max(float(tip_angle), 1.0), 179.0) / 2.0)
        height = radius / max(math.tan(half), 1e-9)
        tools.append(Cone(radius, height, _t(shoulder - n * (height / 2.0)), _t(-n)))

    return tools


def tools_for(feature) -> List[Any]:
    """`hole_tools` read off a `HoleFeature`."""
    return hole_tools(
        radius=feature.radius, depth=feature.depth,
        center=(feature.center.x, feature.center.y, feature.center.z),
        axis=(feature.axis.x, feature.axis.y, feature.axis.z),
        hole_type=getattr(feature, "hole_type", "simple"),
        recess_radius=getattr(feature, "recess_radius", 0.0),
        recess_depth=getattr(feature, "recess_depth", 0.0),
        countersink_angle=getattr(feature, "countersink_angle", 90.0),
        tip_angle=getattr(feature, "tip_angle", 0.0),
        through=bool(getattr(feature, "through", False)))


#: ISO 261 coarse pitches, M2–M12 — the same range `parts_catalog.METRIC`
#: covers. Kept here rather than imported because this module is kernel-side
#: geometry and `services/` may not be imported from it.
_COARSE_PITCH: Dict[str, float] = {
    "M2": 0.40, "M2.5": 0.45, "M3": 0.50, "M4": 0.70, "M5": 0.80,
    "M6": 1.00, "M8": 1.25, "M10": 1.50, "M12": 1.75,
}


def tap_drill_radius(designation: str) -> float:
    """The bore a thread is cut into, in metres.

    ISO 2306's rule for a nominal 100% thread is `D - P`, which is what every
    tap chart lists for coarse metric and what a shop will drill. Returned as a
    radius because that is what the kernel takes.
    """
    key = str(designation).strip().upper()
    if key not in _COARSE_PITCH:
        raise KeyError(designation)
    nominal = float(key[1:])
    return (nominal - _COARSE_PITCH[key]) / 2000.0

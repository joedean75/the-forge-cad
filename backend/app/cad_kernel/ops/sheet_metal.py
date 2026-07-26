"""Sheet metal features: base flange, edge flanges, and flat-pattern unfold.

Sheet metal is modelled the way a brake actually forms it, not as a generic
solid: a flat base flange of constant thickness, plus edge flanges each joined
by a circular bend of a given inside radius and angle. Keeping that structure
explicit is what makes the flat pattern computable — the developed length needs
the bend geometry, which is unrecoverable from an arbitrary solid.

Bend allowance uses the K-factor convention::

    BA = angle_rad * (inside_radius + K * thickness)

K is the fraction of the thickness at which the neutral axis sits (~0.44 for
soft aluminium and mild steel air bends; 0.33 for harder tempers). The developed
length of a part is the sum of its flat segments plus one allowance per bend,
which is what a press brake operator needs to cut the blank.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List

from app.cad_kernel.core.feature_tree import Feature
from app.cad_kernel.core.topology import Solid

# Edges of the base flange a flange can be attached to. The letter is the axis
# the edge runs perpendicular to; the sign is which end of that axis.
EDGES = ("x+", "x-", "y+", "y-")

DEFAULT_K_FACTOR = 0.44


def bend_allowance(angle_deg: float, radius: float, thickness: float, k_factor: float) -> float:
    """Arc length of the neutral axis through a bend (metres)."""
    return math.radians(abs(angle_deg)) * (radius + k_factor * thickness)


def bend_deduction(angle_deg: float, radius: float, thickness: float, k_factor: float) -> float:
    """How much shorter the blank is than the sum of the outside dimensions.

    Positive for the usual case; this is the number stamped on shop travellers.
    """
    angle = math.radians(abs(angle_deg))
    outside_setback = (radius + thickness) * math.tan(min(angle, math.pi - 1e-9) / 2.0)
    return 2.0 * outside_setback - bend_allowance(angle_deg, radius, thickness, k_factor)


def _normalize_flange(f: Dict[str, Any], default_radius: float) -> Dict[str, Any]:
    edge = str(f.get("edge", "x+"))
    if edge not in EDGES:
        raise ValueError(f"flange edge must be one of {EDGES}, got {edge!r}")
    length = float(f.get("length", 0.02))
    if length <= 0:
        raise ValueError("flange length must be positive")
    angle = float(f.get("angle", 90.0))
    if not -180.0 < angle < 180.0 or angle == 0.0:
        raise ValueError("flange angle must be a non-zero angle in (-180, 180) degrees")
    radius = float(f.get("radius", default_radius))
    if radius < 0:
        raise ValueError("bend radius cannot be negative")
    return {"edge": edge, "length": length, "angle": angle, "radius": radius}


@dataclass
class SheetMetalFeature(Feature):
    """A base flange plus edge flanges, all at one constant thickness."""

    thickness: float = 0.0015
    length: float = 0.10          # base flange extent along X
    width: float = 0.06           # base flange extent along Y
    bend_radius: float = 0.002    # default inside radius for flanges
    k_factor: float = DEFAULT_K_FACTOR
    flanges: List[Dict[str, Any]] = field(default_factory=list)

    def normalized_flanges(self) -> List[Dict[str, Any]]:
        return [_normalize_flange(f, self.bend_radius) for f in self.flanges]

    def evaluate(self) -> Solid:
        from app.cad_kernel.evaluator.sdf import evaluate_sdf, sdf_from_feature

        return evaluate_sdf(sdf_from_feature(self))

    def flat_pattern(self) -> Dict[str, Any]:
        """Developed blank size and bend lines for this part.

        The blank is the base flange grown by each flange's developed length
        (its flat leg plus the bend allowance). Bend lines are reported as a
        distance from the blank edge, which is how they are set up on a brake.
        """
        t, k = self.thickness, self.k_factor
        spans = {"x": [0.0, self.length], "y": [0.0, self.width]}
        bends: List[Dict[str, Any]] = []

        for f in self.normalized_flanges():
            axis, sign = f["edge"][0], f["edge"][1]
            ba = bend_allowance(f["angle"], f["radius"], t, k)
            # Flat leg measured from the end of the bend, so the developed
            # addition is the allowance plus the remaining straight length.
            developed = ba + f["length"]
            if sign == "+":
                spans[axis][1] += developed
            else:
                spans[axis][0] -= developed
            bends.append({
                "edge": f["edge"],
                "angle": f["angle"],
                "radius": f["radius"],
                "bend_allowance": round(ba, 6),
                "bend_deduction": round(bend_deduction(f["angle"], f["radius"], t, k), 6),
                "developed_length": round(developed, 6),
            })

        blank_x = spans["x"][1] - spans["x"][0]
        blank_y = spans["y"][1] - spans["y"][0]

        # Bend-line offsets from the blank's min edge on each axis.
        for b, f in zip(bends, self.normalized_flanges()):
            axis, sign = f["edge"][0], f["edge"][1]
            origin = spans[axis][0]
            base_edge = self.length if axis == "x" else self.width
            pos = (base_edge - origin) if sign == "+" else (0.0 - origin)
            b["distance_from_edge"] = round(pos, 6)

        return {
            "thickness": t,
            "k_factor": k,
            "blank_size": {"x": round(blank_x, 6), "y": round(blank_y, 6)},
            "blank_area": round(blank_x * blank_y, 9),
            "bends": bends,
            "bend_count": len(bends),
        }

    def _serialize_params(self) -> Dict[str, Any]:
        return {
            "thickness": self.thickness,
            "length": self.length,
            "width": self.width,
            "bend_radius": self.bend_radius,
            "k_factor": self.k_factor,
            "flanges": [dict(f) for f in self.flanges],
        }

    @classmethod
    def _from_dict(cls, data: Dict[str, Any]) -> "SheetMetalFeature":
        p = data["params"]
        return cls(
            id=data.get("id"),
            name=data.get("name", "Sheet metal"),
            thickness=p.get("thickness", 0.0015),
            length=p.get("length", 0.10),
            width=p.get("width", 0.06),
            bend_radius=p.get("bend_radius", 0.002),
            k_factor=p.get("k_factor", DEFAULT_K_FACTOR),
            flanges=[dict(f) for f in p.get("flanges", [])],
        )

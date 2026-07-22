"""Sketch-based features (Horizon 1 sketch MVP).

An ExtrudeFeature is a 2D profile (rect/circle/polygon) on a base plane (xy/xz/yz)
extruded to a depth, evaluated as an SDF. It composes with booleans and modifiers
like any other feature — the first step toward real sketch-based modeling.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict

from app.cad_kernel.core.feature_tree import Feature
from app.cad_kernel.core.topology import Solid


def _default_profile() -> Dict[str, Any]:
    return {"type": "rect", "width": 0.05, "height": 0.03, "cx": 0.0, "cy": 0.0}


@dataclass
class ExtrudeFeature(Feature):
    profile: Dict[str, Any] = field(default_factory=_default_profile)
    plane: str = "xy"        # xy | xz | yz
    offset: float = 0.0      # position along the plane normal
    depth: float = 0.02
    # In-plane (u, v) origin — the face centre when sketched on a face, so the
    # profile is centered on the face rather than the world origin.
    plane_origin: Any = field(default_factory=lambda: [0.0, 0.0])

    def evaluate(self) -> Solid:
        from app.cad_kernel.evaluator.sdf import evaluate_sdf, sdf_from_feature

        return evaluate_sdf(sdf_from_feature(self))

    def _serialize_params(self) -> Dict[str, Any]:
        return {"profile": self.profile, "plane": self.plane, "offset": self.offset,
                "depth": self.depth, "plane_origin": self.plane_origin}

    @classmethod
    def _from_dict(cls, data: Dict[str, Any]) -> "ExtrudeFeature":
        p = data["params"]
        return cls(
            id=data.get("id"),
            name=data.get("name", "Extrude"),
            profile=p.get("profile") or _default_profile(),
            plane=p.get("plane", "xy"),
            offset=p.get("offset", 0.0),
            depth=p.get("depth", 0.02),
            plane_origin=p.get("plane_origin") or [0.0, 0.0],
        )

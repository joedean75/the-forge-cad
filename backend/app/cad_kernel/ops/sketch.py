"""Sketch-based features.

An ExtrudeFeature is a 2D profile on a base plane (xy/xz/yz) extruded to a depth.
The profile is a `rect`/`circle`/`polygon`, or the `loops` form a constrained
sketch solves to (see `cad_kernel.geom.profile2d`).

A **SketchFeature** is the sketch itself, as a thing rather than an argument. It
holds the constrained spec, the profile it solves to, and where it lives, and it
produces *no body of its own* — it is geometry other features consume, so several
features can share one outline and editing it moves all of them.

Because it has no body, a SketchFeature never reaches an evaluator: `feature_ops.
resolve_sketches` substitutes its profile into whatever references it and takes
it out of the tree, the same way `resolve_suppressed` prunes a suppressed node.
The SDF builder below exists to say so if that ever fails to happen, rather than
to produce a plausible empty solid.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict

from app.cad_kernel.core.feature_tree import Feature, register_feature
from app.cad_kernel.core.topology import Solid


def _default_profile() -> Dict[str, Any]:
    return {"type": "rect", "width": 0.05, "height": 0.03, "cx": 0.0, "cy": 0.0}


@register_feature
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


class SketchNotConsumed(ValueError):
    """A SketchFeature reached an evaluator, which means it was not substituted."""


@register_feature
@dataclass
class SketchFeature(Feature):
    """A constrained 2D sketch other features consume. Contributes no geometry."""

    sketch: Dict[str, Any] = field(default_factory=dict)     # the editable spec
    profile: Dict[str, Any] = field(default_factory=dict)    # what it solved to
    plane: str = "xy"
    offset: float = 0.0
    plane_origin: Any = field(default_factory=lambda: [0.0, 0.0])
    on_face: Any = None
    solved: Dict[str, Any] = field(default_factory=dict)     # status / dof / residual

    def evaluate(self) -> Solid:
        raise SketchNotConsumed(
            f"sketch {self.id} has no body of its own — it is geometry for another "
            "feature to consume (add_extrude/add_revolve/add_loft with sketch=...)")

    def _serialize_params(self) -> Dict[str, Any]:
        return {"sketch": self.sketch, "profile": self.profile, "plane": self.plane,
                "offset": self.offset, "plane_origin": self.plane_origin,
                "on_face": self.on_face, "solved": self.solved}

    @classmethod
    def _from_dict(cls, data: Dict[str, Any]) -> "SketchFeature":
        p = data["params"]
        return cls(
            id=data.get("id"),
            name=data.get("name", "Sketch"),
            sketch=p.get("sketch") or {},
            profile=p.get("profile") or {},
            plane=p.get("plane", "xy"),
            offset=p.get("offset", 0.0),
            plane_origin=p.get("plane_origin") or [0.0, 0.0],
            on_face=p.get("on_face"),
            solved=p.get("solved") or {},
        )

"""Advanced modeling features: revolve, mirror, sweep, loft.

These fill the standard modeling ops a CAD suite needs beyond extrude/boolean/
modifier — turned parts (revolve), symmetry (mirror), pipes/tubing (sweep along
a path), and blended transitions (loft). All are SDF-backed and compose with
everything else in the tree.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal

from app.cad_kernel.core.feature_tree import Feature
from app.cad_kernel.core.topology import Solid
from app.cad_kernel.core.types import Point3, Vector3


def _default_profile() -> Dict[str, Any]:
    return {"type": "rect", "width": 0.04, "height": 0.03, "cx": 0.02, "cy": 0.0}


def _vd(v) -> Dict[str, float]:
    return {"x": v.x, "y": v.y, "z": v.z}


def _vec(d: Dict[str, Any]) -> Vector3:
    return Vector3(d.get("x", 0.0), d.get("y", 0.0), d.get("z", 0.0))


def _pt(d: Dict[str, Any]) -> Point3:
    return Point3(d.get("x", 0.0), d.get("y", 0.0), d.get("z", 0.0))


def _evaluate_via_sdf(feature: Feature) -> Solid:
    from app.cad_kernel.evaluator.sdf import evaluate_sdf, sdf_from_feature

    return evaluate_sdf(sdf_from_feature(feature))


def _children_from(data: Dict[str, Any]) -> List[Feature]:
    return [Feature.from_dict(c) for c in data["params"].get("children", [])]


@dataclass
class RevolveFeature(Feature):
    """A 2D profile revolved about an axis — the standard way to make turned
    parts (shafts, wheels, pulleys, bottles). The profile is authored in the
    (radius, height) half-plane; ``angle`` < 360 gives a partial revolve."""

    profile: Dict[str, Any] = field(default_factory=_default_profile)
    axis: Literal["x", "y", "z"] = "z"
    center: Point3 = field(default_factory=Point3)
    angle: float = 360.0  # degrees

    def evaluate(self) -> Solid:
        return _evaluate_via_sdf(self)

    def _serialize_params(self) -> Dict[str, Any]:
        return {"profile": self.profile, "axis": self.axis, "center": _vd(self.center), "angle": self.angle}

    @classmethod
    def _from_dict(cls, data: Dict[str, Any]) -> "RevolveFeature":
        p = data["params"]
        return cls(id=data.get("id"), name=data.get("name", "Revolve"),
                   profile=p.get("profile") or _default_profile(),
                   axis=p.get("axis", "z"), center=_pt(p.get("center", {})),
                   angle=float(p.get("angle", 360.0)))


@dataclass
class MirrorFeature(Feature):
    """Reflect the child across a plane and union the two — symmetry in one op."""

    plane: Literal["xy", "xz", "yz"] = "yz"
    offset: float = 0.0  # plane position along its normal (metres)
    children: List[Feature] = field(default_factory=list)

    def evaluate(self) -> Solid:
        return _evaluate_via_sdf(self)

    def _serialize_params(self) -> Dict[str, Any]:
        return {"plane": self.plane, "offset": self.offset, "children": [c.to_dict() for c in self.children]}

    @classmethod
    def _from_dict(cls, data: Dict[str, Any]) -> "MirrorFeature":
        p = data["params"]
        return cls(id=data.get("id"), name=data.get("name", "Mirror"),
                   plane=p.get("plane", "yz"), offset=float(p.get("offset", 0.0)),
                   children=_children_from(data))


@dataclass
class SweepFeature(Feature):
    """A circular profile swept along a polyline path — pipes, tubing, wire,
    handrails. ``path`` is a list of points (metres); ``radius`` the tube radius."""

    path: List[List[float]] = field(default_factory=lambda: [[0, 0, 0], [0, 0, 0.05]])
    radius: float = 0.005

    def evaluate(self) -> Solid:
        return _evaluate_via_sdf(self)

    def _serialize_params(self) -> Dict[str, Any]:
        return {"path": self.path, "radius": self.radius}

    @classmethod
    def _from_dict(cls, data: Dict[str, Any]) -> "SweepFeature":
        p = data["params"]
        return cls(id=data.get("id"), name=data.get("name", "Sweep"),
                   path=[list(map(float, pt)) for pt in p.get("path", [[0, 0, 0], [0, 0, 0.05]])],
                   radius=float(p.get("radius", 0.005)))


@dataclass
class LoftFeature(Feature):
    """Blend between two profiles at two heights along a plane's normal — the
    standard transition op (round-to-square adapters, tapers, funnels)."""

    profile_a: Dict[str, Any] = field(default_factory=_default_profile)
    profile_b: Dict[str, Any] = field(default_factory=lambda: {"type": "circle", "radius": 0.02, "cx": 0.0, "cy": 0.0})
    plane: Literal["xy", "xz", "yz"] = "xy"
    offset_a: float = 0.0
    offset_b: float = 0.05

    def evaluate(self) -> Solid:
        return _evaluate_via_sdf(self)

    def _serialize_params(self) -> Dict[str, Any]:
        return {"profile_a": self.profile_a, "profile_b": self.profile_b,
                "plane": self.plane, "offset_a": self.offset_a, "offset_b": self.offset_b}

    @classmethod
    def _from_dict(cls, data: Dict[str, Any]) -> "LoftFeature":
        p = data["params"]
        return cls(id=data.get("id"), name=data.get("name", "Loft"),
                   profile_a=p.get("profile_a") or _default_profile(),
                   profile_b=p.get("profile_b") or {"type": "circle", "radius": 0.02},
                   plane=p.get("plane", "xy"),
                   offset_a=float(p.get("offset_a", 0.0)), offset_b=float(p.get("offset_b", 0.05)))

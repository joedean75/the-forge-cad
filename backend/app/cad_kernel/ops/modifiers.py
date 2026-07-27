"""Standard CAD modifier features (Phase 2).

All are SDF-backed and store their operand(s) under ``children`` (a list) so the
frontend can walk them uniformly. Evaluation flows through the SDF evaluator,
which is the kernel's primary path; ``evaluate()`` is provided for parity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal

from app.cad_kernel.core.feature_tree import Feature, register_feature
from app.cad_kernel.core.topology import Solid
from app.cad_kernel.core.types import Point3, Vector3


def _vec(d: Dict[str, Any]) -> Vector3:
    return Vector3(d["x"], d["y"], d["z"])


def _pt(d: Dict[str, Any]) -> Point3:
    return Point3(d["x"], d["y"], d["z"])


def _vd(v) -> Dict[str, float]:
    return {"x": v.x, "y": v.y, "z": v.z}


def _children_from(data: Dict[str, Any]) -> List[Feature]:
    return [Feature.from_dict(c) for c in data["params"].get("children", [])]


def _evaluate_via_sdf(feature: Feature) -> Solid:
    from app.cad_kernel.evaluator.sdf import evaluate_sdf, sdf_from_feature

    return evaluate_sdf(sdf_from_feature(feature))


@register_feature
@dataclass
class FilletFeature(Feature):
    """Smooth (rounded) boolean blend of its children."""

    radius: float = 0.01
    operation: Literal["union", "subtract", "intersect"] = "union"
    children: List[Feature] = field(default_factory=list)

    def evaluate(self) -> Solid:
        return _evaluate_via_sdf(self)

    def _serialize_params(self) -> Dict[str, Any]:
        return {
            "radius": self.radius,
            "operation": self.operation,
            "children": [c.to_dict() for c in self.children],
        }

    @classmethod
    def _from_dict(cls, data: Dict[str, Any]) -> "FilletFeature":
        p = data["params"]
        return cls(
            id=data.get("id"),
            name=data.get("name", "Fillet"),
            radius=p.get("radius", 0.01),
            operation=p.get("operation", "union"),
            children=_children_from(data),
        )


@register_feature
@dataclass
class ChamferFeature(Feature):
    """Chamfered boolean blend of its children."""

    radius: float = 0.01
    operation: Literal["union", "subtract", "intersect"] = "union"
    children: List[Feature] = field(default_factory=list)

    def evaluate(self) -> Solid:
        return _evaluate_via_sdf(self)

    def _serialize_params(self) -> Dict[str, Any]:
        return {
            "radius": self.radius,
            "operation": self.operation,
            "children": [c.to_dict() for c in self.children],
        }

    @classmethod
    def _from_dict(cls, data: Dict[str, Any]) -> "ChamferFeature":
        p = data["params"]
        return cls(
            id=data.get("id"),
            name=data.get("name", "Chamfer"),
            radius=p.get("radius", 0.01),
            operation=p.get("operation", "union"),
            children=_children_from(data),
        )


@register_feature
@dataclass
class ShellFeature(Feature):
    """Hollow the single child to a wall of ``thickness``."""

    thickness: float = 0.004
    children: List[Feature] = field(default_factory=list)

    def evaluate(self) -> Solid:
        return _evaluate_via_sdf(self)

    def _serialize_params(self) -> Dict[str, Any]:
        return {"thickness": self.thickness, "children": [c.to_dict() for c in self.children]}

    @classmethod
    def _from_dict(cls, data: Dict[str, Any]) -> "ShellFeature":
        p = data["params"]
        return cls(
            id=data.get("id"),
            name=data.get("name", "Shell"),
            thickness=p.get("thickness", 0.004),
            children=_children_from(data),
        )


@register_feature
@dataclass
class HoleFeature(Feature):
    """Subtract a cylindrical hole from the single child."""

    radius: float = 0.005
    depth: float = 0.1
    center: Point3 = field(default_factory=Point3)
    axis: Vector3 = field(default_factory=lambda: Vector3(0.0, 0.0, 1.0))
    children: List[Feature] = field(default_factory=list)

    def evaluate(self) -> Solid:
        return _evaluate_via_sdf(self)

    def _serialize_params(self) -> Dict[str, Any]:
        return {
            "radius": self.radius,
            "depth": self.depth,
            "center": _vd(self.center),
            "axis": _vd(self.axis),
            "children": [c.to_dict() for c in self.children],
        }

    @classmethod
    def _from_dict(cls, data: Dict[str, Any]) -> "HoleFeature":
        p = data["params"]
        return cls(
            id=data.get("id"),
            name=data.get("name", "Hole"),
            radius=p.get("radius", 0.005),
            depth=p.get("depth", 0.1),
            center=_pt(p.get("center", {"x": 0, "y": 0, "z": 0})),
            axis=_vec(p.get("axis", {"x": 0, "y": 0, "z": 1})),
            children=_children_from(data),
        )


@register_feature
@dataclass
class TransformFeature(Feature):
    """Rigid transform + uniform scale of the single child."""

    translate: Vector3 = field(default_factory=Vector3)
    rotate: Vector3 = field(default_factory=Vector3)  # XYZ euler degrees
    scale: float = 1.0
    children: List[Feature] = field(default_factory=list)

    def evaluate(self) -> Solid:
        return _evaluate_via_sdf(self)

    def _serialize_params(self) -> Dict[str, Any]:
        return {
            "translate": _vd(self.translate),
            "rotate": _vd(self.rotate),
            "scale": self.scale,
            "children": [c.to_dict() for c in self.children],
        }

    @classmethod
    def _from_dict(cls, data: Dict[str, Any]) -> "TransformFeature":
        p = data["params"]
        return cls(
            id=data.get("id"),
            name=data.get("name", "Transform"),
            translate=_vec(p.get("translate", {"x": 0, "y": 0, "z": 0})),
            rotate=_vec(p.get("rotate", {"x": 0, "y": 0, "z": 0})),
            scale=p.get("scale", 1.0),
            children=_children_from(data),
        )


@register_feature
@dataclass
class PatternFeature(Feature):
    """Linear or circular pattern (union of copies) of the single child."""

    kind: Literal["linear", "circular"] = "linear"
    count: int = 3
    spacing: float = 0.05  # metres (linear)
    axis: Vector3 = field(default_factory=lambda: Vector3(1.0, 0.0, 0.0))
    center: Point3 = field(default_factory=Point3)  # circular pivot
    children: List[Feature] = field(default_factory=list)

    def evaluate(self) -> Solid:
        return _evaluate_via_sdf(self)

    def _serialize_params(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "count": self.count,
            "spacing": self.spacing,
            "axis": _vd(self.axis),
            "center": _vd(self.center),
            "children": [c.to_dict() for c in self.children],
        }

    @classmethod
    def _from_dict(cls, data: Dict[str, Any]) -> "PatternFeature":
        p = data["params"]
        return cls(
            id=data.get("id"),
            name=data.get("name", "Pattern"),
            kind=p.get("kind", "linear"),
            count=int(p.get("count", 3)),
            spacing=p.get("spacing", 0.05),
            axis=_vec(p.get("axis", {"x": 1, "y": 0, "z": 0})),
            center=_pt(p.get("center", {"x": 0, "y": 0, "z": 0})),
            children=_children_from(data),
        )

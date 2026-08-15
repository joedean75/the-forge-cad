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
    """Subtract a machined hole from the single child.

    A plain bore with every option at its default — which is what this was, and
    what every stored document still deserialises to. The rest is the shape a
    real hole has: a counterbore or countersink at the mouth, a conical drill
    point below the shoulder, and a thread designation for a tapped one.

    `depth` measures to the **shoulder**, not to the drill point, so adding a
    `tip_angle` to an existing feature deepens the hole rather than silently
    moving its flat bottom up. `through` lengthens the far end only, leaving the
    mouth — and therefore the recess — where the caller put it.

    `thread` is a callout, not geometry. A tapped hole is drilled at the tap
    drill diameter and the thread is cut afterwards; modelling the helix is
    `ThreadFeature`'s job, and a hole that quietly grew a 60° helical cut would
    be a different body from the one every drawing and DFM figure describes.
    The `add_hole` operation is what turns `tapped: "M6"` into the right
    `radius` — this feature holds the number, not the lookup.
    """

    radius: float = 0.005
    depth: float = 0.1
    center: Point3 = field(default_factory=Point3)
    axis: Vector3 = field(default_factory=lambda: Vector3(0.0, 0.0, 1.0))
    hole_type: Literal["simple", "counterbore", "countersink"] = "simple"
    recess_radius: float = 0.0        # counterbore / countersink, at the mouth
    recess_depth: float = 0.0         # counterbore depth; countersink override
    countersink_angle: float = 90.0   # included angle, degrees
    tip_angle: float = 0.0            # 0 = flat bottom; 118 is a jobber drill
    through: bool = False
    thread: str = ""                  # e.g. "M6" — a callout, not a helix
    children: List[Feature] = field(default_factory=list)

    def evaluate(self) -> Solid:
        return _evaluate_via_sdf(self)

    def _serialize_params(self) -> Dict[str, Any]:
        return {
            "radius": self.radius,
            "depth": self.depth,
            "center": _vd(self.center),
            "axis": _vd(self.axis),
            "hole_type": self.hole_type,
            "recess_radius": self.recess_radius,
            "recess_depth": self.recess_depth,
            "countersink_angle": self.countersink_angle,
            "tip_angle": self.tip_angle,
            "through": self.through,
            "thread": self.thread,
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
            hole_type=p.get("hole_type", "simple"),
            recess_radius=float(p.get("recess_radius", 0.0) or 0.0),
            recess_depth=float(p.get("recess_depth", 0.0) or 0.0),
            countersink_angle=float(p.get("countersink_angle", 90.0) or 90.0),
            tip_angle=float(p.get("tip_angle", 0.0) or 0.0),
            through=bool(p.get("through", False)),
            thread=str(p.get("thread", "") or ""),
            children=_children_from(data),
        )


@register_feature
@dataclass
class RibFeature(Feature):
    """A stiffening rib fused onto the single child.

    Until now the only way to model one was to union a thin box in by hand,
    which works right up until the wall thickness changes: a box does not know
    it is a rib, so nothing holds its tip thickness, its height and its draft in
    the relationship a moulder needs. This carries all four.

    A **dimensioned** rib, deliberately — not an "up to next" one. Growing until
    the rib meets material needs a face reference, and the SDF kernel has none,
    so such a rib would exist on one kernel and not the other. Height is a
    parameter; everything else is a real rib.

    The solid is a trapezoidal prism (`geom/ribs.py`), which both kernels build
    from the same four corner points.
    """

    start: Point3 = field(default_factory=Point3)
    end: Point3 = field(default_factory=lambda: Point3(0.05, 0.0, 0.0))
    up: Vector3 = field(default_factory=lambda: Vector3(0.0, 0.0, 1.0))
    thickness: float = 0.002          # at the tip, which is what a tool is cut to
    height: float = 0.01
    draft: float = 0.0                # degrees per side, opening the base out
    children: List[Feature] = field(default_factory=list)

    def evaluate(self) -> Solid:
        return _evaluate_via_sdf(self)

    def _serialize_params(self) -> Dict[str, Any]:
        return {
            "start": _vd(self.start),
            "end": _vd(self.end),
            "up": _vd(self.up),
            "thickness": self.thickness,
            "height": self.height,
            "draft": self.draft,
            "children": [c.to_dict() for c in self.children],
        }

    @classmethod
    def _from_dict(cls, data: Dict[str, Any]) -> "RibFeature":
        p = data["params"]
        return cls(
            id=data.get("id"),
            name=data.get("name", "Rib"),
            start=_pt(p.get("start", {"x": 0, "y": 0, "z": 0})),
            end=_pt(p.get("end", {"x": 0.05, "y": 0, "z": 0})),
            up=_vec(p.get("up", {"x": 0, "y": 0, "z": 1})),
            thickness=float(p.get("thickness", 0.002)),
            height=float(p.get("height", 0.01)),
            draft=float(p.get("draft", 0.0) or 0.0),
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

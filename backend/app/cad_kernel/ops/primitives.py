"""Primitive solid constructors and their feature-tree wrappers."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Dict

import numpy as np

from app.cad_kernel.core.feature_tree import Feature, register_feature
from app.cad_kernel.core.topology import Face, FaceMesh, Shell, Solid
from app.cad_kernel.core.types import Point3, Vector3
from app.cad_kernel.geom.surfaces import (
    PlaneSurface,
    CylinderSurface,
    SphereSurface,
    ConeSurface,
)


def _make_circle_mesh(center: Point3, axis: Vector3, radius: float, segments: int = 32) -> FaceMesh:
    """Return a filled circle (disk) as a fan triangulation."""
    import math

    if abs(abs(axis.z) - 1.0) < 1e-6:
        ref = Vector3(1.0, 0.0, 0.0)
    else:
        ref = Vector3(0.0, 0.0, 1.0)
    u = axis.cross(ref).normalized()
    v = axis.cross(u).normalized()
    center_arr = center.to_array()
    verts = [center_arr]
    for i in range(segments):
        angle = 2.0 * math.pi * i / segments
        p = center_arr + (u.to_array() * math.cos(angle) + v.to_array() * math.sin(angle)) * radius
        verts.append(p)
    faces = [[0, i + 1, ((i + 1) % segments) + 1] for i in range(segments)]
    return FaceMesh(np.array(verts, dtype=float), np.array(faces, dtype=int))


def make_box(size: Vector3, center: Point3 = Point3()) -> Solid:
    """Axis-aligned box."""
    import numpy as np

    hx, hy, hz = size.x / 2.0, size.y / 2.0, size.z / 2.0
    cx, cy, cz = center.x, center.y, center.z
    # 8 corners
    p = [
        Point3(cx - hx, cy - hy, cz - hz),  # 0
        Point3(cx + hx, cy - hy, cz - hz),  # 1
        Point3(cx + hx, cy + hy, cz - hz),  # 2
        Point3(cx - hx, cy + hy, cz - hz),  # 3
        Point3(cx - hx, cy - hy, cz + hz),  # 4
        Point3(cx + hx, cy - hy, cz + hz),  # 5
        Point3(cx + hx, cy + hy, cz + hz),  # 6
        Point3(cx - hx, cy + hy, cz + hz),  # 7
    ]
    # Faces ordered with CCW corners when viewed from outside.
    face_corners = [
        [p[0], p[1], p[2], p[3]],  # bottom, normal -z
        [p[4], p[7], p[6], p[5]],  # top, normal +z
        [p[0], p[4], p[5], p[1]],  # front, normal -y
        [p[2], p[6], p[7], p[3]],  # back, normal +y
        [p[1], p[5], p[6], p[2]],  # right, normal +x
        [p[0], p[3], p[7], p[4]],  # left, normal -x
    ]
    normals = [
        Vector3(0.0, 0.0, -1.0),
        Vector3(0.0, 0.0, 1.0),
        Vector3(0.0, -1.0, 0.0),
        Vector3(0.0, 1.0, 0.0),
        Vector3(1.0, 0.0, 0.0),
        Vector3(-1.0, 0.0, 0.0),
    ]
    faces = []
    for corners, normal in zip(face_corners, normals):
        surf = PlaneSurface(corners)
        # Reorder if PlaneSurface computed normal disagrees with expected
        if surf.normal.dot(normal) < 0:
            corners = list(reversed(corners))
            surf = PlaneSurface(corners)
        faces.append(Face(surface=surf, mesh=surf.tessellate(), id=str(uuid.uuid4())))
    return Solid(Shell(faces), id=str(uuid.uuid4()))


def make_sphere(radius: float, center: Point3 = Point3(), segments: int = 64, stacks: int = 32) -> Solid:
    surf = SphereSurface(center, radius)
    return Solid(Shell([Face(surface=surf, mesh=surf.tessellate(segments=segments, stacks=stacks), id=str(uuid.uuid4()))]), id=str(uuid.uuid4()))


def make_cylinder(radius: float, height: float, center: Point3 = Point3(), axis: Vector3 = Vector3(0.0, 0.0, 1.0), segments: int = 64) -> Solid:
    axis = axis.normalized()
    bottom = center - axis * (height / 2.0)
    top = center + axis * (height / 2.0)
    side = CylinderSurface(bottom, axis, radius, height)
    bottom_face = Face(surface=PlaneSurface([bottom, bottom + _perp(axis, 0), bottom + _perp(axis, 1)]), mesh=_make_circle_mesh(bottom, -axis, radius, segments), id=str(uuid.uuid4()))
    top_face = Face(surface=PlaneSurface([top, top + _perp(axis, 1), top + _perp(axis, 0)]), mesh=_make_circle_mesh(top, axis, radius, segments), id=str(uuid.uuid4()))
    side_face = Face(surface=side, mesh=side.tessellate(segments=segments, stacks=1), id=str(uuid.uuid4()))
    return Solid(Shell([side_face, bottom_face, top_face]), id=str(uuid.uuid4()))


def make_cone(base_radius: float, height: float, center: Point3 = Point3(), axis: Vector3 = Vector3(0.0, 0.0, 1.0), segments: int = 64) -> Solid:
    axis = axis.normalized()
    base_center = center + axis * (height / 2.0)
    apex = center - axis * (height / 2.0)
    side = ConeSurface(apex, axis, base_radius, height)
    base_face = Face(surface=PlaneSurface([base_center, base_center + _perp(axis, 1), base_center + _perp(axis, 0)]), mesh=_make_circle_mesh(base_center, axis, base_radius, segments), id=str(uuid.uuid4()))
    side_face = Face(surface=side, mesh=side.tessellate(segments=segments, stacks=1), id=str(uuid.uuid4()))
    return Solid(Shell([side_face, base_face]), id=str(uuid.uuid4()))


def _perp(axis: Vector3, index: int) -> Vector3:
    """Return a vector perpendicular to axis.  Two calls with index 0/1 return orthonormal vectors."""
    if abs(abs(axis.z) - 1.0) < 1e-6:
        u = Vector3(1.0, 0.0, 0.0) if index == 0 else Vector3(0.0, 1.0, 0.0)
    else:
        u = Vector3(0.0, 0.0, 1.0)
    perp = axis.cross(u).normalized()
    if index == 1:
        perp = axis.cross(perp).normalized()
    return perp


# ---------------------------------------------------------------------------
# Feature wrappers
# ---------------------------------------------------------------------------


@register_feature
@dataclass
class BoxFeature(Feature):
    size: Vector3 = Vector3(1.0, 1.0, 1.0)
    center: Point3 = Point3()

    def evaluate(self) -> Solid:
        return make_box(self.size, self.center)

    def _serialize_params(self) -> Dict[str, Any]:
        return {
            "size": {"x": self.size.x, "y": self.size.y, "z": self.size.z},
            "center": {"x": self.center.x, "y": self.center.y, "z": self.center.z},
        }

    @classmethod
    def _from_dict(cls, data: Dict[str, Any]) -> "BoxFeature":
        p = data["params"]
        return cls(
            id=data.get("id"),
            name=data.get("name", "Box"),
            size=Vector3(**p["size"]),
            center=Point3(**p["center"]),
        )


@register_feature
@dataclass
class SphereFeature(Feature):
    radius: float = 1.0
    center: Point3 = Point3()

    def evaluate(self) -> Solid:
        return make_sphere(self.radius, self.center)

    def _serialize_params(self) -> Dict[str, Any]:
        return {
            "radius": self.radius,
            "center": {"x": self.center.x, "y": self.center.y, "z": self.center.z},
        }

    @classmethod
    def _from_dict(cls, data: Dict[str, Any]) -> "SphereFeature":
        p = data["params"]
        return cls(
            id=data.get("id"),
            name=data.get("name", "Sphere"),
            radius=p["radius"],
            center=Point3(**p["center"]),
        )


@register_feature
@dataclass
class CylinderFeature(Feature):
    radius: float = 1.0
    height: float = 1.0
    center: Point3 = Point3()
    axis: Vector3 = Vector3(0.0, 0.0, 1.0)

    def evaluate(self) -> Solid:
        return make_cylinder(self.radius, self.height, self.center, self.axis)

    def _serialize_params(self) -> Dict[str, Any]:
        return {
            "radius": self.radius,
            "height": self.height,
            "center": {"x": self.center.x, "y": self.center.y, "z": self.center.z},
            "axis": {"x": self.axis.x, "y": self.axis.y, "z": self.axis.z},
        }

    @classmethod
    def _from_dict(cls, data: Dict[str, Any]) -> "CylinderFeature":
        p = data["params"]
        return cls(
            id=data.get("id"),
            name=data.get("name", "Cylinder"),
            radius=p["radius"],
            height=p["height"],
            center=Point3(**p["center"]),
            axis=Vector3(**p["axis"]),
        )


@register_feature
@dataclass
class ConeFeature(Feature):
    base_radius: float = 1.0
    height: float = 1.0
    center: Point3 = Point3()
    axis: Vector3 = Vector3(0.0, 0.0, 1.0)

    def evaluate(self) -> Solid:
        return make_cone(self.base_radius, self.height, self.center, self.axis)

    def _serialize_params(self) -> Dict[str, Any]:
        return {
            "base_radius": self.base_radius,
            "height": self.height,
            "center": {"x": self.center.x, "y": self.center.y, "z": self.center.z},
            "axis": {"x": self.axis.x, "y": self.axis.y, "z": self.axis.z},
        }

    @classmethod
    def _from_dict(cls, data: Dict[str, Any]) -> "ConeFeature":
        p = data["params"]
        return cls(
            id=data.get("id"),
            name=data.get("name", "Cone"),
            base_radius=p["base_radius"],
            height=p["height"],
            center=Point3(**p["center"]),
            axis=Vector3(**p["axis"]),
        )

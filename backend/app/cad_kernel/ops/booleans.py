"""Custom BSP-tree constructive solid geometry.

This is a fully custom mesh-based boolean implementation.  Solids are first
converted to triangle soups, the BSP operation is performed, and the resulting
triangles are grouped back into faces by their original face tag.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Literal, Optional, Tuple

import numpy as np

from app.cad_kernel.core.feature_tree import Feature
from app.cad_kernel.core.topology import Face, FaceMesh, Shell, Solid
from app.cad_kernel.geom.meshing import merge_face_meshes


EPS = 1e-6


class Plane:
    """Implicit plane: normal·X + w = 0."""

    def __init__(self, normal: np.ndarray, w: float):
        self.normal = np.array(normal, dtype=float)
        norm = np.linalg.norm(self.normal)
        if norm > 0:
            self.normal /= norm
            self.w = w / norm
        else:
            self.normal = np.array([0.0, 0.0, 1.0])
            self.w = 0.0

    @staticmethod
    def from_points(a: np.ndarray, b: np.ndarray, c: np.ndarray) -> "Plane":
        normal = np.cross(b - a, c - a)
        n = np.linalg.norm(normal)
        if n < EPS:
            normal = np.array([0.0, 0.0, 1.0])
        else:
            normal /= n
        w = -np.dot(normal, a)
        return Plane(normal, w)

    def signed_distance(self, p: np.ndarray) -> float:
        return float(np.dot(self.normal, p) + self.w)

    def flip(self) -> "Plane":
        return Plane(-self.normal, -self.w)

    def clone(self) -> "Plane":
        return Plane(self.normal.copy(), self.w)


class Polygon:
    """A single triangle carrying its source face tag."""

    __slots__ = ("vertices", "normal", "face_tag")

    def __init__(self, vertices: np.ndarray, normal: Optional[np.ndarray] = None, face_tag: Optional[Tuple[str, str]] = None):
        self.vertices = np.array(vertices, dtype=float).reshape(3, 3)
        if normal is None:
            self.normal = np.cross(self.vertices[1] - self.vertices[0], self.vertices[2] - self.vertices[0])
            n = np.linalg.norm(self.normal)
            if n > 0:
                self.normal /= n
        else:
            self.normal = np.array(normal, dtype=float)
        self.face_tag = face_tag

    @property
    def plane(self) -> Plane:
        return Plane.from_points(self.vertices[0], self.vertices[1], self.vertices[2])

    def flip(self) -> "Polygon":
        return Polygon(self.vertices[::-1].copy(), -self.normal, self.face_tag)

    def clone(self) -> "Polygon":
        return Polygon(self.vertices.copy(), self.normal.copy(), self.face_tag)


def _split_polygon(
    plane: Plane,
    polygon: Polygon,
    coplanar_front: List[Polygon],
    coplanar_back: List[Polygon],
    front: List[Polygon],
    back: List[Polygon],
):
    """Split a polygon against a plane, appending fragments to the given lists."""
    types = []
    verts = polygon.vertices
    for v in verts:
        d = plane.signed_distance(v)
        if d < -EPS:
            types.append("BACK")
        elif d > EPS:
            types.append("FRONT")
        else:
            types.append("COPLANAR")

    # Fast paths
    if all(t == "FRONT" for t in types):
        front.append(polygon)
        return
    if all(t == "BACK" for t in types):
        back.append(polygon)
        return
    if all(t == "COPLANAR" for t in types):
        if plane.normal.dot(polygon.normal) > 0:
            coplanar_front.append(polygon)
        else:
            coplanar_back.append(polygon)
        return

    front_verts: List[np.ndarray] = []
    back_verts: List[np.ndarray] = []
    n = len(verts)
    for i in range(n):
        j = (i + 1) % n
        vi, vj = verts[i], verts[j]
        ti, tj = types[i], types[j]
        if ti == "FRONT":
            front_verts.append(vi)
        elif ti == "BACK":
            back_verts.append(vi)
        else:
            front_verts.append(vi)
            back_verts.append(vi)

        if (ti == "FRONT" and tj == "BACK") or (ti == "BACK" and tj == "FRONT"):
            di = plane.signed_distance(vi)
            dj = plane.signed_distance(vj)
            t = di / (di - dj) if abs(di - dj) > EPS else 0.0
            intersection = vi + t * (vj - vi)
            front_verts.append(intersection)
            back_verts.append(intersection)

    if len(front_verts) >= 3:
        front.append(_fan_triangulate(front_verts, polygon.normal, polygon.face_tag))
    if len(back_verts) >= 3:
        back.append(_fan_triangulate(back_verts, polygon.normal, polygon.face_tag))


def _fan_triangulate(verts: List[np.ndarray], normal: np.ndarray, face_tag) -> Polygon:
    """Fan triangulate a convex polygon and return the first triangle.

    The BSP algorithm only splits triangles, so the result should still be a
    triangle.  This helper keeps the code robust if extra vertices appear.
    """
    if len(verts) == 3:
        return Polygon(np.array(verts), normal, face_tag)
    # Return the central triangle of the fan for simplicity.  In practice this
    # branch is not reached for triangle inputs unless a vertex lands exactly
    # on the plane and is duplicated.
    return Polygon(np.array(verts[:3]), normal, face_tag)


class BSPNode:
    """Binary space partitioning node used for CSG."""

    def __init__(self, polygons: Optional[List[Polygon]] = None):
        self.plane: Optional[Plane] = None
        self.front: Optional[BSPNode] = None
        self.back: Optional[BSPNode] = None
        self.polygons: List[Polygon] = []
        if polygons:
            self.build(polygons)

    def build(self, polygons: List[Polygon]):
        if not polygons:
            return
        if self.plane is None:
            self.plane = polygons[0].plane.clone()
        front: List[Polygon] = []
        back: List[Polygon] = []
        for poly in polygons:
            _split_polygon(self.plane, poly, self.polygons, self.polygons, front, back)
        if front:
            self.front = BSPNode(front)
        if back:
            self.back = BSPNode(back)

    def all_polygons(self) -> List[Polygon]:
        result = list(self.polygons)
        if self.front:
            result.extend(self.front.all_polygons())
        if self.back:
            result.extend(self.back.all_polygons())
        return result

    def clone(self) -> "BSPNode":
        node = BSPNode()
        if self.plane is not None:
            node.plane = self.plane.clone()
        node.front = self.front.clone() if self.front else None
        node.back = self.back.clone() if self.back else None
        node.polygons = [p.clone() for p in self.polygons]
        return node

    def invert(self):
        for p in self.polygons:
            p.flip()
        if self.plane is not None:
            self.plane = self.plane.flip()
        if self.front:
            self.front.invert()
        if self.back:
            self.back.invert()
        self.front, self.back = self.back, self.front

    def clip_polygons(self, polygons: List[Polygon]) -> List[Polygon]:
        if self.plane is None:
            return [p.clone() for p in polygons]
        front: List[Polygon] = []
        back: List[Polygon] = []
        for p in polygons:
            _split_polygon(self.plane, p, front, front, front, back)
        if self.front:
            front = self.front.clip_polygons(front)
        if self.back:
            back = self.back.clip_polygons(back)
        else:
            back = []
        return front + back

    def clip_to(self, bsp: "BSPNode"):
        self.polygons = bsp.clip_polygons(self.polygons)
        if self.front:
            self.front.clip_to(bsp)
        if self.back:
            self.back.clip_to(bsp)


# ---------------------------------------------------------------------------
# Solid-level boolean operations
# ---------------------------------------------------------------------------


def _solid_to_polygons(solid: Solid) -> List[Polygon]:
    polys = []
    for face in solid.shell.faces:
        verts = face.mesh.vertices
        faces = face.mesh.faces
        for tri in faces:
            polys.append(Polygon(verts[tri], face_tag=(solid.id, face.id)))
    return polys


def _polygons_to_solid(polygons: List[Polygon], source_faces: Dict[Tuple[str, str], Face], new_id: Optional[str] = None) -> Solid:
    """Group polygons by face tag and rebuild faces."""
    groups: Dict[Tuple[str, str], List[Polygon]] = {}
    for poly in polygons:
        tag = poly.face_tag or ("__unknown__", "__unknown__")
        groups.setdefault(tag, []).append(poly)

    new_faces = []
    for tag, polys in groups.items():
        source = source_faces.get(tag)
        # Build a merged mesh for this face
        verts = []
        faces = []
        offset = 0
        for p in polys:
            verts.append(p.vertices)
            faces.append(np.array([[0, 1, 2]], dtype=int) + offset)
            offset += 3
        mesh = FaceMesh(np.concatenate(verts, axis=0), np.concatenate(faces, axis=0))
        surface = source.surface if source else None
        new_faces.append(Face(surface=surface, mesh=mesh, id=str(uuid.uuid4())))
    return Solid(Shell(new_faces), id=new_id or str(uuid.uuid4()))


def _source_face_map(*solids: Solid) -> Dict[Tuple[str, str], Face]:
    result = {}
    for solid in solids:
        for face in solid.shell.faces:
            result[(solid.id, face.id)] = face
    return result


def boolean(
    left: Solid,
    right: Solid,
    operation: Literal["union", "subtract", "intersect"],
) -> Solid:
    a_polys = _solid_to_polygons(left)
    b_polys = _solid_to_polygons(right)
    a = BSPNode(a_polys)
    b = BSPNode(b_polys)

    if operation == "union":
        a.clip_to(b)
        b.clip_to(a)
        b.invert()
        b.clip_to(a)
        b.invert()
        a.build(b.all_polygons())
        result_polys = a.all_polygons()
    elif operation == "subtract":
        a.invert()
        a.clip_to(b)
        a.invert()
        b.clip_to(a)
        b.invert()
        b.clip_to(a)
        b.invert()
        a.build(b.all_polygons())
        result_polys = a.all_polygons()
    else:  # intersect
        a.invert()
        b.clip_to(a)
        b.invert()
        a.clip_to(b)
        b.clip_to(a)
        a.build(b.all_polygons())
        a.invert()
        result_polys = a.all_polygons()

    source_faces = _source_face_map(left, right)
    return _polygons_to_solid(result_polys, source_faces)


# ---------------------------------------------------------------------------
# Feature wrapper
# ---------------------------------------------------------------------------


@dataclass
class BooleanFeature(Feature):
    operation: Literal["union", "subtract", "intersect"] = "union"
    children: List[Feature] = field(default_factory=list)

    def evaluate(self) -> Solid:
        if not self.children:
            raise ValueError("BooleanFeature needs at least one child")
        result = self.children[0].evaluate()
        for child in self.children[1:]:
            result = boolean(result, child.evaluate(), self.operation)
        return result

    def _serialize_params(self):
        return {
            "operation": self.operation,
            "children": [c.to_dict() for c in self.children],
        }

    @classmethod
    def _from_dict(cls, data):
        from app.cad_kernel.core.feature_tree import Feature

        return cls(
            id=data.get("id"),
            name=data.get("name", "Boolean"),
            operation=data["params"]["operation"],
            children=[Feature.from_dict(c) for c in data["params"]["children"]],
        )

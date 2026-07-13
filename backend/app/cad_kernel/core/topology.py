"""Boundary-representation topology for the Forge CAD kernel.

This is a lightweight, mesh-backed B-rep.  Each face carries both its
underlying parametric surface and a triangulation of its geometric extent,
which makes it easy to evaluate, boolean, and export without requiring a
full NURBS trimming implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from app.cad_kernel.core.types import Point3, Transform, Vector3


@dataclass
class Vertex:
    """0-dimensional topology: a point in space."""

    point: Point3
    id: Optional[str] = None
    attrs: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Edge:
    """1-dimensional topology: a curve bounded by two vertices."""

    curve: Any
    vertices: Tuple[Vertex, Vertex]
    id: Optional[str] = None
    attrs: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Loop:
    """A closed boundary of a face as an ordered list of oriented edges."""

    edges: List[Edge]
    id: Optional[str] = None


@dataclass
class FaceMesh:
    """Triangulation of a face."""

    vertices: np.ndarray  # shape (n, 3)
    faces: np.ndarray  # shape (m, 3)

    def __post_init__(self):
        if self.vertices.ndim != 2 or self.vertices.shape[1] != 3:
            raise ValueError("vertices must have shape (n, 3)")
        if self.faces.ndim != 2 or self.faces.shape[1] != 3:
            raise ValueError("faces must have shape (m, 3)")

    def copy(self) -> "FaceMesh":
        return FaceMesh(self.vertices.copy(), self.faces.copy())


@dataclass
class Face:
    """2-dimensional topology: a surface patch bounded by loops."""

    surface: Any
    mesh: FaceMesh
    loops: List[Loop] = field(default_factory=list)
    id: Optional[str] = None
    attrs: Dict[str, Any] = field(default_factory=dict)

    def transformed(self, transform: Transform) -> "Face":
        """Return a copy of this face with vertices transformed in place."""
        new_mesh = self.mesh.copy()
        verts = new_mesh.vertices
        hom = np.concatenate([verts, np.ones((verts.shape[0], 1))], axis=1)
        new_mesh.vertices = (hom @ transform.matrix.T)[:, :3]
        # Surface transforms are left to the caller (primitives create faces in
        # world space, so most kernel code does not need transformed surfaces).
        return Face(
            surface=self.surface,
            mesh=new_mesh,
            loops=self.loops,
            id=self.id,
            attrs=dict(self.attrs),
        )


@dataclass
class Shell:
    """A closed set of faces forming a 2-manifold boundary."""

    faces: List[Face]
    id: Optional[str] = None

    def transformed(self, transform: Transform) -> "Shell":
        return Shell(
            faces=[f.transformed(transform) for f in self.faces],
            id=self.id,
        )


@dataclass
class Solid:
    """A 3-dimensional body bounded by a shell."""

    shell: Shell
    id: Optional[str] = None
    attrs: Dict[str, Any] = field(default_factory=dict)

    def transformed(self, transform: Transform) -> "Solid":
        return Solid(
            shell=self.shell.transformed(transform),
            id=self.id,
            attrs=dict(self.attrs),
        )

    def all_triangles(self) -> np.ndarray:
        """Return a single (m, 3, 3) array of all face triangles."""
        parts = []
        for face in self.shell.faces:
            verts = face.mesh.vertices
            faces = face.mesh.faces
            if faces.size == 0:
                continue
            parts.append(verts[faces])
        if not parts:
            return np.zeros((0, 3, 3), dtype=float)
        return np.concatenate(parts, axis=0)


@dataclass
class Instance:
    """A placed solid in an assembly."""

    solid: Solid
    transform: Transform = field(default_factory=Transform.identity)
    id: Optional[str] = None

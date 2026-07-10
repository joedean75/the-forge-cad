"""Mesh utilities for the DAPS CAD kernel.

All functions operate on plain numpy arrays so they can be used both for
individual faces and for evaluated solids.
"""

from __future__ import annotations

from typing import Tuple

import numpy as np

from app.cad_kernel.core.topology import FaceMesh, Solid
from app.cad_kernel.core.types import Point3


def face_normals(vertices: np.ndarray, faces: np.ndarray) -> np.ndarray:
    """Compute outward-facing normals for each triangle."""
    v0 = vertices[faces[:, 0]]
    v1 = vertices[faces[:, 1]]
    v2 = vertices[faces[:, 2]]
    n = np.cross(v1 - v0, v2 - v0)
    lengths = np.linalg.norm(n, axis=1, keepdims=True)
    np.divide(n, lengths, out=n, where=lengths > 0)
    return n


def mesh_area(vertices: np.ndarray, faces: np.ndarray) -> float:
    v0 = vertices[faces[:, 0]]
    v1 = vertices[faces[:, 1]]
    v2 = vertices[faces[:, 2]]
    cross = np.cross(v1 - v0, v2 - v0)
    return float(np.sum(np.linalg.norm(cross, axis=1)) * 0.5)


def mesh_volume(vertices: np.ndarray, faces: np.ndarray) -> float:
    """Signed volume of a closed triangle mesh."""
    v0 = vertices[faces[:, 0]]
    v1 = vertices[faces[:, 1]]
    v2 = vertices[faces[:, 2]]
    return float(np.sum(np.einsum("ij,ij->i", v0, np.cross(v1, v2))) / 6.0)


def mesh_edges(vertices: np.ndarray, faces: np.ndarray) -> np.ndarray:
    """Return unique undirected edges as (n, 2) index array."""
    e0 = faces[:, [0, 1]]
    e1 = faces[:, [1, 2]]
    e2 = faces[:, [2, 0]]
    edges = np.concatenate([e0, e1, e2], axis=0)
    edges = np.sort(edges, axis=1)
    return np.unique(edges, axis=0)


def edge_lengths(vertices: np.ndarray, faces: np.ndarray) -> np.ndarray:
    edges = mesh_edges(vertices, faces)
    pts = vertices[edges]
    return np.linalg.norm(pts[:, 1] - pts[:, 0], axis=1)


def bounding_box(vertices: np.ndarray) -> Tuple[Point3, Point3]:
    if vertices.size == 0:
        return Point3(), Point3()
    min_v = vertices.min(axis=0)
    max_v = vertices.max(axis=0)
    return Point3(float(min_v[0]), float(min_v[1]), float(min_v[2])), Point3(float(max_v[0]), float(max_v[1]), float(max_v[2]))


def merge_face_meshes(meshes) -> FaceMesh:
    """Merge multiple FaceMesh objects into one."""
    if not meshes:
        return FaceMesh(np.zeros((0, 3), dtype=float), np.zeros((0, 3), dtype=int))
    verts = []
    faces = []
    offset = 0
    for m in meshes:
        verts.append(m.vertices)
        faces.append(m.faces + offset)
        offset += m.vertices.shape[0]
    return FaceMesh(np.concatenate(verts, axis=0), np.concatenate(faces, axis=0))


def solid_to_mesh(solid: Solid) -> FaceMesh:
    """Return a single FaceMesh for the whole solid."""
    return merge_face_meshes([f.mesh for f in solid.shell.faces])


def solid_triangle_soup(solid: Solid) -> np.ndarray:
    """Return (m, 3, 3) array of triangles."""
    return solid.all_triangles()

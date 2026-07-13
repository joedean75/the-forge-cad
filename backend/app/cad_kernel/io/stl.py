"""STL exporters for the Forge CAD kernel."""

from __future__ import annotations

import struct
from io import BytesIO
from typing import Union

import numpy as np

from app.cad_kernel.core.topology import FaceMesh, Solid
from app.cad_kernel.geom.meshing import face_normals, solid_to_mesh


def write_stl_binary(mesh: Union[FaceMesh, Solid], name: str = "part") -> bytes:
    """Return binary STL bytes."""
    if isinstance(mesh, Solid):
        mesh = solid_to_mesh(mesh)
    verts = mesh.vertices
    faces = mesh.faces
    normals = face_normals(verts, faces)

    buf = BytesIO()
    buf.write(b" " * 80)
    buf.write(struct.pack("<I", faces.shape[0]))
    for tri, normal in zip(faces, normals):
        for f in normal:
            buf.write(struct.pack("<f", float(f)))
        for idx in tri:
            for f in verts[idx]:
                buf.write(struct.pack("<f", float(f)))
        buf.write(struct.pack("<H", 0))
    return buf.getvalue()


def write_stl_ascii(mesh: Union[FaceMesh, Solid], name: str = "part") -> str:
    """Return an ASCII STL string."""
    if isinstance(mesh, Solid):
        mesh = solid_to_mesh(mesh)
    verts = mesh.vertices
    faces = mesh.faces
    normals = face_normals(verts, faces)
    lines = [f"solid {name}"]
    for tri, normal in zip(faces, normals):
        lines.append(
            f"  facet normal {normal[0]:.6e} {normal[1]:.6e} {normal[2]:.6e}"
        )
        lines.append("    outer loop")
        for idx in tri:
            v = verts[idx]
            lines.append(f"      vertex {v[0]:.6e} {v[1]:.6e} {v[2]:.6e}")
        lines.append("    endloop")
        lines.append("  endfacet")
    lines.append(f"endsolid {name}")
    return "\n".join(lines)

"""A compact binary form of a triangle mesh, for shipping geometry to a viewer.

STL was what the document carried, and it is the wrong shape for this job twice
over. It is **triangle soup** — every vertex repeated once per face that touches
it, discarding the sharing the mesher already computed — and it carries a
per-face normal that three.js recomputes on load anyway. On a 17,865-triangle
part that is 872 KB where the indexed positions are 322 KB, for strictly less
information.

So this is the mesh as the mesher already has it: positions once, then indices.

    magic   b"FMSH"          4 bytes
    version uint32           1
    counts  uint32 x 2       vertex count, face count
    coords  float32 x 3V     positions, metres, x/y/z
    faces   uint32  x 3F     vertex indices

Little-endian throughout, which is every platform this runs on and what
`DataView`/typed arrays assume on the other side. There is no normal and no
colour: the viewer computes normals from the geometry, and a normal that
disagrees with the triangles it belongs to is a bug waiting to be shipped.

STL is still what `export` writes — it is an interchange format and other tools
read it. This one is a transport, and is not written to disk anywhere.
"""

from __future__ import annotations

import struct
from typing import Tuple, Union

import numpy as np

from app.cad_kernel.core.topology import Solid
from app.cad_kernel.geom.meshing import solid_to_mesh

MAGIC = b"FMSH"
VERSION = 1
HEADER = struct.Struct("<4sIII")

MIME = "application/vnd.forge.mesh"


def write_mesh_binary(mesh: Union[Solid, object]) -> bytes:
    """Serialize a mesh (or a solid) to the transport form above."""
    if isinstance(mesh, Solid):
        mesh = solid_to_mesh(mesh)
    vertices = np.ascontiguousarray(np.asarray(mesh.vertices, dtype=np.float32))
    faces = np.ascontiguousarray(np.asarray(mesh.faces, dtype=np.uint32))
    if vertices.size == 0 or faces.size == 0:
        return HEADER.pack(MAGIC, VERSION, 0, 0)
    return (HEADER.pack(MAGIC, VERSION, len(vertices), len(faces))
            + vertices.tobytes() + faces.tobytes())


def read_mesh_binary(data: bytes) -> Tuple[np.ndarray, np.ndarray]:
    """``(vertices, faces)`` — the inverse, for tests and for round-tripping."""
    if len(data) < HEADER.size:
        raise ValueError("mesh payload is shorter than its header")
    magic, version, n_vertices, n_faces = HEADER.unpack_from(data, 0)
    if magic != MAGIC:
        raise ValueError(f"not a Forge mesh: magic {magic!r}")
    if version != VERSION:
        raise ValueError(f"unsupported Forge mesh version {version}")

    coords_end = HEADER.size + n_vertices * 3 * 4
    faces_end = coords_end + n_faces * 3 * 4
    if len(data) < faces_end:
        raise ValueError("mesh payload is shorter than its header claims")
    vertices = np.frombuffer(data, dtype="<f4", count=n_vertices * 3,
                             offset=HEADER.size).reshape(-1, 3)
    faces = np.frombuffer(data, dtype="<u4", count=n_faces * 3,
                          offset=coords_end).reshape(-1, 3)
    return vertices, faces

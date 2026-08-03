"""A compact binary form of a triangle mesh, for shipping geometry to a viewer.

STL was what the document carried, and it is the wrong shape for this job twice
over. It is **triangle soup** — every vertex repeated once per face that touches
it, discarding the sharing the mesher already computed — and it carries a
per-face normal that three.js recomputes on load anyway. On a 17,865-triangle
part that is 872 KB where the indexed positions are 322 KB, for strictly less
information.

So this is the mesh as the mesher already has it: positions once, then indices.

    magic   b"FMSH"          4 bytes
    version uint32           2
    counts  uint32 x 2       vertex count, face count
    coords  float32 x 3V     positions, metres, x/y/z
    faces   uint32  x 3F     vertex indices
    owned   uint32           F when provenance is known, else 0
    tablen  uint32           length in bytes of the id table
    owners  uint32  x F      index into the id table, or NO_OWNER
    table   utf-8            JSON array of feature ids

Little-endian throughout, which is every platform this runs on and what
`DataView`/typed arrays assume on the other side. There is no normal and no
colour: the viewer computes normals from the geometry, and a normal that
disagrees with the triangles it belongs to is a bug waiting to be shipped.

**Owners are an index, not a second mesh.** Clicking the body has to name a
feature, and the alternative — tessellating each feature separately — is N
meshes where the viewport draws one, both more bytes and a different picture.
Four bytes a triangle answers the same question against the mesh that is
already being sent. The block is optional: a body the exact kernel cannot
address ships without it and the viewer falls back to selecting in the tree.

Version 1 (no owner block) is still read, because a payload for a hash the
client already holds may predate the field.

STL is still what `export` writes — it is an interchange format and other tools
read it. This one is a transport, and is not written to disk anywhere.
"""

from __future__ import annotations

import json
import struct
from typing import List, Optional, Sequence, Tuple, Union

import numpy as np

from app.cad_kernel.core.topology import Solid
from app.cad_kernel.geom.meshing import solid_to_mesh

MAGIC = b"FMSH"
VERSION = 2
HEADER = struct.Struct("<4sIII")
OWNER_HEADER = struct.Struct("<II")

# uint32 max: "this triangle belongs to no feature we can name". Distinct from
# an absent owner block, which means the whole body could not be attributed.
NO_OWNER = 0xFFFFFFFF

MIME = "application/vnd.forge.mesh"


def write_mesh_binary(mesh: Union[Solid, object],
                      owners: Optional[Sequence[Optional[str]]] = None) -> bytes:
    """Serialize a mesh (or a solid) to the transport form above."""
    if isinstance(mesh, Solid):
        mesh = solid_to_mesh(mesh)
    vertices = np.ascontiguousarray(np.asarray(mesh.vertices, dtype=np.float32))
    faces = np.ascontiguousarray(np.asarray(mesh.faces, dtype=np.uint32))
    if vertices.size == 0 or faces.size == 0:
        return HEADER.pack(MAGIC, VERSION, 0, 0) + OWNER_HEADER.pack(0, 0)

    head = (HEADER.pack(MAGIC, VERSION, len(vertices), len(faces))
            + vertices.tobytes() + faces.tobytes())
    if owners is None or len(owners) != len(faces):
        # A length mismatch is a bug upstream, and shipping a misaligned owner
        # array would select the wrong feature rather than none.
        return head + OWNER_HEADER.pack(0, 0)

    table: List[str] = []
    index = {}
    packed = np.empty(len(owners), dtype="<u4")
    for i, owner in enumerate(owners):
        if owner is None:
            packed[i] = NO_OWNER
            continue
        slot = index.get(owner)
        if slot is None:
            slot = index[owner] = len(table)
            table.append(owner)
        packed[i] = slot
    encoded = json.dumps(table, separators=(",", ":")).encode("utf-8")
    return (head + OWNER_HEADER.pack(len(owners), len(encoded))
            + packed.tobytes() + encoded)


def read_mesh_binary(data: bytes) -> Tuple[np.ndarray, np.ndarray]:
    """``(vertices, faces)`` — the inverse, for tests and for round-tripping."""
    vertices, faces, _ = read_mesh_binary_owned(data)
    return vertices, faces


def read_mesh_binary_owned(data: bytes):
    """``(vertices, faces, owners)``, where owners is a list of ids or ``None``."""
    if len(data) < HEADER.size:
        raise ValueError("mesh payload is shorter than its header")
    magic, version, n_vertices, n_faces = HEADER.unpack_from(data, 0)
    if magic != MAGIC:
        raise ValueError(f"not a Forge mesh: magic {magic!r}")
    if version not in (1, VERSION):
        raise ValueError(f"unsupported Forge mesh version {version}")

    coords_end = HEADER.size + n_vertices * 3 * 4
    faces_end = coords_end + n_faces * 3 * 4
    if len(data) < faces_end:
        raise ValueError("mesh payload is shorter than its header claims")
    vertices = np.frombuffer(data, dtype="<f4", count=n_vertices * 3,
                             offset=HEADER.size).reshape(-1, 3)
    faces = np.frombuffer(data, dtype="<u4", count=n_faces * 3,
                          offset=coords_end).reshape(-1, 3)
    if version == 1 or len(data) < faces_end + OWNER_HEADER.size:
        return vertices, faces, None

    n_owners, table_bytes = OWNER_HEADER.unpack_from(data, faces_end)
    if not n_owners:
        return vertices, faces, None
    owners_end = faces_end + OWNER_HEADER.size + n_owners * 4
    if len(data) < owners_end + table_bytes:
        raise ValueError("mesh payload is shorter than its header claims")
    packed = np.frombuffer(data, dtype="<u4", count=n_owners,
                           offset=faces_end + OWNER_HEADER.size)
    table = json.loads(data[owners_end:owners_end + table_bytes].decode("utf-8"))
    return vertices, faces, [None if slot == NO_OWNER else table[slot]
                             for slot in packed.tolist()]

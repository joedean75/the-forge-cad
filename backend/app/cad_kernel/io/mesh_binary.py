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
    owners  uint32  x F      index into the id table, or NO_LABEL
    table   utf-8            JSON array of feature ids
    faced   uint32           F when the faces are nameable, else 0   (v3)
    tablen  uint32           length in bytes of the id table         (v3)
    faceids uint32  x F      index into the id table, or NO_LABEL    (v3)
    table   utf-8            JSON array of face ids                  (v3)

Little-endian throughout, which is every platform this runs on and what
`DataView`/typed arrays assume on the other side. There is no normal and no
colour: the viewer computes normals from the geometry, and a normal that
disagrees with the triangles it belongs to is a bug waiting to be shipped.

**A label is an index, not a second mesh.** Clicking the body has to name a
feature, and the alternative — tessellating each feature separately — is N
meshes where the viewport draws one, both more bytes and a different picture.
Four bytes a triangle answers the same question against the mesh that is
already being sent, and the id table means a body with 14 faces stores 14
strings rather than one per triangle.

The **face** block is the same trick asked of a different question: which face
did I click, so it can be filleted or shelled or sketched on. Both blocks are
optional and independent — a body the exact kernel cannot address ships with
neither, and the viewer falls back to the tree and to the face list.

Versions 1 (no labels) and 2 (owners only) are still read, because a payload
for a hash the client already holds may predate a field.

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
VERSION = 3
HEADER = struct.Struct("<4sIII")
OWNER_HEADER = struct.Struct("<II")
LABEL_HEADER = OWNER_HEADER

# uint32 max: "this triangle belongs to nothing we can name". Distinct from an
# absent block, which means the whole body could not be attributed.
NO_LABEL = 0xFFFFFFFF
NO_OWNER = NO_LABEL   # the name this had when owners were the only labels

MIME = "application/vnd.forge.mesh"


def _pack_labels(labels, n_faces: int) -> bytes:
    """One label block: count, table length, the indices, then the table.

    An empty block (count 0) is what "we could not attribute this" looks like on
    the wire, and it is also what a **length mismatch** produces. That is
    deliberate: a misaligned array names the *wrong* face, which is worse than
    naming none, so it is dropped rather than shipped.
    """
    if labels is None or len(labels) != n_faces:
        return LABEL_HEADER.pack(0, 0)
    table: List[str] = []
    index: dict = {}
    packed = np.empty(n_faces, dtype="<u4")
    for i, label in enumerate(labels):
        if label is None:
            packed[i] = NO_LABEL
            continue
        slot = index.get(label)
        if slot is None:
            slot = index[label] = len(table)
            table.append(label)
        packed[i] = slot
    encoded = json.dumps(table, separators=(",", ":")).encode("utf-8")
    # Padded to a 4-byte boundary, because the *next* block starts here and its
    # indices are read as a `Uint32Array` view over the same buffer — which the
    # browser refuses at an unaligned offset ("start offset should be a multiple
    # of 4"). Trailing spaces are legal JSON whitespace, so the padding is
    # counted in the declared length and every reader, old or new, still parses
    # the table.
    encoded += b" " * (-len(encoded) % 4)
    return LABEL_HEADER.pack(n_faces, len(encoded)) + packed.tobytes() + encoded


def write_mesh_binary(mesh: Union[Solid, object],
                      owners: Optional[Sequence[Optional[str]]] = None,
                      face_ids: Optional[Sequence[Optional[str]]] = None) -> bytes:
    """Serialize a mesh (or a solid) to the transport form above."""
    if isinstance(mesh, Solid):
        mesh = solid_to_mesh(mesh)
    vertices = np.ascontiguousarray(np.asarray(mesh.vertices, dtype=np.float32))
    faces = np.ascontiguousarray(np.asarray(mesh.faces, dtype=np.uint32))
    absent = LABEL_HEADER.pack(0, 0) * 2
    if vertices.size == 0 or faces.size == 0:
        return HEADER.pack(MAGIC, VERSION, 0, 0) + absent

    return (HEADER.pack(MAGIC, VERSION, len(vertices), len(faces))
            + vertices.tobytes() + faces.tobytes()
            + _pack_labels(owners, len(faces))
            + _pack_labels(face_ids, len(faces)))

def read_mesh_binary(data: bytes) -> Tuple[np.ndarray, np.ndarray]:
    """``(vertices, faces)`` — the inverse, for tests and for round-tripping."""
    vertices, faces, _, _ = read_mesh_binary_labelled(data)
    return vertices, faces


def read_mesh_binary_owned(data: bytes):
    """``(vertices, faces, owners)`` — the feature label only."""
    vertices, faces, owners, _ = read_mesh_binary_labelled(data)
    return vertices, faces, owners


def _read_labels(data: bytes, offset: int):
    """``(labels, next_offset)`` for one block, or ``(None, offset)`` if absent."""
    if len(data) < offset + LABEL_HEADER.size:
        return None, offset
    count, table_bytes = LABEL_HEADER.unpack_from(data, offset)
    body = offset + LABEL_HEADER.size
    if not count:
        return None, body
    end = body + count * 4
    if len(data) < end + table_bytes:
        raise ValueError("mesh payload is shorter than its header claims")
    packed = np.frombuffer(data, dtype="<u4", count=count, offset=body)
    table = json.loads(data[end:end + table_bytes].decode("utf-8"))
    labels = [None if slot == NO_LABEL else table[slot] for slot in packed.tolist()]
    return labels, end + table_bytes


def read_mesh_binary_labelled(data: bytes):
    """``(vertices, faces, owners, face_ids)``; either label list may be ``None``."""
    if len(data) < HEADER.size:
        raise ValueError("mesh payload is shorter than its header")
    magic, version, n_vertices, n_faces = HEADER.unpack_from(data, 0)
    if magic != MAGIC:
        raise ValueError(f"not a Forge mesh: magic {magic!r}")
    if version not in (1, 2, VERSION):
        raise ValueError(f"unsupported Forge mesh version {version}")

    coords_end = HEADER.size + n_vertices * 3 * 4
    faces_end = coords_end + n_faces * 3 * 4
    if len(data) < faces_end:
        raise ValueError("mesh payload is shorter than its header claims")
    vertices = np.frombuffer(data, dtype="<f4", count=n_vertices * 3,
                             offset=HEADER.size).reshape(-1, 3)
    faces = np.frombuffer(data, dtype="<u4", count=n_faces * 3,
                          offset=coords_end).reshape(-1, 3)
    if version == 1:
        return vertices, faces, None, None
    owners, offset = _read_labels(data, faces_end)
    if version == 2:
        # v2 has no face block. Stopping here rather than reading past the end
        # is why the version is in the header at all.
        return vertices, faces, owners, None
    face_ids, _ = _read_labels(data, offset)
    return vertices, faces, owners, face_ids

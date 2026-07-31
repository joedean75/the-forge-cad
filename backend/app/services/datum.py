"""Datum / face-reference resolution — a pragmatic topological-naming bridge.

The SDF kernel has no persistent B-rep faces or edges, so we cannot track a face
identity through arbitrary operations the way OCCT/Parasolid do. What we *can* do
cheaply and robustly is resolve a **symbolic** face reference — "the top face of
feature X" — against that feature's *live* geometry at regeneration time. When an
upstream feature changes size, a sketch placed on its face moves with it instead
of dangling at a stale absolute offset.

This covers the common "sketch on a face, then edit the base" workflow. It does
NOT survive an upstream edit that removes the referenced feature or fundamentally
changes which face is "top". Where the exact kernel can build the body,
`cad_kernel/core/naming.py` names real faces and edges by descriptor instead;
this module remains the answer whenever it cannot.

Face names map to a sketch plane + the extreme coordinate along that plane's
normal:

    top / bottom  -> xy plane at max / min Z
    right / left  -> yz plane at max / min X
    back / front  -> xz plane at max / min Y
"""

from __future__ import annotations

from typing import Dict, Tuple

# face -> (sketch_plane, axis_index, take_max)
FACE_MAP: Dict[str, Tuple[str, int, bool]] = {
    "top": ("xy", 2, True),
    "bottom": ("xy", 2, False),
    "right": ("yz", 0, True),
    "left": ("yz", 0, False),
    "back": ("xz", 1, True),
    "front": ("xz", 1, False),
}

FACES = tuple(FACE_MAP.keys())


def resolve_face(tree: dict, feature_id: str, face: str) -> Tuple[str, float]:
    """Return ``(plane, offset)`` for ``face`` of the referenced feature (offset
    along the plane normal). See :func:`resolve_face_frame` for the in-plane origin."""
    plane, offset, _ = resolve_face_frame(tree, feature_id, face)
    return plane, offset


def resolve_face_frame(tree: dict, feature_id: str, face: str):
    """Return ``(plane, offset, origin_uv)`` for ``face`` of the referenced feature.

    ``origin_uv`` is the in-plane centre of the feature's face — so a sketch
    placed on the face is zeroed on the face, not the world origin. Evaluates
    just the referenced feature's subtree to read its current bounding box.
    Raises ``KeyError`` if the feature is missing, ``ValueError`` for a bad face.
    """
    if face not in FACE_MAP:
        raise ValueError(f"unknown face '{face}'; expected one of {FACES}")

    # Imported lazily to avoid a heavy import chain at module load.
    from app.cad_kernel.core.feature_tree import FeatureTree
    from app.cad_kernel.kernel import Kernel
    from app.services import feature_ops

    node = feature_ops.find_node(tree, feature_id)
    if node is None:
        raise KeyError(f"referenced feature {feature_id} not found")

    subtree = {"version": tree.get("version", "brep-v1"), "root": node}
    solid = Kernel.evaluate_tree(FeatureTree.from_dict(subtree))
    mesh = Kernel.tessellate(solid)
    if mesh.vertices is None or len(mesh.vertices) == 0:
        raise ValueError(f"referenced feature {feature_id} produced empty geometry")

    plane, axis, take_max = FACE_MAP[face]
    coords = mesh.vertices[:, axis]
    offset = float(coords.max() if take_max else coords.min())
    # In-plane centre = midpoint of the feature's bbox on the two spanning axes.
    from app.cad_kernel.core.planes import plane_uvw

    ua, va, _ = plane_uvw(plane)
    verts = mesh.vertices
    u0 = float((verts[:, ua].min() + verts[:, ua].max()) / 2.0)
    v0 = float((verts[:, va].min() + verts[:, va].max()) / 2.0)
    return plane, offset, (u0, v0)

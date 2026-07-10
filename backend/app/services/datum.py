"""Datum / face-reference resolution — a pragmatic topological-naming bridge.

The SDF kernel has no persistent B-rep faces or edges, so we cannot track a face
identity through arbitrary operations the way OCCT/Parasolid do. What we *can* do
cheaply and robustly is resolve a **symbolic** face reference — "the top face of
feature X" — against that feature's *live* geometry at regeneration time. When an
upstream feature changes size, a sketch placed on its face moves with it instead
of dangling at a stale absolute offset.

This covers the common "sketch on a face, then edit the base" workflow. It does
NOT survive an upstream edit that removes the referenced feature or fundamentally
changes which face is "top"; true topological naming is gated on the Horizon-2
hybrid B-rep kernel (see ROADMAP.md).

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
    """Return ``(plane, offset)`` for ``face`` of the referenced feature.

    Evaluates just the referenced feature's subtree to read its current bounding
    box. Raises ``KeyError`` if the feature is missing and ``ValueError`` for an
    unknown face name.
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
    return plane, offset

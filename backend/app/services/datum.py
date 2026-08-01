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


# ------------------------------------------------- a real, named face --------

# Which sketch plane a face normal picks out, and the axis it stands off along.
_NORMAL_PLANES = (("xy", 2), ("xz", 1), ("yz", 0))


def feature_faces(tree: dict, feature_id: str):
    """``(shape, face_table)`` for one feature's subtree, or ``(None, [])``.

    A sketch is placed on a face of *the feature it names*, not of the whole
    body — the same scoping the symbolic path uses, and for the same reason:
    resolving against the whole body would fold the sketch's own extrusion into
    the datum it is measured from.

    Built with history so each descriptor carries its tag, which is what lets a
    reference survive an edit that re-topologises the feature rather than merely
    resizing it.
    """
    from app.cad_kernel.core import naming
    from app.cad_kernel.core.feature_tree import FeatureTree
    from app.cad_kernel.evaluator import brep
    from app.services import feature_ops

    if not brep.AVAILABLE:
        return None, []
    node = feature_ops.find_node(tree, feature_id)
    if node is None:
        raise KeyError(f"referenced feature {feature_id} not found")
    subtree = {"version": tree.get("version", "brep-v1"), "root": node}
    resolved = feature_ops.resolve_sketches(feature_ops.resolve_suppressed(subtree))
    shape, history = brep.build_with_history(FeatureTree.from_dict(resolved).root)
    if shape is None:
        return None, []
    return shape, naming.face_table(shape, (history or {}).get("entities"))


def feature_shape(tree: dict, feature_id: str):
    """Just the exact body of one feature's subtree, or None."""
    return feature_faces(tree, feature_id)[0]


def resolve_face_id_frame(tree: dict, feature_id: str, reference):
    """``(plane, offset, origin_uv, status)`` for a face named by `list_faces`.

    Where `resolve_face` offers six bounding-box extremes, this places a sketch on
    any planar face the exact kernel can name — and follows it through a
    re-topologising edit, because the reference is resolved by
    `naming.resolve_references`, tag first. When the face is gone the status says
    ``deleted`` or ``unresolved`` rather than the sketch landing on whatever was
    nearest, which is the whole point of the naming scheme.

    ``reference`` is the whole stored descriptor, not an id. An id is a hash of
    the face's current measurements, so it changes the moment an upstream
    dimension moves — resize a plate and its top face has a new id but is plainly
    the same face. The descriptor is what `naming` matches on; storing only the
    id is what made the first version of this lose the face on every resize.
    """
    from app.cad_kernel.core import naming

    if isinstance(reference, str):
        reference = {"id": reference}
    face_id = reference.get("id")
    shape = feature_shape(tree, feature_id)
    if shape is None:
        raise ValueError(f"feature {feature_id} has no exact form, so no named faces")

    row = naming.resolve_references(shape, [reference], kind="face")[0]
    if row["status"] in ("deleted", "unresolved") or not row["shapes"]:
        raise ValueError(f"face {face_id} on {feature_id} is {row['status']}")

    descriptor = naming.face_descriptor(naming.as_kind(row["shapes"][0], "face"))
    if descriptor["kind"] != "plane":
        raise ValueError(f"face {face_id} is a {descriptor['kind']}, and a sketch "
                         "plane has to be planar")

    normal = [abs(n) for n in descriptor["normal"]]
    plane, axis = max(_NORMAL_PLANES, key=lambda pair: normal[pair[1]])
    if normal[axis] < 0.999:
        raise ValueError(f"face {face_id} is not square to an axis, and sketch "
                         "planes are xy/xz/yz")

    from app.cad_kernel.core.planes import plane_uvw

    ua, va, _ = plane_uvw(plane)
    centre = descriptor["center"]
    return plane, float(centre[axis]), (float(centre[ua]), float(centre[va])), row["status"]

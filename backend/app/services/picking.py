"""Which feature made each triangle — so a click on the body names a feature.

The viewport could already be clicked, and the answer was a guess: `pickFeatureAt`
in the frontend built an analytic proxy (a box, a sphere, a cylinder) for each
feature and returned the nearest one. That is right most of the time for a part
made of primitives and simply has no answer for an extrude, a sheet-metal body
or an imported solid — and "right most of the time" is the worst accuracy a
selection can have, because it is wrong exactly when the part is interesting.

The real answer comes from the tessellation itself and travels with the mesh
(`cad_kernel/io/mesh_binary`, four bytes a triangle):

* **On the exact kernel it is not a guess at all.** Every face carries a tag
  minted by the feature that created it (`core/history`), and every triangle was
  meshed from exactly one face. `brep.tessellate_owned` reads it off.
* **On the SDF kernel there are no faces**, so the question is asked of the
  fields instead: a triangle centroid lies on the body's surface, and the leaf
  whose own distance field vanishes there is the leaf that put it there. That is
  exact wherever surfaces do not coincide; where two leaves both vanish — a boss
  flush with the plate under it — the first wins, and where none does within the
  sampling tolerance the triangle is left unowned rather than attributed to
  whatever was nearest.

Unowned is a real answer and the viewer treats it as one: no selection, rather
than a selection of something else.
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional

import numpy as np

from app.cad_kernel.core.feature_tree import node_children

_LOGGER = logging.getLogger(__name__)

# Above this the per-leaf sweep stops being cheap, and a tree this wide is one
# whose owners the exact kernel almost certainly supplied already.
_MAX_LEAVES = 64


def owners_for(solid: Any, mesh: Any, tree: dict, resolution: int = 128
               ) -> Optional[List[Optional[str]]]:
    """One feature id (or None) per triangle of ``mesh``, or None if unknowable."""
    recorded = (getattr(solid, "attrs", None) or {}).get("triangle_owners")
    if recorded is not None and len(recorded) == len(mesh.faces):
        return list(recorded)
    try:
        return _owners_from_fields(tree, mesh, resolution)
    except Exception as exc:  # noqa: BLE001 — provenance is a nicety, never a failure
        _LOGGER.debug("triangle ownership unavailable: %s", exc)
        return None


def _leaves(tree: dict) -> List[dict]:
    out: List[dict] = []

    def walk(node: dict) -> None:
        kids = node_children(node)
        if not kids:
            out.append(node)
            return
        for kid in kids:
            walk(kid)

    root = (tree or {}).get("root")
    if root:
        walk(root)
    return out


def _owners_from_fields(tree: dict, mesh: Any, resolution: int
                        ) -> Optional[List[Optional[str]]]:
    from app.cad_kernel.core.feature_tree import Feature
    from app.cad_kernel.evaluator.sdf import sdf_from_feature

    faces = np.asarray(mesh.faces, dtype=np.int64)
    vertices = np.asarray(mesh.vertices, dtype=float)
    if not len(faces) or not len(vertices):
        return None
    leaves = _leaves(tree)
    if not leaves or len(leaves) > _MAX_LEAVES:
        return None

    centroids = vertices[faces].mean(axis=1)
    # The surface is only located to within a sample spacing, so "on this leaf"
    # has to be judged at that scale. Tied to the grid the body was marched on,
    # not to a fixed number: the same part at resolution 64 and 256 must give
    # the same answer.
    span = float(np.ptp(vertices, axis=0).max()) or 1.0
    tolerance = 2.0 * span / max(int(resolution), 8)

    best = np.full(len(faces), np.inf)
    owner = np.full(len(faces), -1, dtype=np.int64)
    for index, node in enumerate(leaves):
        if not node.get("id"):
            continue
        field = np.abs(sdf_from_feature(Feature.from_dict(node))(centroids))
        closer = field < best
        best = np.where(closer, field, best)
        owner = np.where(closer, index, owner)

    owner = np.where(best <= tolerance, owner, -1)
    ids = [node.get("id") for node in leaves]
    return [None if slot < 0 else ids[slot] for slot in owner.tolist()]

"""Which feature made each triangle, and how it reaches the viewport.

Clicking the body could already select a feature, and the answer was a guess:
the frontend built an analytic proxy for each feature and returned the nearest.
That is right for a part made of primitives and has no answer at all for an
extrude or an imported solid — and "right most of the time" is the worst
accuracy a selection can have, because it is wrong exactly when the part is
interesting enough to need clicking.

These tests pin the two things that make the real answer usable: that it is
*correct* (the triangle came from that feature or it did not), and that it
degrades to "unowned" rather than to a plausible wrong id.
"""

import json

import numpy as np
import pytest

from app.cad_kernel.evaluator import brep
from app.cad_kernel.io.mesh_binary import (
    NO_OWNER,
    read_mesh_binary,
    read_mesh_binary_owned,
    write_mesh_binary,
)
from app.services import cad_operations as ops
from app.services import document_analysis
from app.services import document_service as docs
from app.services import picking

exact_only = pytest.mark.skipif(not brep.AVAILABLE, reason="needs OpenCascade")


class _Mesh:
    def __init__(self, vertices, faces):
        self.vertices = np.asarray(vertices, dtype=float)
        self.faces = np.asarray(faces, dtype=int)


def _plate_and_boss():
    doc_id = docs.create_document(name="pickable").id
    ops.execute("add_primitive", doc_id, {
        "shape": "box", "params": {"size": {"x": 0.1, "y": 0.06, "z": 0.02}}})
    ops.execute("add_primitive", doc_id, {"shape": "cylinder", "params": {
        "radius": 0.008, "height": 0.04, "center": {"x": 0.03, "y": 0.0, "z": 0.012}}})
    return doc_id


def _owners_over_the_wire(doc_id):
    doc = docs.get_document(doc_id)
    payload = document_analysis.mesh_payload(doc_id, doc.derived.mesh_hash)
    assert payload is not None
    _, _, owners = read_mesh_binary_owned(payload)
    return owners


# ------------------------------------------------------------- the format --

def test_the_owner_block_round_trips():
    mesh = _Mesh([[0, 0, 0], [1, 0, 0], [0, 1, 0], [1, 1, 0]], [[0, 1, 2], [1, 3, 2]])
    payload = write_mesh_binary(mesh, ["f1", "f7"])
    vertices, faces, owners = read_mesh_binary_owned(payload)
    assert owners == ["f1", "f7"]
    assert len(vertices) == 4 and len(faces) == 2


def test_an_unowned_triangle_survives_the_round_trip():
    """`None` is a real answer — "this triangle belongs to no feature we can
    name" — and it must not come back as the first entry of the id table."""
    mesh = _Mesh([[0, 0, 0], [1, 0, 0], [0, 1, 0]], [[0, 1, 2]])
    assert read_mesh_binary_owned(write_mesh_binary(mesh, [None]))[2] == [None]


def test_the_id_table_stores_each_feature_once():
    """Four bytes a triangle, not a string a triangle. On a real part the whole
    block is a rounding error next to the positions."""
    mesh = _Mesh([[0, 0, 0], [1, 0, 0], [0, 1, 0], [1, 1, 0]],
                 [[0, 1, 2], [1, 3, 2], [0, 1, 3], [0, 3, 2]])
    payload = write_mesh_binary(mesh, ["f1"] * 4)
    assert json.dumps(["f1"], separators=(",", ":")).encode() in payload


def test_a_mismatched_owner_list_is_dropped_rather_than_shipped():
    """Misaligned provenance selects the *wrong* feature, which is worse than
    none — so a length that does not match the triangles is refused here."""
    mesh = _Mesh([[0, 0, 0], [1, 0, 0], [0, 1, 0], [1, 1, 0]], [[0, 1, 2], [1, 3, 2]])
    assert read_mesh_binary_owned(write_mesh_binary(mesh, ["f1"]))[2] is None


def test_a_version_1_payload_still_reads():
    """A hash the client already holds may name a payload written before the
    field existed."""
    import struct

    from app.cad_kernel.io import mesh_binary

    mesh = _Mesh([[0, 0, 0], [1, 0, 0], [0, 1, 0]], [[0, 1, 2]])
    v2 = write_mesh_binary(mesh)
    v1 = struct.pack("<4sIII", mesh_binary.MAGIC, 1, 3, 1) + v2[16:16 + 3 * 12 + 12]
    vertices, faces, owners = read_mesh_binary_owned(v1)
    assert owners is None and len(faces) == 1
    assert np.array_equal(read_mesh_binary(v1)[1], faces)


def test_no_owner_is_not_a_valid_table_slot():
    assert NO_OWNER == 0xFFFFFFFF


# --------------------------------------------------------- the exact path --

@exact_only
def test_every_triangle_names_the_feature_that_made_it():
    doc_id = _plate_and_boss()
    owners = _owners_over_the_wire(doc_id)
    assert owners is not None

    from app.cad_kernel.core.feature_tree import walk_nodes

    leaves = {n["id"]: n["type"] for n in walk_nodes(docs.get_document(doc_id).feature_tree)
              if not (n.get("params") or {}).get("children")}
    assert set(owners) <= set(leaves), "a triangle was attributed to something not in the tree"
    assert len(set(owners)) == 2, "both bodies should own some of the surface"


@exact_only
def test_the_owner_is_the_body_the_triangle_is_actually_on():
    """Not a proximity test dressed up. Every triangle attributed to the boss
    must lie on the boss — within the cylinder's radius of its axis — and the
    plate's must not be up the boss."""
    doc_id = _plate_and_boss()
    doc = docs.get_document(doc_id)
    payload = document_analysis.mesh_payload(doc_id, doc.derived.mesh_hash)
    vertices, faces, owners = read_mesh_binary_owned(payload)
    centroids = np.asarray(vertices, dtype=float)[np.asarray(faces, dtype=int)].mean(axis=1)

    from app.cad_kernel.core.feature_tree import walk_nodes

    boss = next(n for n in walk_nodes(doc.feature_tree) if n["type"] == "CylinderFeature")
    picked = np.array([o == boss["id"] for o in owners])
    assert picked.any()

    radial = np.hypot(centroids[:, 0] - 0.03, centroids[:, 1])
    # 1e-4 of slack for the tessellation chord; the boss is 8 mm in radius.
    assert radial[picked].max() <= 0.008 + 1e-4
    # The plate's top is at z = 0.01 and the boss reaches 0.032; nothing above
    # the plate may be attributed to the plate.
    assert centroids[~picked][:, 2].max() <= 0.0101


@exact_only
def test_a_bore_belongs_to_the_hole_that_made_it():
    """A hole cuts with a cylinder of its own, tagged in a namespace of its own.
    The wall it leaves is that cylinder's face — and the feature someone
    clicking it means is the hole."""
    doc_id = docs.create_document(name="bored").id
    ops.execute("add_primitive", doc_id, {
        "shape": "box", "params": {"size": {"x": 0.06, "y": 0.06, "z": 0.02}}})
    result = ops.execute("add_modifier", doc_id, {"modifier": "hole", "params": {
        "radius": 0.006, "depth": 0.06, "center": {"x": 0.0, "y": 0.0, "z": 0.0}}})
    assert result["ok"], result.get("error")
    hole_id = result["result"]["feature_id"]

    owners = _owners_over_the_wire(doc_id)
    assert owners is not None
    assert hole_id in owners, "the bore wall was not attributed to the hole"
    assert not any(o and "~" in o for o in owners), "an internal namespace leaked out"


@exact_only
def test_the_answer_does_not_depend_on_the_cache_being_warm():
    doc_id = _plate_and_boss()
    warm = _owners_over_the_wire(doc_id)
    brep.clear_cache()
    docs._MESH_CACHE.clear()
    assert _owners_over_the_wire(doc_id) == warm


# ----------------------------------------------------------- the SDF path --

def test_a_body_with_no_exact_form_is_still_attributed():
    """No faces to read a tag off, so the question goes to the fields: the leaf
    whose own distance vanishes at a triangle centroid is the leaf that put it
    there."""
    doc_id = docs.create_document(name="shelled").id
    ops.execute("add_primitive", doc_id, {
        "shape": "box", "params": {"size": {"x": 0.08, "y": 0.05, "z": 0.02}}})
    ops.execute("add_primitive", doc_id, {"shape": "sphere", "params": {
        "radius": 0.012, "center": {"x": 0.025, "y": 0.0, "z": 0.012}}})
    assert ops.execute("add_modifier", doc_id,
                       {"modifier": "shell", "params": {"thickness": 0.003}})["ok"]
    doc = docs.get_document(doc_id)
    assert doc.derived.sampler != "brep", "this case is about the SDF kernel"

    owners = _owners_over_the_wire(doc_id)
    assert owners is not None
    named = {o for o in owners if o}
    assert len(named) == 2, f"expected both bodies to be recognised, got {named}"


def test_a_triangle_on_no_leaf_surface_is_left_unowned():
    """The honest failure. A centroid that no leaf's field vanishes at is not
    attributed to whichever leaf happened to be closest."""
    tree = {"root": {"id": "f1", "type": "BoxFeature", "name": "Box",
                     "params": {"size": {"x": 0.02, "y": 0.02, "z": 0.02},
                                "center": {"x": 0.0, "y": 0.0, "z": 0.0}}}}
    # A triangle a long way from the box — nothing on the body is there.
    far = _Mesh([[1.0, 0, 0], [1.0, 0.01, 0], [1.0, 0, 0.01]], [[0, 1, 2]])
    assert picking.owners_for(None, far, tree, resolution=128) == [None]

    on_it = _Mesh([[0.01, 0, 0], [0.01, 0.005, 0], [0.01, 0, 0.005]], [[0, 1, 2]])
    assert picking.owners_for(None, on_it, tree, resolution=128) == ["f1"]


def test_provenance_never_breaks_an_evaluation():
    """A nicety, not a dependency: anything that goes wrong working out who
    owns a triangle costs the picker, not the document."""

    class Exploding:
        @property
        def faces(self):
            raise RuntimeError("no")

        vertices = None

    assert picking.owners_for(None, Exploding(), {"root": {}}) is None

"""Reproducibility guarantees for recorded/replayed operation sequences.

Trajectory export, the tree decompiler and any regression fixture all assume the
same inputs produce the same outputs — same feature ids, same geometry. Nothing
asserted that before: ids came from the wall clock and there was no output-side
hash at all, so a replay silently diverged from the run it was replaying.
"""

import pytest

from app.cad_kernel.core.feature_tree import FeatureTree, walk_nodes
from app.cad_kernel.geom.meshing import mesh_hash
from app.cad_kernel.kernel import Kernel
from app.services import cad_operations as ops
from app.services import document_service as docs
from app.services.feature_ops import IdAllocator, next_id


# ------------------------------------------------------------ id allocation --

def test_allocated_ids_depend_only_on_the_tree():
    tree = {"version": "brep-v1", "root": {"type": "BoxFeature", "id": "f1", "params": {}}}
    assert next_id(tree) == next_id(tree) == "f2"


def test_allocator_fills_the_lowest_free_slot():
    tree = {"version": "brep-v1", "root": {
        "type": "BooleanFeature", "id": "f1",
        "params": {"children": [{"type": "BoxFeature", "id": "f3", "params": {}}]},
    }}
    ids = IdAllocator(tree)
    assert [ids.alloc(), ids.alloc(), ids.alloc()] == ["f2", "f4", "f5"]


def test_allocator_never_reissues_a_legacy_id():
    """Documents persisted before this change carry timestamp- and uuid-shaped ids."""
    tree = {"version": "brep-v1", "root": {
        "type": "BooleanFeature", "id": "f18f3a2b4c171",
        "params": {"children": [
            {"type": "BoxFeature", "id": "f1", "params": {}},
            {"type": "BoxFeature", "id": "3f2504e0-4f89-11d3-9a0c-0305e82c3301", "params": {}},
        ]},
    }}
    ids = IdAllocator(tree)
    minted = {ids.alloc() for _ in range(5)}
    existing = {n["id"] for n in walk_nodes(tree)}
    assert not (minted & existing)


def test_ensure_does_not_consume_a_number_for_a_node_that_has_an_id():
    """setdefault(…, alloc()) would burn one here and shift every later id."""
    ids = IdAllocator({})
    assert ids.ensure({"type": "BoxFeature", "id": "keep-me"}) == "keep-me"
    assert ids.alloc() == "f1"


# -------------------------------------------------------------- mesh hashing --

def _box_tree(size: float) -> FeatureTree:
    return FeatureTree.from_dict({"version": "brep-v1", "root": {
        "type": "BoxFeature", "id": "f1", "name": "B",
        "params": {"size": {"x": size, "y": 0.05, "z": 0.05},
                   "center": {"x": 0, "y": 0, "z": 0}},
    }})


def test_the_same_tree_meshes_identically_twice():
    tree = _box_tree(0.05)
    a = Kernel.tessellate(Kernel.evaluate_tree(tree, resolution=32))
    b = Kernel.tessellate(Kernel.evaluate_tree(tree, resolution=32))
    assert mesh_hash(a) == mesh_hash(b)


def test_a_different_tree_meshes_differently():
    a = Kernel.tessellate(Kernel.evaluate_tree(_box_tree(0.05), resolution=32))
    b = Kernel.tessellate(Kernel.evaluate_tree(_box_tree(0.09), resolution=32))
    assert mesh_hash(a) != mesh_hash(b)


def test_resolution_changes_the_mesh_so_it_must_be_recorded():
    """Why DerivedCache carries `resolution`: it is part of the geometry's identity."""
    tree = _box_tree(0.05)
    coarse = Kernel.tessellate(Kernel.evaluate_tree(tree, resolution=32))
    fine = Kernel.tessellate(Kernel.evaluate_tree(tree, resolution=64))
    assert mesh_hash(coarse) != mesh_hash(fine)


def test_sampler_is_reported_and_is_one_of_the_two_known_paths():
    assert Kernel.sampler_for(_box_tree(0.05).root) in ("native", "numpy")


# ------------------------------------------------------------- replay parity --

_BUILD = [
    ("add_primitive", {"shape": "box", "params": {"size": {"x": 0.08, "y": 0.04, "z": 0.01}}}),
    ("add_primitive", {"shape": "cylinder", "operation": "subtract",
                       "params": {"radius": 0.004, "height": 0.05}}),
    ("add_modifier", {"modifier": "fillet", "params": {"radius": 0.002}}),
]


def _replay(steps):
    doc = docs.create_document(name="replay", resolution=32)
    for op, args in steps:
        ops.dispatch(op, doc.id, args)
    return docs.get_document(doc.id).feature_tree


def test_replaying_an_op_sequence_reproduces_the_tree_exactly():
    """The core promise: a logged trajectory replays to the same document.

    Ids are part of this — a trajectory whose later steps reference `f2` is only
    replayable if the second feature is called `f2` every time.
    """
    assert _replay(_BUILD) == _replay(_BUILD)


def test_replayed_documents_have_identical_geometry():
    first, second = _replay(_BUILD), _replay(_BUILD)
    meshes = [
        mesh_hash(Kernel.tessellate(Kernel.evaluate_tree(FeatureTree.from_dict(t), resolution=32)))
        for t in (first, second)
    ]
    assert meshes[0] == meshes[1]


def test_feature_ids_are_readable_sequence_numbers():
    """A trajectory is much easier to read (and to learn from) as f1/f2/f3."""
    tree = _replay(_BUILD)
    assert all(n["id"].startswith("f") for n in walk_nodes(tree))


def test_evaluation_records_the_conditions_that_produced_the_geometry():
    doc = docs.create_document(name="stamped", resolution=32)
    docs.evaluate_document(doc, resolution=32)
    assert doc.derived.resolution == 32
    assert doc.derived.sampler in ("native", "numpy")
    assert doc.derived.mesh_hash

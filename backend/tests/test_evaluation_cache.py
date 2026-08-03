"""Evaluating a subtree once instead of once per edit.

Every mutating operation used to rebuild the whole feature tree from the root,
and then rebuild it *again* the first time any topology operation asked for the
exact body. A subtree's shape is a pure function of that subtree, so it can be
keyed by content and kept — but a build cache is only worth having if a warm
build is indistinguishable from a cold one, and "indistinguishable" here means
three separate things: the same triangles, the same entity tags, and the same
reference verdicts. Those are what these tests pin.

The interesting failure is not a miss. It is a hit that returns something
subtly different from what a fresh build would have produced, because that
looks exactly like a working edit.
"""

import math

import numpy as np
import pytest

from app.cad_kernel.evaluator import brep
from app.cad_kernel.evaluator import sdf as sdf_module
from app.cad_kernel.geom import profile2d
from app.services import cad_operations as ops
from app.services import document_service as docs
from app.services import feature_ops

exact_only = pytest.mark.skipif(not brep.AVAILABLE, reason="needs OpenCascade")


def _plate_with_bosses(count: int = 8) -> str:
    doc_id = docs.create_document(name="cached").id
    ops.execute("add_primitive", doc_id, {
        "shape": "box", "params": {"size": {"x": 0.12, "y": 0.08, "z": 0.02}}})
    for i in range(count):
        ops.execute("add_primitive", doc_id, {"shape": "cylinder", "params": {
            "radius": 0.005, "height": 0.03,
            "center": {"x": -0.045 + 0.012 * i, "y": 0.0, "z": 0.01}}})
    return doc_id


def _leaves(doc_id):
    from app.cad_kernel.core.feature_tree import walk_nodes

    tree = docs.get_document(doc_id).feature_tree
    return [n for n in walk_nodes(tree) if not (n.get("params") or {}).get("children")]


def _mesh_hash(doc_id) -> str:
    return docs.get_document(doc_id).derived.mesh_hash


# --------------------------------------------------------- same answer --

@exact_only
def test_a_warm_build_gives_the_same_geometry_as_a_cold_one():
    doc_id = _plate_with_bosses()
    warm = _mesh_hash(doc_id)

    brep.clear_cache()
    doc = docs.get_document(doc_id)
    doc.derived.eval_key = None
    docs._SOLID_CACHE.pop(doc_id, None)
    docs.evaluate_document(doc, persist=False)
    assert doc.derived.mesh_hash == warm


@exact_only
def test_an_edit_and_its_reverse_land_back_on_the_same_body():
    """The cache-creep test.

    A cached shape is handed to a *different* parent boolean than the one it was
    built under. If any of that leaked back into the child — a widened tolerance,
    a stashed triangulation — the second time a document reached this state it
    would mesh differently, and nothing else in the system would notice.
    """
    doc_id = _plate_with_bosses(4)
    plate = _leaves(doc_id)[0]["id"]
    before = _mesh_hash(doc_id)

    ops.execute("set_feature_params", doc_id, {
        "feature_id": plate, "params": {"size": {"x": 0.2, "y": 0.08, "z": 0.02}}})
    assert _mesh_hash(doc_id) != before
    ops.execute("set_feature_params", doc_id, {
        "feature_id": plate, "params": {"size": {"x": 0.12, "y": 0.08, "z": 0.02}}})
    assert _mesh_hash(doc_id) == before


@exact_only
def test_tessellating_a_body_twice_gives_the_same_mesh():
    """Meshing is a read, and OCC does not treat it as one.

    `BRepMesh` widens edge tolerances to cover the deflection it achieved, and
    `BRepTools.Clean_s` does not put them back — so the second mesh of a body is
    built against a slightly different shape. Invisible while every build was
    discarded after one mesh; a `mesh_hash` that drifts for an untouched
    document as soon as bodies are shared.
    """
    doc_id = _plate_with_bosses(3)
    shape = docs.get_exact_shape(doc_id)
    assert shape is not None

    first = brep.tessellate(shape, resolution=64)
    second = brep.tessellate(shape, resolution=64)
    assert np.array_equal(first[0], second[0])
    assert np.array_equal(first[1], second[1])
    # And a coarser mesh in between must not contaminate the fine one either.
    brep.tessellate(shape, resolution=160)
    third = brep.tessellate(shape, resolution=64)
    assert np.array_equal(first[0], third[0])


# ------------------------------------------------------ what it reuses --

@exact_only
def test_editing_the_last_feature_reuses_the_boolean_prefix():
    """Twelve bodies fused is eleven booleans; editing the twelfth is one.

    Counted rather than timed: a timing assertion on a shared runner is a
    flaky test, and the claim is about work avoided, not milliseconds.
    """
    doc_id = _plate_with_bosses(8)
    leaves = _leaves(doc_id)
    calls = []
    real = brep.BRepAlgoAPI_Fuse

    def counted(a, b):
        calls.append(1)
        return real(a, b)

    doc = docs.get_document(doc_id)
    brep.BRepAlgoAPI_Fuse = counted
    try:
        doc.derived.eval_key = None
        docs.evaluate_document(doc, persist=False)
        cold = len(calls)          # already warm: nothing should run at all
        calls.clear()

        ops.execute("set_feature_params", doc_id, {
            "feature_id": leaves[-1]["id"], "params": {"radius": 0.0061}})
        last = len(calls)
        calls.clear()

        ops.execute("set_feature_params", doc_id, {
            "feature_id": leaves[1]["id"], "params": {"radius": 0.0062}})
        early = len(calls)
    finally:
        brep.BRepAlgoAPI_Fuse = real

    assert cold == 0, "re-evaluating an unchanged document rebuilt it"
    assert last == 1, f"editing the last body cost {last} booleans"
    # Editing an early one legitimately invalidates every prefix after it.
    assert early > last


@exact_only
def test_the_exact_body_is_not_rebuilt_after_the_display_build():
    """`evaluate_document` and `get_exact_body` used to build the same tree twice."""
    doc_id = _plate_with_bosses(6)
    docs._BREP_CACHE.clear()

    calls = []
    real = brep.BRepAlgoAPI_Fuse
    brep.BRepAlgoAPI_Fuse = lambda a, b: (calls.append(1), real(a, b))[1]
    try:
        assert docs.get_exact_body(doc_id) is not None
    finally:
        brep.BRepAlgoAPI_Fuse = real
    assert not calls


# ------------------------------------------- what has to come with it --

@exact_only
def test_a_cached_subtree_still_carries_its_entity_tags():
    """Without the tag map, `history.advance` sees an untagged input and the
    whole naming scheme quietly falls back to measuring."""
    doc_id = _plate_with_bosses(3)
    faces = ops.execute("list_faces", doc_id, {})
    assert faces["ok"] and faces["result"]["faces"]

    # A second evaluation, served entirely from the cache.
    docs._BREP_CACHE.clear()
    again = ops.execute("list_faces", doc_id, {})
    assert again["ok"]
    assert ([f["id"] for f in again["result"]["faces"]]
            == [f["id"] for f in faces["result"]["faces"]])


@exact_only
def test_a_cached_subtree_replays_the_reference_verdicts_it_filed():
    """`check_references` reads verdicts that are only knowable while the child
    body exists. A cached subtree never rebuilds it, so they come back with the
    entry or they do not come back at all."""
    doc_id = docs.create_document(name="filleted").id
    ops.execute("add_primitive", doc_id, {
        "shape": "box", "params": {"size": {"x": 0.05, "y": 0.05, "z": 0.02}}})
    edges = ops.execute("list_edges", doc_id, {})
    assert edges["ok"]
    picked = [edges["result"]["edges"][0]["id"]]
    assert ops.execute("fillet_edges", doc_id, {"edges": picked, "radius": 0.002})["ok"]

    cold = ops.execute("check_references", doc_id, {})
    docs._BREP_CACHE.clear()
    warm = ops.execute("check_references", doc_id, {})
    assert cold["ok"] and warm["ok"]
    assert warm["result"] == cold["result"]


@exact_only
def test_a_feature_that_will_not_serialise_is_simply_not_cached():
    """Fail open. A miss is slow; a stale hit is a document that stops meaning
    what it says."""

    class Unkeyable:
        id = "fX"

        def to_dict(self):
            raise TypeError("not serialisable")

    assert brep._subtree_key(Unkeyable()) is None


# ------------------------------------------------- the 2D profile field --

def _ring(count: int, radius: float, lobe: float = 0.0) -> dict:
    points = []
    for i in range(count):
        angle = 2 * math.pi * i / count
        r = radius * (1 + lobe * math.sin(7 * angle))
        points.append((r * math.cos(angle), r * math.sin(angle)))
    return {"type": "loops", "loops": [{"curves": [
        {"kind": "line", "a": list(points[i]), "b": list(points[(i + 1) % count])}
        for i in range(count)]}]}


@pytest.mark.parametrize("profile", [
    _ring(64, 0.04),
    _ring(300, 0.04, lobe=0.35),
    {"type": "loops", "loops": [
        {"curves": [{"kind": "circle", "center": [0.0, 0.0], "r": 0.04}]},
        {"curves": [{"kind": "circle", "center": [0.0, 0.0], "r": 0.02}]},
        {"curves": [{"kind": "arc", "a": [0.05, 0.0], "b": [0.0, 0.05],
                     "center": [0.0, 0.0], "ccw": True},
                    {"kind": "line", "a": [0.0, 0.05], "b": [0.05, 0.05]},
                    {"kind": "line", "a": [0.05, 0.05], "b": [0.05, 0.0]}]},
    ]},
], ids=["ring", "lobed", "mixed-kinds"])
def test_the_spatial_index_returns_exactly_the_swept_field(profile):
    """The index changes what is evaluated, never what is returned. Not
    `allclose` — the culled curves provably cannot be the nearest one and
    provably cannot cross the ray, so the answer is the same float."""
    grid = np.linspace(-0.07, 0.07, 128)
    u, v = np.meshgrid(grid, grid)
    curves = profile2d.curves_of(profile)
    swept = profile2d._swept_sdf(curves, u.ravel(), v.ravel())
    binned = profile2d._binned_sdf(curves, u.ravel(), v.ravel())
    assert np.array_equal(swept, binned)


def test_the_index_matches_the_per_loop_parity_it_replaced():
    """Parity is additive mod 2, so counting crossings over one flat list of
    curves is the same answer as taking each loop's parity and combining them.
    Worth pinning, because it is the step that lets the whole field be computed
    in one pass."""
    profile = {"type": "loops", "loops": [
        {"curves": [{"kind": "circle", "center": [0.0, 0.0], "r": 0.05}]},
        {"curves": [{"kind": "circle", "center": [0.0, 0.0], "r": 0.03}]},
        {"curves": [{"kind": "circle", "center": [0.0, 0.0], "r": 0.01}]},
    ]}
    grid = np.linspace(-0.06, 0.06, 61)
    u, v = np.meshgrid(grid, grid)
    u, v = u.ravel(), v.ravel()

    parity = np.zeros(u.shape, dtype=int)
    for loop in profile2d.loops_of(profile):
        parity += profile2d.loop_contains(loop, u, v).astype(int)
    per_loop = (parity % 2) == 1

    # `signbit`, not `< 0`: a sample landing exactly on a circle has distance
    # zero, and the inside branch returns -0.0, which is not less than zero.
    assert np.array_equal(np.signbit(profile2d.loops_sdf(profile, u, v)), per_loop)


def test_a_repeated_uv_pair_is_evaluated_once():
    """An extrude asks the same 2D question once per slice. The distinct pairs
    are what gets evaluated; the answer is indexed back out."""
    profile = _ring(120, 0.04)
    grid = np.linspace(-0.06, 0.06, 64)
    u2, v2 = np.meshgrid(grid, grid, indexing="ij")
    w = np.linspace(0, 0.01, 64)

    points = np.stack(np.meshgrid(grid, grid, w, indexing="ij"), axis=-1).reshape(-1, 3)
    assert points.shape[0] >= sdf_module._DEDUP_MIN_POINTS
    plan = sdf_module._grid_plan(points[:, 0], points[:, 1])
    assert plan is not None, "an axis-aligned sample grid was not recognised"

    full = sdf_module.profile_sdf_2d(profile, points[:, 0], points[:, 1])
    slice_field = sdf_module.profile_sdf_2d(profile, u2.ravel(), v2.ravel())
    assert np.array_equal(full.reshape(64, 64, 64)[:, :, 0], slice_field.reshape(64, 64))


def test_a_radius_field_is_not_deduplicated():
    """A revolve's radius is different at nearly every point. Paying for a
    two-million-element `np.unique` to learn that is the tax the probe exists to
    avoid, so the plan must decline rather than merely not help."""
    rng = np.random.default_rng(0)
    u = rng.random(200_000)
    v = np.repeat(np.linspace(0, 1, 100), 2000)
    assert sdf_module._grid_plan(u, v) is None


# ------------------------------------------------------ the copy chain --

def test_resolving_for_evaluation_matches_the_three_stages_it_replaces():
    doc_id = docs.create_document(name="stages").id
    ops.execute("add_primitive", doc_id, {
        "shape": "box", "params": {"size": {"x": 0.05, "y": 0.05, "z": 0.01}}})
    ops.execute("add_primitive", doc_id, {"shape": "sphere", "params": {"radius": 0.02}})
    tree = docs.get_document(doc_id).feature_tree
    leaves = _leaves(doc_id)
    ops.execute("suppress_feature", doc_id, {"feature_id": leaves[-1]["id"],
                                             "suppressed": True})
    tree = docs.get_document(doc_id).feature_tree

    staged = feature_ops.resolve_datums(
        feature_ops.resolve_sketches(feature_ops.resolve_suppressed(tree)))
    assert feature_ops.resolve_for_evaluation(tree) == staged


def test_resolving_for_evaluation_does_not_touch_the_document_tree():
    """One copy instead of three is still a copy — the stages mutate nodes in
    place, and the tree they are handed belongs to a live document."""
    doc_id = docs.create_document(name="untouched").id
    ops.execute("add_primitive", doc_id, {
        "shape": "box", "params": {"size": {"x": 0.05, "y": 0.05, "z": 0.01}}})
    tree = docs.get_document(doc_id).feature_tree
    import copy as _copy

    original = _copy.deepcopy(tree)
    feature_ops.resolve_for_evaluation(tree)
    assert tree == original

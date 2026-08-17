"""Patterns along a curve, and patterns from a table.

`PatternFeature` was linear or circular, and each kernel worked out its own
placements — the B-rep builder carried a comment saying it picks a single
dominant world axis for a circular pattern "because the SDF does, and matching
that is what keeps the two kernels in agreement", which is the comment you
write when the arithmetic lives in two places and has already disagreed once.

The placements now come from `geom/patterns.py` and both kernels read them, so
the first group of tests here is about the two original kinds landing in
*exactly* the same places they did before. The rest is the two new ones.
"""

import math

import numpy as np
import pytest

from app.cad_kernel.core.feature_tree import FeatureTree
from app.cad_kernel.evaluator import brep
from app.cad_kernel.geom import patterns
from app.cad_kernel.geom.meshing import mesh_volume, solid_to_mesh
from app.cad_kernel.kernel import Kernel
from app.services import cad_operations as ops
from app.services import document_service as docs

V = lambda x, y, z: {"x": x, "y": y, "z": z}  # noqa: E731

CUBE = {"type": "BoxFeature", "id": "c1",
        "params": {"size": V(.01, .01, .01), "center": V(0, 0, 0)}}


def _tree(**params) -> FeatureTree:
    merged = {"kind": "linear", "count": 3, "spacing": .03,
              "axis": V(1, 0, 0), "center": V(0, 0, 0), "children": [CUBE]}
    merged.update(params)
    return FeatureTree.from_dict(
        {"version": "pattern-v1", "root": {"type": "PatternFeature", "id": "p1",
                                           "params": merged}})


def _placements(**params):
    return patterns.placements(_tree(**params).root)


def _sdf_volume(tree, resolution=200) -> float:
    mesh = solid_to_mesh(Kernel.evaluate_tree(tree, resolution=resolution))
    return abs(mesh_volume(mesh.vertices, mesh.faces))


# ------------------------------------------- the two originals, unchanged --

def test_a_linear_pattern_still_steps_along_the_axis():
    places = _placements(kind="linear", count=4, spacing=.03, axis=V(1, 0, 0))
    assert len(places) == 4
    for i, (rotation, translate) in enumerate(places):
        assert np.allclose(rotation, np.eye(3))
        assert np.allclose(translate, [.03 * i, 0, 0])


def test_a_circular_pattern_still_turns_about_the_dominant_world_axis():
    """A single dominant axis rather than the vector as given is a real
    limitation, and it is the *original* behaviour. Using the vector properly
    would rotate every stored pattern whose axis was not exactly a world one —
    a body moving under a document nobody edited."""
    places = _placements(kind="circular", count=4, axis=V(.1, .1, 1),
                         center=V(0, 0, 0))
    assert len(places) == 4
    # Quarter turns about +Z: the seed's +X corner ends up on +Y.
    quarter = places[1][0]
    assert np.allclose(quarter @ np.array([1, 0, 0]), [0, 1, 0], atol=1e-12)


def test_a_circular_pattern_turns_about_its_centre_not_the_origin():
    places = _placements(kind="circular", count=4, axis=V(0, 0, 1),
                         center=V(.05, 0, 0))
    rotation, translate = places[1]
    seed = np.array([.05, 0, 0])
    assert np.allclose(rotation @ seed + translate, seed)     # the pivot is fixed


# ------------------------------------------------------------ along a curve --

def test_a_curve_pattern_spaces_instances_by_arc_length():
    """Not by point index: a polyline whose segments differ in length would
    otherwise bunch the copies up on the short one."""
    path = [V(0, 0, 0), V(.01, 0, 0), V(.10, 0, 0)]     # 10 mm then 90 mm
    places = _placements(kind="curve", count=3, path=path)
    assert np.allclose(places[0][1], [0, 0, 0])
    assert np.allclose(places[1][1], [.05, 0, 0])       # halfway *by length*
    assert np.allclose(places[2][1], [.10, 0, 0])


def test_a_curve_pattern_follows_a_bend():
    path = [V(0, 0, 0), V(.04, 0, 0), V(.04, .04, 0)]
    places = _placements(kind="curve", count=5, path=path)
    assert len(places) == 5
    assert np.allclose(places[-1][1], [.04, .04, 0])
    # Arc length 80 mm over 5 instances: the middle one is at 40 mm, the corner.
    assert np.allclose(places[2][1], [.04, 0, 0])


def test_curve_copies_keep_the_seeds_orientation():
    """Translation only, on purpose: bolt holes around an arc stay parallel to
    their axis rather than fanning out, and "follow the tangent" would need a
    second reference direction nobody could guess the meaning of."""
    for rotation, _ in _placements(kind="curve", count=4,
                                   path=[V(0, 0, 0), V(.04, .04, 0)]):
        assert np.allclose(rotation, np.eye(3))


def test_one_instance_on_a_curve_is_the_seed_and_nothing_else():
    places = _placements(kind="curve", count=1, path=[V(0, 0, 0), V(.04, 0, 0)])
    assert len(places) == 1 and np.allclose(places[0][1], [0, 0, 0])


@pytest.mark.parametrize("path, needle", [
    ([V(0, 0, 0)], "at least two points"),
    ([V(.01, 0, 0), V(.01, 0, 0)], "no length"),
])
def test_a_curve_pattern_with_no_curve_is_refused(path, needle):
    with pytest.raises(ValueError, match=needle):
        _placements(kind="curve", count=3, path=path)


# -------------------------------------------------------------- from a table --

def test_a_table_pattern_places_each_instance_where_it_says():
    places = _placements(kind="table", transforms=[
        {"translate": V(0, 0, 0)},
        {"translate": V(.05, 0, 0)},
        {"translate": V(0, .05, 0), "rotate": V(0, 0, 90)}])
    assert len(places) == 3
    assert np.allclose(places[1][1], [.05, 0, 0])
    assert np.allclose(places[2][0] @ np.array([1, 0, 0]), [0, 1, 0], atol=1e-12)


def test_a_table_pattern_has_no_implicit_copy_at_the_origin():
    """The list is complete. An implicit seed would take explicit control away
    from the one kind that exists to provide it."""
    places = _placements(kind="table", transforms=[{"translate": V(.05, 0, 0)}])
    assert len(places) == 1
    assert np.allclose(places[0][1], [.05, 0, 0])


def test_an_empty_table_is_refused_rather_than_silently_building_nothing():
    with pytest.raises(ValueError, match="at least one entry"):
        _placements(kind="table", transforms=[])


def test_count_is_ignored_by_a_table_pattern():
    """The transforms are the instances; a `count` that disagreed with the list
    would be a second source of truth for the same number."""
    places = _placements(kind="table", count=99,
                         transforms=[{"translate": V(0, 0, 0)},
                                     {"translate": V(.05, 0, 0)}])
    assert len(places) == 2


# --------------------------------------------------- both kernels, all kinds --

def _rotation_roundtrip(rotation):
    axis, angle = patterns.axis_and_angle(rotation)
    v = np.asarray(axis, dtype=float)
    k = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np.eye(3) + math.sin(angle) * k + (1 - math.cos(angle)) * (k @ k)


@pytest.mark.parametrize("degrees", [0, 30, 90, 120, 180, 270])
def test_a_rotation_survives_the_trip_through_axis_and_angle(degrees):
    """OCC wants an axis and an angle; the placement is a matrix. Rebuilding
    the transform from the angles instead would be a second derivation, and 180
    degrees is where the naive skew-vector formula divides by zero."""
    from app.cad_kernel.evaluator.sdf import _euler_matrix

    rotation = _euler_matrix(0, 0, degrees)
    assert np.allclose(_rotation_roundtrip(rotation), rotation, atol=1e-9)


@pytest.mark.skipif(not brep.AVAILABLE, reason="OCP (OpenCascade) not installed")
@pytest.mark.parametrize("params, ident", [
    ({"kind": "linear", "count": 3, "spacing": .03, "axis": V(1, 0, 0)}, "linear"),
    ({"kind": "circular", "count": 4, "axis": V(0, 0, 1), "center": V(.03, 0, 0)}, "circular"),
    ({"kind": "curve", "count": 4, "path": [V(0, 0, 0), V(.05, 0, 0), V(.05, .05, 0)]}, "curve"),
    ({"kind": "table", "transforms": [{"translate": V(0, 0, 0)},
                                      {"translate": V(.04, 0, 0), "rotate": V(0, 0, 45)},
                                      {"translate": V(0, .04, .02)}]}, "table"),
])
def test_both_kernels_place_the_copies_identically(params, ident):
    tree = _tree(**params)
    shape = brep.brep_from_feature(tree.root)
    assert shape is not None and brep.is_valid(shape)
    assert brep.volume(shape) == pytest.approx(_sdf_volume(tree), rel=.02)


@pytest.mark.skipif(not brep.AVAILABLE, reason="OCP (OpenCascade) not installed")
def test_disjoint_copies_sum_to_the_seeds_volume_times_the_count():
    """Placed far enough apart that nothing overlaps, so the arithmetic is
    exact and a copy landing on top of another would show immediately."""
    shape = brep.brep_from_feature(
        _tree(kind="curve", count=4,
              path=[V(0, 0, 0), V(.09, 0, 0)]).root)
    assert brep.volume(shape) == pytest.approx(4 * .01 ** 3, rel=1e-6)


# ------------------------------------------------------------- the operation --

def _cube_doc():
    doc = docs.create_document(name="pattern", owner_id="dev")
    ops.dispatch("add_primitive", doc.id,
                 {"shape": "box", "params": {"size": V(.01, .01, .01),
                                             "center": V(0, 0, 0)}})
    return doc.id


def test_add_modifier_builds_a_curve_pattern():
    doc_id = _cube_doc()
    before = ops.dispatch("mass_properties", doc_id, {})["volume_cm3"]
    ops.dispatch("add_modifier", doc_id, {
        "modifier": "pattern",
        "params": {"kind": "curve", "count": 4,
                   "path": [V(0, 0, 0), V(.09, 0, 0)]}})
    after = ops.dispatch("mass_properties", doc_id, {})["volume_cm3"]
    assert after == pytest.approx(4 * before, rel=1e-4)


def test_add_modifier_builds_a_table_pattern():
    doc_id = _cube_doc()
    before = ops.dispatch("mass_properties", doc_id, {})["volume_cm3"]
    ops.dispatch("add_modifier", doc_id, {
        "modifier": "pattern",
        "params": {"kind": "table",
                   "transforms": [{"translate": V(0, 0, 0)},
                                  {"translate": V(.05, 0, 0)},
                                  {"translate": V(0, .05, 0)}]}})
    after = ops.dispatch("mass_properties", doc_id, {})["volume_cm3"]
    assert after == pytest.approx(3 * before, rel=1e-4)


def test_an_unbuildable_pattern_is_a_warning_not_a_crash():
    """A tree that does not evaluate is `derived.error`, not a wedged document
    — the standing contract, and a pattern with no path is a way to reach it."""
    doc_id = _cube_doc()
    envelope = ops.execute("add_modifier", doc_id, {
        "modifier": "pattern", "params": {"kind": "curve", "count": 3, "path": []}})
    assert envelope["ok"] is True
    assert envelope["warnings"], "expected the evaluation failure to be reported"


def test_the_decompiler_recovers_a_table_pattern():
    """The kind that exists so an arbitrary arrangement is recoverable at all."""
    from app.services import decompiler

    doc_id = _cube_doc()
    ops.dispatch("add_modifier", doc_id, {
        "modifier": "pattern",
        "params": {"kind": "table",
                   "transforms": [{"translate": V(0, 0, 0)},
                                  {"translate": V(.05, 0, 0)}]}})
    recovered = decompiler.decompile_document(doc_id, record=False)
    assert recovered["ok"], recovered.get("reason")
    step = next(s for s in recovered["steps"]
                if s["args"].get("modifier") == "pattern")
    assert len(step["args"]["params"]["transforms"]) == 2

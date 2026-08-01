"""Sketch profiles as loops of typed curves, and the two kernels agreeing on them.

A sketch used to be flattened to a ring of points before either kernel saw it,
which made it the one path in the system that *downgrades* geometry on its way
in: circles could not be extruded at all, and an arc arrived as 24 planar faces
that nothing counting cylindrical faces could see.

The tests here are mostly differential, for the same reason `test_brep_kernel`
is: containment is decided in one place (`geom.profile2d`) precisely so the SDF
and the B-rep cannot describe different bodies, and the way to know that held is
to build both and compare.
"""

import math

import numpy as np
import pytest

from app.cad_kernel.core.feature_tree import FeatureTree
from app.cad_kernel.evaluator import brep
from app.cad_kernel.evaluator.sdf import evaluate_sdf, sdf_from_feature
from app.cad_kernel.geom import profile2d
from app.cad_kernel.geom.meshing import mesh_volume
from app.cad_kernel.kernel import Kernel
from app.services import cad_operations as ops
from app.services import document_service as docs
from app.services import sketch_solver

# ------------------------------------------------------------------ sketches --

RECT = {
    "points": [{"id": "p1", "x": -0.03, "y": -0.02}, {"id": "p2", "x": 0.03, "y": -0.02},
               {"id": "p3", "x": 0.03, "y": 0.02}, {"id": "p4", "x": -0.03, "y": 0.02}],
    "lines": [{"id": "l1", "a": "p1", "b": "p2"}, {"id": "l2", "a": "p2", "b": "p3"},
              {"id": "l3", "a": "p3", "b": "p4"}, {"id": "l4", "a": "p4", "b": "p1"}],
    "constraints": [],
}

CIRCLE_ONLY = {
    "points": [{"id": "c", "x": 0.0, "y": 0.0}],
    "circles": [{"id": "k", "center": "c", "radius": 0.02}],
    "constraints": [],
}


def _with(base: dict, **extra) -> dict:
    spec = {k: list(v) if isinstance(v, list) else v for k, v in base.items()}
    for key, value in extra.items():
        spec[key] = spec.get(key, []) + value if isinstance(value, list) else value
    return spec


HOLED = _with(RECT, points=[{"id": "c", "x": 0.0, "y": 0.0}],
              circles=[{"id": "k", "center": "c", "radius": 0.01}])

ISLAND = _with(RECT, points=[{"id": "c", "x": 0.0, "y": 0.0}],
               circles=[{"id": "k", "center": "c", "radius": 0.015},
                        {"id": "k2", "center": "c", "radius": 0.005}])

# A 60x40 plate with one 10 mm-radius corner rounded off.
FILLETED = {
    "points": [{"id": "p1", "x": -0.03, "y": -0.02}, {"id": "p2", "x": 0.03, "y": -0.02},
               {"id": "p3", "x": 0.03, "y": 0.01}, {"id": "p4", "x": 0.02, "y": 0.02},
               {"id": "p5", "x": -0.03, "y": 0.02}, {"id": "cc", "x": 0.02, "y": 0.01}],
    "lines": [{"id": "l1", "a": "p1", "b": "p2"}, {"id": "l2", "a": "p2", "b": "p3"},
              {"id": "l3", "a": "p4", "b": "p5"}, {"id": "l4", "a": "p5", "b": "p1"}],
    "arcs": [{"id": "a1", "a": "p3", "b": "p4", "center": "cc", "ccw": True}],
    "constraints": [],
}


def _profile(spec: dict) -> dict:
    return sketch_solver.sketch_to_loops(spec, sketch_solver.solve_sketch(spec))


def _sketch_doc(spec: dict, depth: float = 0.01, name: str = "sketch"):
    doc_id = docs.create_document(name=name).id
    result = ops.execute("add_sketch", doc_id, {"sketch": spec, "depth": depth})
    assert result["ok"], result["error"]
    return doc_id


def _root(doc_id: str):
    return FeatureTree.from_dict(docs.resolved_tree(docs.get_document(doc_id))).root


def _sdf_volume_mm3(root, resolution: int = 200) -> float:
    mesh = Kernel.tessellate(evaluate_sdf(sdf_from_feature(root), resolution=resolution))
    return abs(mesh_volume(mesh.vertices, mesh.faces)) * 1e9


# ------------------------------------------------------------ solver output --

def test_a_circle_only_sketch_becomes_a_profile():
    """The plainest sketch anyone draws used to raise "no lines or arcs"."""
    profile = _profile(CIRCLE_ONLY)
    assert profile["type"] == "loops"
    assert profile2d.kinds(profile) == ["circle"]
    assert profile["loops"][0]["curves"][0]["r"] == pytest.approx(0.02)


def test_an_arc_stays_an_arc():
    kinds = profile2d.kinds(_profile(FILLETED))
    assert kinds.count("arc") == 1
    assert kinds.count("line") == 4


def test_every_loop_is_walked_not_just_the_first():
    profile = _profile(HOLED)
    assert len(profile["loops"]) == 2
    assert profile2d.nesting(profile) == [0, 1]


def test_a_sketch_with_nothing_closed_is_refused():
    open_chain = {"points": [{"id": "a", "x": 0.0, "y": 0.0}, {"id": "b", "x": 0.01, "y": 0.0}],
                  "lines": [{"id": "l", "a": "a", "b": "b"}], "constraints": []}
    profile = _profile(open_chain)
    # An open chain still produces a loop entry (the walk records what it found);
    # what must not happen is a silent empty profile.
    assert profile["loops"]

    with pytest.raises(sketch_solver.SketchError):
        _profile({"points": [{"id": "a", "x": 0.0, "y": 0.0}], "constraints": []})


def test_the_traversal_direction_of_an_arc_is_preserved():
    """An arc stored b->a sweeps the other way round, and the walk must say so."""
    reversed_arc = dict(FILLETED)
    reversed_arc["arcs"] = [{"id": "a1", "a": "p4", "b": "p3", "center": "cc", "ccw": False}]
    forward = _profile(FILLETED)
    backward = _profile(reversed_arc)
    assert profile2d.area(forward) == pytest.approx(profile2d.area(backward), rel=1e-6)


# ----------------------------------------------------------- profile2d maths --

def test_even_odd_containment_nests_to_any_depth():
    profile = _profile(ISLAND)
    assert sorted(profile2d.nesting(profile)) == [0, 1, 2]
    faces = profile2d.faces_of(profile)
    # The plate with its bore, and the island inside the bore, are two faces.
    assert len(faces) == 2
    assert sum(len(f["holes"]) for f in faces) == 1


def test_the_field_is_negative_inside_and_positive_outside():
    profile = _profile(HOLED)
    u = np.array([0.0, 0.02, 0.05])   # in the bore, in the material, off the plate
    v = np.array([0.0, 0.0, 0.0])
    d = profile2d.loops_sdf(profile, u, v)
    assert d[0] > 0 and d[1] < 0 and d[2] > 0
    # Distance is measured, not signed-only: the bore's centre is r from its wall.
    assert d[0] == pytest.approx(0.01, abs=1e-9)


def test_bounds_cover_an_arc_bulge_not_just_its_chord():
    """A box around the chord is not a box around the arc."""
    half = {"loops": [{"curves": [
        {"kind": "arc", "a": [0.01, 0.0], "b": [-0.01, 0.0], "center": [0.0, 0.0], "ccw": True},
        {"kind": "line", "a": [-0.01, 0.0], "b": [0.01, 0.0]}]}], "type": "loops"}
    umin, vmin, umax, vmax = profile2d.loops_bounds(half)
    assert (umin, umax) == pytest.approx((-0.01, 0.01))
    assert vmax == pytest.approx(0.01)   # the top of the arc, not the chord
    assert vmin == pytest.approx(0.0)


def test_area_subtracts_holes_and_adds_islands_back():
    # `area` tessellates, so a curved loop is an inscribed polygon and comes out
    # a fraction small. It is a sanity check, not a measurement — the exact
    # figures come off the B-rep below.
    assert profile2d.area(_profile(RECT)) == pytest.approx(0.06 * 0.04, rel=1e-6)
    assert profile2d.area(_profile(HOLED)) == pytest.approx(
        0.06 * 0.04 - math.pi * 0.01 ** 2, rel=3e-3)
    assert profile2d.area(_profile(ISLAND)) == pytest.approx(
        0.06 * 0.04 - math.pi * 0.015 ** 2 + math.pi * 0.005 ** 2, rel=3e-3)


def test_construction_geometry_constrains_but_is_not_extruded():
    """A centreline or a bolt circle is there to measure from, not to extrude."""
    with_axis = _with(RECT,
                      points=[{"id": "m1", "x": 0.0, "y": -0.02},
                              {"id": "m2", "x": 0.0, "y": 0.02}],
                      lines=[{"id": "axis", "a": "m1", "b": "m2", "construction": True}])
    with_axis["constraints"] = [{"type": "vertical", "line": "axis"}]

    profile = _profile(with_axis)
    assert len(profile["loops"]) == 1, "the construction line is not a second loop"
    assert profile2d.curve_count(profile) == 4
    # It still took part in the solve — an unknown line would have raised.
    assert sketch_solver.solve_sketch(with_axis)["status"] == "under_constrained"


def test_a_construction_circle_is_not_a_hole():
    bolt_circle = _with(RECT, points=[{"id": "c", "x": 0.0, "y": 0.0}],
                        circles=[{"id": "bc", "center": "c", "radius": 0.015,
                                  "construction": True}])
    profile = _profile(bolt_circle)
    assert profile2d.kinds(profile) == ["line"] * 4


# ------------------------------------------------------------ back-compat --

def test_a_persisted_polygon_profile_still_means_what_it_meant():
    """`{"type": "polygon"}` is on disk in documents; it must not change shape.

    Same reason `LoftFeature` was left alone when `RuledLoftFeature` was added:
    a document that evaluates to a 24-gon today must not silently become a true
    arc because the code around it learned how.
    """
    solved = sketch_solver.solve_sketch(FILLETED)
    polygon = sketch_solver.sketch_to_polygon(FILLETED, solved)
    assert len(polygon) == 4 + 24            # four line ends, 24 arc segments
    assert all(len(p) == 2 for p in polygon)

    doc_id = docs.create_document(name="legacy").id
    tree = {"version": "brep-v1", "root": {
        "type": "ExtrudeFeature", "id": "f1", "name": "Legacy",
        "params": {"profile": {"type": "polygon", "points": polygon},
                   "plane": "xy", "offset": 0.0, "depth": 0.01}}}
    docs.update_document(doc_id, feature_tree=tree)
    faces = ops.execute("list_faces", doc_id, {})
    assert faces["ok"], faces["error"]
    # Still faceted, deliberately: 24 arc segments are 24 planar side faces.
    assert not [f for f in faces["result"]["faces"] if f["kind"] == "cylinder"]


# ----------------------------------------------------- the two kernels agree --

pytestmark_brep = pytest.mark.skipif(not brep.AVAILABLE, reason="OCP not installed")


@pytestmark_brep
@pytest.mark.parametrize("spec,expected_mm3,cylinders", [
    (CIRCLE_ONLY, math.pi * 20 * 20 * 10, [20.0]),
    (HOLED, (60 * 40 - math.pi * 100) * 10, [10.0]),
    (ISLAND, (60 * 40 - math.pi * 225 + math.pi * 25) * 10, [5.0, 15.0]),
    (FILLETED, (60 * 40 - (100 - math.pi * 25)) * 10, [10.0]),
], ids=["circle", "holed", "island", "filleted"])
def test_the_exact_kernel_builds_the_profile_exactly(spec, expected_mm3, cylinders):
    root = _root(_sketch_doc(spec))
    shape = brep.brep_from_feature(root)
    assert shape is not None, "a loops profile must reach the exact kernel"
    assert brep.volume(shape) * 1e9 == pytest.approx(expected_mm3, rel=1e-9)


@pytestmark_brep
@pytest.mark.parametrize("spec,expected_mm3", [
    (CIRCLE_ONLY, math.pi * 20 * 20 * 10),
    (HOLED, (60 * 40 - math.pi * 100) * 10),
    (ISLAND, (60 * 40 - math.pi * 225 + math.pi * 25) * 10),
    (FILLETED, (60 * 40 - (100 - math.pi * 25)) * 10),
], ids=["circle", "holed", "island", "filleted"])
def test_the_distance_field_describes_the_same_body(spec, expected_mm3):
    """Both kernels or neither — a loops profile only the B-rep understood would
    make the document mean two things depending on whether OCP is installed."""
    root = _root(_sketch_doc(spec))
    assert _sdf_volume_mm3(root) == pytest.approx(expected_mm3, rel=2e-3)


@pytestmark_brep
@pytest.mark.parametrize("spec,radii", [
    (CIRCLE_ONLY, [20.0]),
    (HOLED, [10.0]),
    (ISLAND, [5.0, 15.0]),
    (FILLETED, [10.0]),
], ids=["circle", "holed", "island", "filleted"])
def test_a_sketched_curve_is_an_addressable_cylindrical_face(spec, radii):
    """The whole point: 24 planes could not be filleted, measured or specced."""
    result = ops.execute("list_faces", _sketch_doc(spec), {})
    assert result["ok"], result["error"]
    found = sorted(f["radius_mm"] for f in result["result"]["faces"]
                   if f["kind"] == "cylinder")
    assert found == pytest.approx(radii)


@pytestmark_brep
def test_a_filleted_corner_is_six_planes_and_a_cylinder_not_twenty_nine_planes():
    result = ops.execute("list_faces", _sketch_doc(FILLETED), {})["result"]
    kinds = sorted(f["kind"] for f in result["faces"])
    assert kinds == ["cylinder"] + ["plane"] * 6


@pytestmark_brep
def test_a_sketched_bore_satisfies_a_hole_requirement():
    """`design_spec._c_hole` counts cylindrical faces on the body, so a sketched
    bore was invisible to it while sketches were tessellated."""
    doc_id = _sketch_doc(HOLED)
    assert ops.execute("set_spec", doc_id,
                       {"requirements": [{"kind": "hole", "diameter": 20, "count": 1}]})["ok"]
    report = ops.execute("verify_against_spec", doc_id, {})["result"]
    assert report["score"] == 1.0, report["checks"]


@pytestmark_brep
def test_a_sketched_bore_can_be_filleted_by_name():
    """A bore made of 24 planes has 24 face ids and no edge worth naming."""
    doc_id = _sketch_doc(HOLED)
    edges = ops.execute("list_edges", doc_id, {})["result"]["edges"]
    circular = [e["id"] for e in edges if e["kind"] == "circle"]
    assert len(circular) == 2, "the bore's two rims"
    result = ops.execute("fillet_edges", doc_id, {"edges": circular, "radius": 0.001})
    assert result["ok"], result["error"]

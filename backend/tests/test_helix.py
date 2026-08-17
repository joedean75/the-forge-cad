"""Helices — coil springs, auger flights, wound wire.

Nothing here could make one before, which is why springs were absent from the
catalog entirely. `SweepFeature` is a union of *capsules* along a polyline, so a
helix built that way is faceted at whatever segment count somebody picked, and
the facets show at any count a document can afford.

**A note for anyone verifying a swept body here.** `Bnd_Box` on the result
bounds the *control points* of the swept BSpline surface, not the surface, and
reports a 10 mm coil as 60 mm across. Every assertion below measures the
tessellation instead. Two hours went into chasing a sweep that was correct all
along, which is the whole reason this paragraph exists.
"""

import math

import numpy as np
import pytest

from app.cad_kernel.core.feature_tree import FeatureTree
from app.cad_kernel.evaluator import brep
from app.services import cad_operations as ops
from app.services import document_service as docs

V = lambda x, y, z: {"x": x, "y": y, "z": z}  # noqa: E731

pytestmark = pytest.mark.skipif(not brep.AVAILABLE, reason="OCP (OpenCascade) not installed")


def _helix(**params):
    merged = {"helix_radius": .010, "wire_radius": .0012, "pitch": .004, "turns": 6.0,
              "center": V(0, 0, 0), "axis": V(0, 0, 1), "right_handed": True}
    merged.update(params)
    tree = FeatureTree.from_dict({"version": "helix-v1", "root": {
        "type": "HelixFeature", "id": "h1", "params": merged}})
    shape = brep.brep_from_feature(tree.root)
    assert shape is not None, "expected an exact form"
    return shape


def _points(shape) -> np.ndarray:
    vertices, _ = brep.tessellate(shape, resolution=128)
    return np.asarray(vertices)


def _wire_length(radius: float, pitch: float, turns: float) -> float:
    return turns * math.hypot(2.0 * math.pi * radius, pitch)


# ------------------------------------------------------------- the geometry --

def test_a_coils_volume_matches_the_wire_it_is_wound_from():
    """A torus-like sweep has no shortcut: the volume is the section area times
    the centreline length, and the centreline of a helix has a closed form."""
    shape = _helix()
    expected = math.pi * .0012 ** 2 * _wire_length(.010, .004, 6.0)
    assert brep.volume(shape) == pytest.approx(expected, rel=.001)


def test_the_coil_stays_on_its_radius():
    """The measurement that says the wire is actually wound rather than
    spiralling off — which is what every sweep mode except Frenet did."""
    points = _points(_helix())
    radial = np.hypot(points[:, 0], points[:, 1])
    assert radial.min() == pytest.approx(.010 - .0012, abs=5e-5)
    assert radial.max() == pytest.approx(.010 + .0012, abs=5e-5)


def test_free_length_is_pitch_times_turns():
    points = _points(_helix(turns=6.0, pitch=.004))
    assert points[:, 2].max() - points[:, 2].min() == pytest.approx(
        .004 * 6.0 + 2 * .0012, abs=2e-4)


def test_a_fractional_turn_count_is_honoured():
    half = _helix(turns=0.5)
    full = _helix(turns=1.0)
    assert brep.volume(full) == pytest.approx(2 * brep.volume(half), rel=.01)


def test_handedness_mirrors_the_coil_without_changing_its_size():
    right, left = _helix(right_handed=True), _helix(right_handed=False)
    assert brep.volume(left) == pytest.approx(brep.volume(right), rel=1e-6)
    # A right-handed coil rises as it turns anticlockwise; a left-handed one
    # reaches the same height the other way round, so the first quarter turn
    # lands on the opposite side.
    quarter_r = _points(_helix(turns=0.25, right_handed=True))
    quarter_l = _points(_helix(turns=0.25, right_handed=False))
    assert quarter_r[:, 1].max() > 0 > quarter_l[:, 1].min() + .0001
    assert np.sign(quarter_r[:, 1].mean()) == -np.sign(quarter_l[:, 1].mean())


def test_a_helix_can_be_wound_about_any_axis():
    """The start point and tangent are read off the frame rather than assumed:
    `gp_Ax3` picks its own X for an arbitrary axis, so a hardcoded (R, 0, 0)
    would only ever have been right for +Z."""
    shape = _helix(axis=V(1, 0, 0), center=V(0, 0, 0))
    points = _points(shape)
    radial = np.hypot(points[:, 1], points[:, 2])     # about the X axis now
    assert radial.max() == pytest.approx(.010 + .0012, abs=5e-5)
    assert points[:, 0].max() - points[:, 0].min() == pytest.approx(
        .004 * 6.0 + 2 * .0012, abs=2e-4)


def test_the_coil_is_a_valid_solid_that_booleans():
    """The point of `MakeSolid` — an open shell measures a plausible volume and
    then fails the first time anything tries to cut it."""
    shape = _helix()
    assert brep.is_valid(shape)
    tree = FeatureTree.from_dict({"version": "v", "root": {
        "type": "BooleanFeature", "id": "b", "params": {"operation": "union", "children": [
            {"type": "HelixFeature", "id": "h", "params": {
                "helix_radius": .010, "wire_radius": .0012, "pitch": .004, "turns": 3.0,
                "center": V(0, 0, 0), "axis": V(0, 0, 1), "right_handed": True}},
            {"type": "CylinderFeature", "id": "c", "params": {
                "radius": .004, "height": .014, "center": V(0, 0, .006), "axis": V(0, 0, 1)}}]}}})
    fused = brep.brep_from_feature(tree.root)
    assert fused is not None and brep.is_valid(fused)


# --------------------------------------------------- geometry that cannot be --

@pytest.mark.parametrize("params, needle", [
    ({"wire_radius": .012, "helix_radius": .010}, "through the axis"),
    ({"pitch": .002, "wire_radius": .0012}, "consecutive turns"),
    ({"turns": 0}, "positive radius"),
])
def test_impossible_coils_are_refused_with_the_reason(params, needle):
    """Each of these produces a self-intersecting sweep that OCC will either
    build wrong or fail on much later with nothing pointing at the cause."""
    from app.cad_kernel.ops.exact import RequiresExactKernel  # noqa: F401

    tree = FeatureTree.from_dict({"version": "v", "root": {
        "type": "HelixFeature", "id": "h", "params": {
            "helix_radius": .010, "wire_radius": .0012, "pitch": .004, "turns": 6.0,
            **params}}})
    with pytest.raises(brep.BrepUnsupported, match=needle):
        brep.build_strict(tree.root)


# ------------------------------------------------------------ the operation --

def _doc():
    return docs.create_document(name="spring", owner_id="dev").id


def test_add_helix_builds_a_spring_and_reports_its_free_length():
    result = ops.dispatch("add_helix", _doc(),
                          {"helix_radius": .010, "wire_radius": .0012,
                           "pitch": .004, "turns": 6.0})
    assert result["free_length_mm"] == pytest.approx(24.0)


def test_add_helix_refuses_a_pitch_the_wire_will_not_fit_in():
    envelope = ops.execute("add_helix", _doc(),
                           {"wire_radius": .002, "pitch": .003})
    assert envelope["ok"] is False
    assert envelope["error"]["arg"] == "pitch"


def test_add_helix_refuses_wire_thicker_than_the_coil():
    envelope = ops.execute("add_helix", _doc(),
                           {"helix_radius": .002, "wire_radius": .003, "pitch": .01})
    assert envelope["ok"] is False
    assert envelope["error"]["arg"] == "wire_radius"


def test_a_helix_needs_the_exact_kernel_and_says_which_operation_failed(monkeypatch):
    from app.cad_kernel.evaluator import sdf

    tree = FeatureTree.from_dict({"version": "v", "root": {
        "type": "HelixFeature", "id": "h", "params": {}}})
    with pytest.raises(Exception, match="(?i)exact"):
        sdf.sdf_from_feature(tree.root)
    assert "HelixFeature" in brep.EXACT_ONLY


def test_the_decompiler_recovers_a_helix():
    from app.services import decompiler

    doc_id = _doc()
    ops.dispatch("add_helix", doc_id, {"helix_radius": .012, "wire_radius": .001,
                                       "pitch": .005, "turns": 4.0,
                                       "right_handed": False})
    recovered = decompiler.decompile_document(doc_id, record=False)
    assert recovered["ok"], recovered.get("reason")
    helix = next(s for s in recovered["steps"] if s["op"] == "add_helix")
    assert helix["args"]["turns"] == pytest.approx(4.0)
    assert helix["args"]["right_handed"] is False

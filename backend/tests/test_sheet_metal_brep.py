"""Sheet metal on the exact kernel.

A bracket was the last common part with no addressable topology: it evaluated
only as a distance field, so no face could be selected, its dimensions were
exact to a voxel, and it exported as triangles. The bend model is unchanged —
`flat_pattern` is analytic and never touched geometry — so what this adds is the
body, not the maths.

The tests that matter are the ones asserting the exact build is the *same shape*
the field describes. A translation that quietly rounded a corner or mitred a
joint would look fine and unfold into the wrong blank.
"""

import math

import pytest

from app.cad_kernel.evaluator import brep
from app.cad_kernel.ops.sheet_metal import SheetMetalFeature
from app.services import cad_operations as ops
from app.services import document_service as docs

pytestmark = pytest.mark.skipif(
    not brep.AVAILABLE, reason="exact sheet metal needs cadquery-ocp")

_T, _L, _W, _R = 0.0015, 0.10, 0.06, 0.002


def _feature(flanges=()):
    return SheetMetalFeature(thickness=_T, length=_L, width=_W, bend_radius=_R,
                             flanges=list(flanges))


def _flange(edge="x+", angle=90.0, length=0.02, radius=_R):
    return {"edge": edge, "length": length, "angle": angle, "radius": radius}


def _occ_mm3(feature) -> float:
    shape = brep.brep_from_feature(feature)
    assert shape is not None, "the exact kernel declined a sheet-metal body"
    assert brep.is_valid(shape)
    return brep.volume(shape) * 1e9


def _sdf_mm3(feature) -> float:
    import numpy as np

    from app.cad_kernel.evaluator.sdf import evaluate_sdf, sdf_from_feature
    from app.cad_kernel.geom.meshing import mesh_volume, solid_to_mesh

    mesh = solid_to_mesh(evaluate_sdf(sdf_from_feature(feature), resolution=200))
    return abs(mesh_volume(np.asarray(mesh.vertices), np.asarray(mesh.faces))) * 1e9


def test_a_plain_blank_is_just_the_plate():
    assert _occ_mm3(_feature()) == pytest.approx(100 * 60 * 1.5, abs=1e-6)


def test_a_bend_has_the_volume_the_geometry_implies():
    """Plate + a quarter annulus across the width + the straight leg.

    Worked out on paper rather than recorded from a run, so the test fails if
    the bend is built as anything other than the sector the field defines.
    """
    leg = 0.020
    quarter = 0.25 * math.pi * ((_R + _T) ** 2 - _R ** 2) * _W
    expected = (_L * _W * _T + quarter + leg * _T * _W) * 1e9

    assert _occ_mm3(_feature([_flange(length=leg)])) == pytest.approx(expected, rel=1e-9)


@pytest.mark.parametrize("name,flanges", [
    ("one 90° up", [_flange("x+", 90.0)]),
    ("two opposed", [_flange("x+", 90.0), _flange("x-", -90.0)]),
    ("all four edges", [_flange(e, 90.0, 0.015) for e in ("x+", "x-", "y+", "y-")]),
    ("shallow 45°", [_flange("y+", 45.0, 0.02, 0.003)]),
    ("135° down", [_flange("y-", -135.0)]),
])
def test_the_exact_body_is_the_body_the_field_describes(name, flanges):
    """The point of the whole exercise. The two kernels must agree on the shape;
    only their resolution differs — 200³ voxels on a 1.5 mm sheet is where the
    remaining fraction of a percent comes from."""
    feature = _feature(flanges)

    assert _occ_mm3(feature) == pytest.approx(_sdf_mm3(feature), rel=0.01)


def test_a_sharp_bend_is_legal():
    """Zero inside radius collapses the inner arc to a point. Building the arc
    anyway hands OCC two coincident endpoints and the wire fails to close."""
    assert _occ_mm3(_feature([_flange("y+", 45.0, 0.02, 0.0)])) > 0.0


# ── through the document ──

def _bracket(*flanges):
    doc = docs.create_document(name="Bracket", resolution=64, owner_id=None)
    ops.dispatch("add_sheet_metal", doc.id,
                 {"thickness": _T, "length": _L, "width": _W, "bend_radius": _R})
    for f in flanges:
        ops.dispatch("add_flange", doc.id, f)
    return doc.id


def test_a_bracket_evaluates_through_the_exact_kernel():
    doc_id = _bracket({"edge": "x+", "length": 0.02, "angle": 90.0})

    assert docs.get_document(doc_id).derived.sampler == "brep"
    assert docs.get_document(doc_id).derived.error is None


def test_a_bracket_has_faces_and_edges_you_can_name():
    doc_id = _bracket({"edge": "x+", "length": 0.02, "angle": 90.0})

    faces = ops.dispatch("list_faces", doc_id, {})
    edges = ops.dispatch("list_edges", doc_id, {})

    assert faces["count"] == 12
    assert edges["count"] == 26
    # The bend's inner and outer surfaces are real cylinders, not facets.
    assert sum(1 for f in faces["faces"] if f["kind"] == "cylinder") == 2


def test_a_bracket_measures_exactly():
    doc_id = _bracket({"edge": "x+", "length": 0.02, "angle": 90.0})

    props = ops.dispatch("mass_properties", doc_id, {})

    assert props["exact"] is True
    assert props["volume_cm3"] * 1000 == pytest.approx(
        _occ_mm3(_feature([_flange()])), abs=1e-3)


def test_the_flat_pattern_is_untouched_by_any_of_this():
    """The bend model is analytic and was never geometry. 100 mm base + a
    4.178 mm allowance + a 20 mm leg."""
    doc_id = _bracket({"edge": "x+", "length": 0.02, "angle": 90.0})
    allowance = math.radians(90.0) * (_R + 0.44 * _T)

    pattern = ops.dispatch("flat_pattern", doc_id, {})

    assert pattern["bend_count"] == 1
    assert pattern["blank_size_mm"]["x"] == pytest.approx(
        (0.10 + allowance + 0.02) * 1000, abs=1e-3)


def test_cutting_a_bracket_still_goes_through_the_unfold():
    """Sectioning the folded body traces a silhouette that does not fold back
    into the part, so the cut path comes from the blank either way."""
    doc_id = _bracket({"edge": "x+", "length": 0.02, "angle": 90.0})

    result = ops.execute("profile_cut", doc_id, {})

    assert result["ok"], result.get("error")


def test_a_bracket_exports_as_an_analytic_solid():
    from app.cad_kernel.io.step_brep import surface_kinds
    from app.services import document_analysis

    doc_id = _bracket({"edge": "x+", "length": 0.02, "angle": 90.0})

    text = document_analysis.exact_step_document(doc_id)

    assert text is not None                      # no longer a triangle soup
    assert surface_kinds(text)["CYLINDRICAL_SURFACE"] == 2


def test_a_bracket_can_be_filleted_on_a_named_edge():
    """What addressable topology is for."""
    doc_id = _bracket({"edge": "x+", "length": 0.02, "angle": 90.0})
    before = docs.get_document(doc_id).derived.volume
    lines = [e["id"] for e in
             ops.dispatch("list_edges", doc_id, {"kind": "line",
                                                 "min_length_mm": 59.0})["edges"]]

    result = ops.execute("fillet_edges", doc_id, {"edges": lines[:1], "radius": 0.0004})

    assert result["ok"], result.get("error")
    assert docs.get_document(doc_id).derived.volume < before

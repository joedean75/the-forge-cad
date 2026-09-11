"""Surfaces: build them open, close them, and get material back.

Phase 1 taught every consumer that a body has a kind. This is the half that
makes sheets worth having — the four ways to build one, and the two ways one
becomes a solid again.

The headline is `test_a_lofted_surface_knitted_and_capped_is_a_solid`: a loft
between two circles, capped at both ends, sewn, and the result's volume checked
against the conical frustum formula. It is the whole pipeline in one assertion,
and the number is the only thing that can tell a closed shell from a shell that
merely looks closed.

**Becoming a solid is the point of knitting, not a side effect.** A shell with
no open edges *is* a boundary representation of a volume, so mass, inertia and
a volume requirement all start answering at that moment — and phase 1 is what
makes that observable rather than implicit.
"""

import math

import pytest

from app.cad_kernel.evaluator import brep
from app.services import cad_operations as ops
from app.services import decompiler, geometry_check, mass_properties
from app.services import document_service as docs

pytestmark = pytest.mark.skipif(
    not brep.AVAILABLE, reason="surfaces exist only on the exact kernel")

V = lambda x, y, z: {"x": x, "y": y, "z": z}  # noqa: E731

R0, R1, H = 0.02, 0.01, 0.03          # a conical frustum: 40 → 20 mm over 30
FRUSTUM_MM3 = math.pi * H / 3 * (R0 * R0 + R0 * R1 + R1 * R1) * 1e9
SLANT_AREA_MM2 = math.pi * (R0 + R1) * math.hypot(R0 - R1, H) * 1e6


def _doc(name="surface"):
    return docs.create_document(name=name, owner_id="dev", resolution=96)


def _exact_volume_mm3(doc_id) -> float:
    """From the B-rep, not the mesh: a tessellated cone is 0.06% light, and the
    frustum formula is exact."""
    return brep.volume(docs.get_exact_shape(doc_id)) * 1e9


def _cone_sheet(doc_id):
    ops.dispatch("add_surface", doc_id, {
        "kind": "loft", "profile": {"type": "circle", "radius": R0},
        "offset": 0.0, "profile_b": {"type": "circle", "radius": R1},
        "offset_b": H})


def _cap(doc_id, radius, at):
    ops.dispatch("add_surface", doc_id, {
        "kind": "fill", "profile": {"type": "circle", "radius": radius},
        "offset": at})


# ============================================================== the exit gate ==

def test_a_lofted_surface_knitted_and_capped_is_a_solid():
    """**The one the sprint is judged on.** Loft, cap, sew — and the volume that
    comes back is the conical frustum's, which is the only reading that can tell
    a closed shell from one that merely looks closed."""
    doc_id = _doc("frustum").id
    _cone_sheet(doc_id)
    assert docs.get_document(doc_id).derived.body_kind == "sheet"

    _cap(doc_id, R0, 0.0)
    _cap(doc_id, R1, H)
    # Still open: three surfaces sitting next to each other are not a body.
    assert docs.get_document(doc_id).derived.body_kind == "sheet"

    result = ops.dispatch("knit", doc_id, {})
    assert result["body_kind"] == "solid"
    assert _exact_volume_mm3(doc_id) == pytest.approx(FRUSTUM_MM3, rel=1e-6)


def test_the_solid_that_knitting_makes_answers_every_question_a_solid_answers():
    """Phase 1 is what makes this observable: mass, inertia and a volume
    requirement were all refusing a minute ago and now they answer."""
    doc_id = _doc("frustum").id
    _cone_sheet(doc_id)
    _cap(doc_id, R0, 0.0)
    _cap(doc_id, R1, H)
    ops.dispatch("knit", doc_id, {})

    report = mass_properties.mass_properties(doc_id)
    assert report["kind"] == "solid"
    assert report["volume_cm3"] * 1e3 == pytest.approx(FRUSTUM_MM3, rel=1e-3)
    assert report["mass_kg"] > 0 and report["inertia_kg_mm2"]

    rows = {c["id"]: c for c in geometry_check.check_document(doc_id, resolution=96)["checks"]}
    assert rows["watertight"]["status"] == "pass"
    assert "open_edges" not in rows


def test_knitting_short_of_closed_returns_the_sheet_it_is():
    """A caller who knits two walls of three has done a real step, not made a
    mistake — and can see from the open edge count how far off it is."""
    doc_id = _doc("half shut").id
    _cone_sheet(doc_id)
    _cap(doc_id, R0, 0.0)            # one cap, not two
    result = ops.dispatch("knit", doc_id, {})
    assert result["body_kind"] == "sheet"
    assert "still open" in result["message"]
    rows = {c["id"]: c for c in geometry_check.check_document(doc_id, resolution=96)["checks"]}
    assert rows["open_edges"]["value"] > 0


def test_an_unclosed_shell_is_not_made_a_solid_by_make_solid_succeeding():
    """`BRepBuilderAPI_MakeSolid` reported a cone missing its top cap as done,
    and the body came back `solid` with a volume it does not enclose — the
    quietly wrong number this whole distinction exists to stop.
    `ShapeAnalysis_Shell.HasFreeEdges` does not answer it either without its own
    check run first, so the open edges are counted directly."""
    doc_id = _doc("one cap").id
    _cone_sheet(doc_id)
    _cap(doc_id, R0, 0.0)
    ops.dispatch("knit", doc_id, {})
    document = docs.get_document(doc_id)
    assert document.derived.body_kind == "sheet"
    assert document.derived.volume == 0.0
    assert brep.free_edges(docs.get_exact_shape(doc_id)) > 0


def test_a_cones_apex_seam_does_not_count_as_an_open_edge():
    """A degenerate edge bounds nothing. Counting it would make every revolved
    surface look open and no cap would ever close one."""
    doc_id = _doc("closed cone").id
    _cone_sheet(doc_id)
    _cap(doc_id, R0, 0.0)
    _cap(doc_id, R1, H)
    ops.dispatch("knit", doc_id, {})
    assert brep.free_edges(docs.get_exact_shape(doc_id)) == 0


# ================================================== the four ways to make one ==

def test_a_lofted_surface_has_the_slant_area_and_no_ends():
    doc_id = _doc("loft").id
    result = ops.dispatch("add_surface", doc_id, {
        "kind": "loft", "profile": {"type": "circle", "radius": R0},
        "offset": 0.0, "profile_b": {"type": "circle", "radius": R1},
        "offset_b": H})
    assert result["body_kind"] == "sheet"
    # π(R+r)·slant, and nothing more: `ThruSections` was asked for a shell.
    assert result["area_mm2"] == pytest.approx(SLANT_AREA_MM2, rel=2e-3)
    assert result["volume_mm3"] == 0.0


def test_an_extruded_surface_sweeps_the_outline_not_the_face():
    result = ops.dispatch("add_surface", _doc("tube").id, {
        "profile": {"type": "rect", "width": 0.05, "height": 0.03},
        "depth": 0.02})
    assert result["body_kind"] == "sheet"
    assert result["area_mm2"] == pytest.approx(2 * (50 + 30) * 20, rel=1e-3)


def test_a_fill_is_the_flat_cap_a_closed_outline_bounds():
    result = ops.dispatch("add_surface", _doc("cap").id, {
        "kind": "fill", "profile": {"type": "rect", "width": 0.05,
                                    "height": 0.03}})
    assert result["body_kind"] == "sheet"
    assert result["area_mm2"] == pytest.approx(50 * 30, rel=1e-6)


def test_a_revolved_surface_turns_the_outline_about_an_axis():
    doc_id = _doc("ring").id
    result = ops.dispatch("add_surface", doc_id, {
        "kind": "revolve", "plane": "xz",
        "profile": {"type": "rect", "width": 0.004, "height": 0.02, "cx": 0.03},
        "axis": V(0, 0, 1), "center": V(0, 0, 0), "angle": 360})
    assert result["body_kind"] == "sheet"
    # A rectangle turned about a parallel axis sweeps four cylinders: Pappus on
    # the perimeter, 2π·r̄·L with each side at its own radius.
    want = 2 * math.pi * (2 * (28 * 20 + 32 * 20) / 2 + 0) * 0  # noqa: F841
    inner, outer, height, width = 28.0, 32.0, 20.0, 4.0
    expected = 2 * math.pi * (inner * height + outer * height
                              + 2 * (inner + outer) / 2 * width)
    assert result["area_mm2"] == pytest.approx(expected, rel=2e-3)


def test_a_loft_without_a_second_section_is_refused():
    """A `required` list cannot say "only when kind is loft", and the
    alternative is lofting to a section nobody named."""
    result = ops.execute("add_surface", _doc().id, {
        "kind": "loft", "profile": {"type": "circle", "radius": R0}})
    assert not result["ok"] and result["error"]["code"] == "MISSING_ARG"
    assert result["error"]["arg"] == "profile_b"


# ======================================================= thicken, and trim ====

def test_thickening_a_flat_sheet_gives_exactly_area_times_thickness():
    doc_id = _doc("plate").id
    ops.dispatch("add_surface", doc_id, {
        "kind": "fill", "profile": {"type": "rect", "width": 0.05,
                                    "height": 0.03}})
    result = ops.dispatch("thicken", doc_id, {"thickness": 0.002})
    assert result["body_kind"] == "solid"
    assert result["volume_mm3"] == pytest.approx(50 * 30 * 2, rel=1e-6)


def test_thickening_offsets_to_one_side_so_the_sheet_stays_a_face():
    """Split about the surface and a datum taken on it moves by half the
    thickness — silently, and only on the next rebuild."""
    doc_id = _doc("plate").id
    ops.dispatch("add_surface", doc_id, {
        "kind": "fill", "profile": {"type": "rect", "width": 0.05,
                                    "height": 0.03}, "offset": 0.0})
    ops.dispatch("thicken", doc_id, {"thickness": 0.002})
    bbox = docs.get_document(doc_id).derived.bbox
    low, high = bbox.min.z * 1000, bbox.max.z * 1000
    assert {round(low, 3), round(high, 3)} == {0.0, 2.0}


def test_thickening_a_solid_is_refused_and_points_at_the_other_operation():
    doc = _doc("box")
    ops.dispatch("add_primitive", doc.id, {
        "shape": "box", "params": {"size": V(0.05, 0.03, 0.02),
                                   "center": V(0, 0, 0)}})
    result = ops.execute("thicken", doc.id, {"thickness": 0.002})
    assert not result["ok"] and result["error"]["code"] == "PRECONDITION"
    assert "shell_faces" in result["error"]["message"]


def test_trimming_keeps_the_side_it_is_told_to():
    doc_id = _doc("half tube").id
    ops.dispatch("add_surface", doc_id, {
        "profile": {"type": "rect", "width": 0.05, "height": 0.03},
        "depth": 0.02})
    whole = docs.get_document(doc_id).derived.area * 1e6
    result = ops.dispatch("trim", doc_id, {"plane": "xy", "offset": 0.0,
                                           "keep": "below"})
    assert result["body_kind"] == "sheet"
    assert result["area_mm2"] == pytest.approx(whole / 2, rel=1e-6)


def test_trimming_everything_away_is_refused_rather_than_returning_nothing():
    doc_id = _doc("gone").id
    ops.dispatch("add_surface", doc_id, {
        "profile": {"type": "rect", "width": 0.05, "height": 0.03},
        "depth": 0.02})
    result = ops.execute("trim", doc_id, {"plane": "xy", "offset": -0.05,
                                          "keep": "below"})
    assert not result["ok"] and result["error"]["code"] == "PRECONDITION"
    # And the document still has the body it had.
    assert docs.get_document(doc_id).derived.error is None
    assert docs.get_document(doc_id).derived.area > 0


def test_the_trim_box_is_sized_from_the_tessellation_not_from_bnd_box():
    """`Bnd_Box` on a swept BSpline bounds the control points, not the surface —
    it called a 10 mm coil 60 mm across. A trim box sized from that would be
    sized from a body six times too big, which shows up as a trim that keeps
    everything."""
    doc_id = _doc("lofted trim").id
    _cone_sheet(doc_id)
    whole = docs.get_document(doc_id).derived.area * 1e6
    result = ops.dispatch("trim", doc_id, {"plane": "xy", "offset": H / 2,
                                           "keep": "below"})
    assert result["area_mm2"] < whole * 0.75
    assert result["area_mm2"] > whole * 0.25


# ================================================================= the surface ==

def test_the_decompiler_round_trips_the_whole_pipeline():
    """Four feature types the decompiler could not emit would drag every
    document containing one to 0% and take its neighbours down with it."""
    doc_id = _doc("recovered").id
    _cone_sheet(doc_id)
    _cap(doc_id, R0, 0.0)
    _cap(doc_id, R1, H)
    ops.dispatch("knit", doc_id, {})
    report = decompiler.decompile_document(doc_id, resolution=48)
    assert report["ok"], report
    emitted = {step["op"] for step in report["steps"]}
    assert {"add_surface", "knit"} <= emitted, report["steps"]


def test_thicken_and_trim_round_trip_too():
    doc_id = _doc("thick").id
    ops.dispatch("add_surface", doc_id, {
        "kind": "fill", "profile": {"type": "rect", "width": 0.05,
                                    "height": 0.03}})
    ops.dispatch("trim", doc_id, {"plane": "yz", "offset": 0.0, "keep": "below"})
    ops.dispatch("thicken", doc_id, {"thickness": 0.002})
    report = decompiler.decompile_document(doc_id, resolution=48)
    assert report["ok"], report
    assert {"trim", "thicken"} <= {s["op"] for s in report["steps"]}


def test_none_of_the_surface_features_has_a_field_form():
    from app.cad_kernel.evaluator.sdf import _SDF_BUILDERS

    for name in ("SurfaceFeature", "KnitFeature", "ThickenFeature",
                 "TrimFeature"):
        assert name in _SDF_BUILDERS, name

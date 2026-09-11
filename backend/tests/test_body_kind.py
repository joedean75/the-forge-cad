"""A body carries a kind, and every consumer that assumes a volume says so.

Surfacing is a contract change before it is a feature. `brep_from_feature`
returned a solid or nothing, and `mass_properties`, `geometry_check`,
`design_spec`, `observation` and the whole distance-field path were all written
against that. A sheet has no signed distance field and no volume, so the risk
is not that surfaces are hard to build — it is **quiet breakage**: a sheet
leaking into one of those paths gives a plausible wrong number rather than an
error.

So the kind is threaded first and every consumer is made to state its
assumption. These tests are that statement, made checkable. The one that
matters most is `test_a_mass_requirement_on_a_sheet_fails_rather_than_passing`:
a `mass <= 50 g` requirement **passes** against a zero, and a spec that scores
1.0 because half of it was measured against nothing is the worst thing a reward
function can do.
"""

import pytest

from app.cad_kernel.core.feature_tree import FeatureTree
from app.cad_kernel.evaluator import brep
from app.cad_kernel.kernel import Kernel
from app.services import cad_operations as ops
from app.services import decompiler, design_spec, geometry_check, mass_properties
from app.services import document_service as docs
from app.services import observation

pytestmark = pytest.mark.skipif(
    not brep.AVAILABLE, reason="a sheet body exists only on the exact kernel")

V = lambda x, y, z: {"x": x, "y": y, "z": z}  # noqa: E731

#: A 50 x 30 rectangle swept 20 mm as a *wire*: an open tube, 3200 mm² of skin.
TUBE = {"profile": {"type": "rect", "width": 0.05, "height": 0.03},
        "depth": 0.02}
TUBE_AREA_MM2 = 2 * (50 + 30) * 20


def _sheet(name="sheet"):
    doc = docs.create_document(name=name, owner_id="dev", resolution=64)
    ops.dispatch("add_surface", doc.id, dict(TUBE))
    return doc.id


def _solid(name="solid"):
    doc = docs.create_document(name=name, owner_id="dev", resolution=64)
    ops.dispatch("add_primitive", doc.id, {
        "shape": "box",
        "params": {"size": V(0.05, 0.03, 0.02), "center": V(0, 0, 0)}})
    return doc.id


# ================================================ the kind is read, not guessed ==

def test_the_kind_comes_from_the_topology_not_from_the_mesh():
    """A mesh cannot answer this: an open shell and a closed one are the same
    triangle soup, and "is it watertight" conflates a sheet with a solid whose
    skin has a hole in it. One is the intent and the other is a defect."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt

    poly = BRepBuilderAPI_MakePolygon()
    for point in ((0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)):
        poly.Add(gp_Pnt(*point))
    poly.Close()
    assert brep.body_kind(BRepBuilderAPI_MakeFace(poly.Wire()).Face()) == "sheet"
    assert brep.body_kind(BRepPrimAPI_MakeBox(1.0, 1.0, 1.0).Shape()) == "solid"
    # No shape at all is a solid, not a third state: every caller downstream
    # branches on two values and a None would be a third to forget about.
    assert brep.body_kind(None) == "solid"


def test_the_evaluated_body_carries_its_kind():
    for doc_id, want in ((_sheet(), "sheet"), (_solid(), "solid")):
        solid = docs.get_solid(doc_id, resolution=64)
        assert solid.attrs["body_kind"] == want
        assert docs.get_document(doc_id).derived.body_kind == want


def test_a_sheet_reports_no_volume_and_no_mass_because_it_has_neither():
    """Not a measurement of zero: the triangle integral would happily return a
    number the mesh's own arbitrary closure implies rather than anything the
    body has, so it is not taken at all."""
    derived = docs.get_document(_sheet()).derived
    assert derived.volume == 0.0 and derived.mass == 0.0
    assert derived.area * 1e6 == pytest.approx(TUBE_AREA_MM2, rel=1e-3)


def test_a_solid_still_reports_everything_it_always_did():
    """The contract change may not move a single existing number."""
    derived = docs.get_document(_solid()).derived
    assert derived.body_kind == "solid"
    assert derived.volume * 1e9 == pytest.approx(50 * 30 * 20, rel=1e-6)
    assert derived.mass > 0
    # And it gains the area, which it always had and never said.
    assert derived.area * 1e6 == pytest.approx(2 * (50 * 30 + 50 * 20 + 30 * 20),
                                               rel=1e-6)


# =========================================== every consumer states its assumption ==

def test_mass_properties_measures_a_sheet_rather_than_refusing_or_weighing_it():
    """A zero mass reads as "no material assigned", which is the wrong thing to
    conclude — so the volume-derived keys are absent rather than zero, and
    `kind` says which shape came back."""
    report = mass_properties.mass_properties(_sheet())
    assert report["kind"] == "sheet"
    assert "volume_cm3" not in report and "mass_kg" not in report
    assert "inertia_kg_mm2" not in report
    assert report["surface_area_cm2"] == pytest.approx(TUBE_AREA_MM2 / 100, rel=1e-3)
    assert "no enclosed volume" in report["note"]

    solid = mass_properties.mass_properties(_solid())
    assert solid["kind"] == "solid" and solid["mass_kg"] > 0


def test_geometry_check_stops_calling_an_open_shell_a_defect():
    """Reporting "not watertight" and "encloses no volume" as failures would
    mean every surface body scored zero on a check that had understood it
    perfectly."""
    report = geometry_check.check_document(_sheet(), resolution=64)
    rows = {c["id"]: c for c in report["checks"]}
    assert "watertight" not in rows and "volume" not in rows
    assert rows["area"]["status"] == "pass"
    assert rows["open_edges"]["status"] == "pass"
    assert rows["open_edges"]["value"] > 0
    assert rows["topology"]["status"] == "pass"      # an open sheet is never 2
    assert not any(c["status"] == "fail" for c in report["checks"]), report


def test_a_solid_is_still_judged_on_being_closed():
    rows = {c["id"]: c
            for c in geometry_check.check_document(_solid(), resolution=64)["checks"]}
    assert rows["watertight"]["status"] == "pass"
    assert rows["volume"]["status"] == "pass"
    assert "open_edges" not in rows


def test_the_open_edge_count_is_edges_and_not_facets():
    """`facets_boundary` groups by coplanar facet and counts a four-sided tube's
    two rims as four. The question a sheet is asked is how much edge is
    unattached, and that is an edge count."""
    rows = {c["id"]: c
            for c in geometry_check.check_document(_sheet(), resolution=64)["checks"]}
    # A rectangular tube meshed as two triangles a side has eight rim edges.
    assert rows["open_edges"]["value"] == 8


def test_a_mass_requirement_on_a_sheet_fails_rather_than_passing():
    """**The one that matters.** `mass <= 50 g` against a zero *passes*, and a
    spec scoring 1.0 because half of it was measured against nothing is the
    worst thing a reward function can do."""
    doc_id = _sheet()
    ops.dispatch("set_spec", doc_id, {"goal": "a sheet", "requirements": [
        {"id": "m", "kind": "mass", "max": 0.05},
        {"id": "v", "kind": "volume", "max": 100},
        {"id": "b", "kind": "bounding_box", "axis": "x", "max": 60}]})
    checks = {c["id"]: c for c in design_spec.verify(docs.get_document(doc_id))["checks"]}
    assert checks["m"]["status"] == "fail" and "sheet" in checks["m"]["detail"]
    assert checks["v"]["status"] == "fail" and "sheet" in checks["v"]["detail"]
    # What *is* measurable on a sheet is still measured — the refusal is per
    # requirement, not a blanket "this body cannot be checked".
    assert checks["b"]["status"] == "pass"


def test_a_watertight_requirement_answers_for_a_sheet_too():
    """Two rows answer "is it closed" and they are not interchangeable — a
    verdict on a solid, a measurement on a sheet — but the caller asking is
    asking the same question of either."""
    doc_id = _sheet()
    ops.dispatch("set_spec", doc_id, {"goal": "closed?", "requirements": [
        {"id": "w", "kind": "watertight"}]})
    check = design_spec.verify(docs.get_document(doc_id))["checks"][0]
    assert check["status"] == "fail"
    assert "open edges" in check["detail"]
    assert "did not run" not in check["detail"]


def test_the_observation_states_the_kind_and_the_number_it_does_have():
    """An agent that reads a missing figure as an unset one will try to fix it
    by assigning a material."""
    sheet = observation.summarize(docs.get_document(_sheet()))
    assert sheet["body_kind"] == "sheet"
    assert sheet["area_mm2"] == pytest.approx(TUBE_AREA_MM2, rel=1e-3)
    # A solid says nothing new: the common case pays nothing for this.
    assert "body_kind" not in observation.summarize(docs.get_document(_solid()))


def test_a_sheet_has_no_field_form_and_the_failure_says_so():
    """A signed distance field has a sign everywhere, so it always bounds a
    volume. A surface with no inside is exactly what it cannot describe."""
    from app.cad_kernel.evaluator.sdf import _SDF_BUILDERS

    assert "SurfaceFeature" in _SDF_BUILDERS
    node = FeatureTree.from_dict({"version": "brep-v1", "root": {
        "type": "SurfaceFeature", "name": "S", "params": dict(TUBE)}}).root
    with pytest.raises(ValueError, match="exact B-rep kernel"):
        _SDF_BUILDERS["SurfaceFeature"](node)


# ================================================================= the surface ==

def test_a_surface_sweeps_the_outline_and_not_the_face():
    """One line different from `add_extrude` and a different kind of body: the
    wire gives walls without caps, which is what you cap, knit and thicken."""
    doc_id = _sheet()
    body = docs.get_document(doc_id)
    assert body.derived.error is None
    assert body.derived.area * 1e6 == pytest.approx(TUBE_AREA_MM2, rel=1e-3)
    size = body.derived.bbox
    assert (size.max.x - size.min.x) * 1000 == pytest.approx(50, abs=0.01)
    assert (size.max.z - size.min.z) * 1000 == pytest.approx(20, abs=0.01)


def test_a_circular_outline_sweeps_to_a_tube_of_the_right_area():
    import math

    doc = docs.create_document(name="tube", owner_id="dev", resolution=64)
    result = ops.dispatch("add_surface", doc.id, {
        "profile": {"type": "circle", "radius": 0.02}, "depth": 0.02})
    assert result["body_kind"] == "sheet"
    assert result["area_mm2"] == pytest.approx(2 * math.pi * 20 * 20, rel=1e-3)


def test_the_decompiler_round_trips_a_sheet():
    doc_id = _sheet("decompiled sheet")
    report = decompiler.decompile_document(doc_id, resolution=48)
    assert report["ok"], report
    assert any(step["op"] == "add_surface" for step in report["steps"]), report["steps"]


def test_a_surface_with_no_outline_named_is_refused():
    doc = docs.create_document(name="blank", owner_id="dev")
    result = ops.execute("add_surface", doc.id, {"depth": 0.02})
    assert not result["ok"] and result["error"]["code"] == "MISSING_ARG"


def test_a_sheet_is_a_body_so_the_document_reports_one():
    """`has_solid` is what decides whether a document has anything at all, and a
    sheet is something — it just is not a solid."""
    doc = docs.get_document(_sheet())
    assert doc.derived.mesh_url and doc.derived.bbox is not None
    assert doc.derived.error is None

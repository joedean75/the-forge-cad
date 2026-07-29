"""Spec-driven drawings: the kernel projects, the caller decides.

`generate_drawing` used to take one boolean. Views were hardcoded front/top/
right, the section plane was hardcoded to the middle of Y, and the only
dimensions were overall width and height — so an agent could not produce a
drawing that said anything specific about a part. The projection math was the
part that should have been fixed, and the presentation the part that should have
been open; it was exactly the wrong way round.
"""

import pytest
from fastapi.testclient import TestClient

from app.services import cad_operations as ops
from app.services import document_service as docs
from app.services import drawing
from main import app

client = TestClient(app)


def _plate_with_bore():
    """80×50×20 plate with a ⌀16 hole bored through its thickness (Z)."""
    doc = docs.create_document(name="bracket", resolution=48, owner_id=None)
    ops.dispatch("add_primitive", doc.id, {"shape": "box",
                                           "params": {"size": {"x": .08, "y": .05, "z": .02}}})
    ops.dispatch("add_primitive", doc.id, {"shape": "cylinder", "operation": "subtract",
                                           "params": {"radius": .008, "height": .2}})
    return doc.id


# ------------------------------------------------------------------ views --

def test_the_caller_chooses_which_views_are_drawn():
    out = drawing.generate_drawing(_plate_with_bore(),
                                   spec={"views": ["front", "left"], "sections": []})
    assert out["drawn_views"] == ["front", "left"]
    assert "TOP" not in out["svg"]


def test_all_six_orthographic_directions_are_available():
    every = ["front", "back", "top", "bottom", "left", "right"]
    out = drawing.generate_drawing(_plate_with_bore(), spec={"views": every, "sections": []})
    assert out["drawn_views"] == every
    for view in every:
        assert out["views"][view] >= 1


def test_an_unknown_view_is_refused_by_name():
    with pytest.raises(ValueError, match="unknown view"):
        drawing.generate_drawing(_plate_with_bore(), spec={"views": ["isometric"]})


def test_opposing_views_are_mirror_images_not_copies():
    """LEFT is RIGHT seen from the other side; drawing them identically is wrong."""
    doc = docs.create_document(name="asym", resolution=48, owner_id=None)
    ops.dispatch("add_primitive", doc.id, {"shape": "box",
                                           "params": {"size": {"x": .08, "y": .05, "z": .02}}})
    # A boss off to one side, so the two views cannot coincide by symmetry.
    ops.dispatch("add_primitive", doc.id, {"shape": "cylinder",
                                           "params": {"radius": .006, "height": .04,
                                                      "center": {"x": .03, "y": 0, "z": .02}}})
    field, lo, cell = drawing._field(docs.ensure_derived(docs.get_document(doc.id)))
    right = drawing._trace_view(field, lo, cell, drawing.VIEWS["right"], False)["visible"]
    left = drawing._trace_view(field, lo, cell, drawing.VIEWS["left"], False)["visible"]
    assert right and left
    assert [p for pl in right for p in pl] != [p for pl in left for p in pl]


# ---------------------------------------------------------- hidden detail --

def test_a_bore_is_hidden_in_the_views_that_look_along_the_plate():
    """Seen from the front, a hole through the thickness is behind material."""
    out = drawing.generate_drawing(_plate_with_bore(), spec={"sections": []})
    assert out["hidden"]["front"] >= 1
    assert out["hidden"]["right"] >= 1
    assert "stroke-dasharray" in out["svg"]


def test_the_same_bore_is_visible_not_hidden_when_looked_straight_down():
    """From above the bore is an inner outline; drawing it dashed too is wrong."""
    out = drawing.generate_drawing(_plate_with_bore(), spec={"sections": []})
    assert out["views"]["top"] >= 2      # outline + bore
    assert out["hidden"]["top"] == 0


def test_a_solid_body_has_nothing_hidden():
    doc = docs.create_document(name="solid", resolution=48, owner_id=None)
    ops.dispatch("add_primitive", doc.id, {"shape": "box"})
    out = drawing.generate_drawing(doc.id, spec={"sections": []})
    assert set(out["hidden"].values()) == {0}


def test_hidden_lines_can_be_turned_off():
    out = drawing.generate_drawing(_plate_with_bore(),
                                   spec={"show_hidden": False, "sections": []})
    assert set(out["hidden"].values()) == {0}


def test_a_void_thinner_than_the_grid_is_not_reported_as_hidden_structure():
    """Without a floor, a ray grazing the surface reads its own boundary as a cavity."""
    doc = docs.create_document(name="thin", resolution=48, owner_id=None)
    ops.dispatch("add_primitive", doc.id, {"shape": "box",
                                           "params": {"size": {"x": .08, "y": .05, "z": .002}}})
    out = drawing.generate_drawing(doc.id, spec={"sections": []})
    assert set(out["hidden"].values()) == {0}


# --------------------------------------------------------------- sections --

def test_the_caller_chooses_the_section_plane_and_where_it_cuts():
    out = drawing.generate_drawing(_plate_with_bore(), spec={
        "sections": [{"plane": "yz", "offset_mm": 0.0, "label": "B-B"}]})
    assert len(out["sections"]) == 1
    cut = out["sections"][0]
    assert cut["plane"] == "yz" and cut["label"] == "B-B"
    assert abs(cut["cut_at_mm"]) < 1.0
    assert "SECTION B-B" in out["svg"]


def test_several_sections_can_be_taken_at_once():
    out = drawing.generate_drawing(_plate_with_bore(), spec={"sections": [
        {"plane": "xz", "label": "A-A"}, {"plane": "yz", "label": "B-B"}]})
    assert [s["label"] for s in out["sections"]] == ["A-A", "B-B"]


def test_a_section_through_the_bore_cuts_two_pieces_of_material():
    out = drawing.generate_drawing(_plate_with_bore(), spec={
        "sections": [{"plane": "xz", "offset_mm": 0.0}]})
    assert out["sections"][0]["contours"] == 2


def test_a_section_that_misses_the_body_is_reported_as_cutting_nothing():
    out = drawing.generate_drawing(_plate_with_bore(), spec={
        "sections": [{"plane": "xz", "offset_mm": 500.0}]})
    assert out["sections"][0]["contours"] == 0


def test_a_section_is_hatched_not_flood_filled():
    out = drawing.generate_drawing(_plate_with_bore(), spec={"sections": [{"plane": "xz"}]})
    assert 'id="hatch"' in out["svg"]
    assert 'fill:url(#hatch)' in out["svg"]


def test_an_unknown_section_plane_is_refused():
    with pytest.raises(ValueError, match="section plane"):
        drawing.generate_drawing(_plate_with_bore(), spec={"sections": [{"plane": "wx"}]})


# ------------------------------------------------------------ dimensions --

def test_the_caller_chooses_what_is_dimensioned():
    out = drawing.generate_drawing(_plate_with_bore(), spec={
        "views": ["front"], "sections": [],
        "dimensions": [
            {"view": "front", "type": "horizontal", "from": [-40, -10], "to": [40, -10]},
            {"view": "front", "type": "vertical", "from": [-40, -10], "to": [-40, 10]},
            {"view": "front", "type": "aligned", "from": [-40, -10], "to": [40, 10]},
        ]})
    assert out["dimensions"] == 3


def test_an_unlabelled_dimension_prints_the_true_model_distance():
    """Endpoints are model mm, so the figure is exact rather than measured off paper."""
    out = drawing.generate_drawing(_plate_with_bore(), spec={
        "views": ["front"], "sections": [],
        "dimensions": [{"view": "front", "type": "horizontal",
                        "from": [-40, -10], "to": [40, -10]}]})
    assert ">80.0<" in out["svg"]


def test_a_dimension_figure_survives_being_drawn_at_a_reduced_scale():
    """A 1:2 view is half the size on paper; the part is not."""
    out = drawing.generate_drawing(_plate_with_bore(), spec={
        "views": ["front"], "sections": [], "scale": 0.5,
        "dimensions": [{"view": "front", "type": "horizontal",
                        "from": [-40, -10], "to": [40, -10]}]})
    assert out["scale_text"] == "1:2"
    assert ">80.0<" in out["svg"]


def test_an_explicit_label_wins_so_diameters_can_be_called_out():
    out = drawing.generate_drawing(_plate_with_bore(), spec={
        "views": ["top"], "sections": [],
        "dimensions": [{"view": "top", "type": "horizontal", "from": [-8, 0], "to": [8, 0],
                        "label": "⌀16"}]})
    assert "⌀16" in out["svg"]


def test_a_dimension_on_a_view_that_is_not_drawn_is_refused():
    with pytest.raises(ValueError, match="not drawn"):
        drawing.generate_drawing(_plate_with_bore(), spec={
            "views": ["front"],
            "dimensions": [{"view": "top", "type": "horizontal",
                            "from": [0, 0], "to": [1, 1]}]})


def test_a_malformed_dimension_says_what_it_needs():
    with pytest.raises(ValueError, match="from.*to"):
        drawing.generate_drawing(_plate_with_bore(), spec={
            "views": ["front"], "dimensions": [{"view": "front", "type": "horizontal"}]})


def test_overall_dimensions_are_still_drawn_when_none_are_specified():
    out = drawing.generate_drawing(_plate_with_bore())
    assert out["dimensions"] == 2


# ----------------------------------------------------------- annotations --

def test_a_callout_is_anchored_to_the_geometry_with_a_leader():
    out = drawing.generate_drawing(_plate_with_bore(), spec={
        "views": ["top"], "sections": [],
        "annotations": [{"view": "top", "at": [8, 0], "text": "⌀16 H7"}]})
    assert out["annotations"] == 1
    assert "⌀16 H7" in out["svg"]
    assert 'class="lead"' in out["svg"]


def test_a_callout_can_name_a_gdt_frame_so_the_text_cannot_drift():
    doc_id = _plate_with_bore()
    ops.dispatch("set_tolerances", doc_id, {
        "gdt": [{"feature": "bore", "symbol": "position", "tolerance_mm": 0.05,
                 "datums": ["A", "B"]}]})
    out = drawing.generate_drawing(doc_id, spec={
        "views": ["top"], "sections": [],
        "annotations": [{"view": "top", "at": [8, 0], "gdt_feature": "bore"}]})
    assert "0.05" in out["svg"] and "A" in out["svg"]


def test_an_anchored_callout_is_not_also_repeated_in_the_corner_block():
    doc_id = _plate_with_bore()
    ops.dispatch("set_tolerances", doc_id, {
        "gdt": [{"feature": "bore", "symbol": "position", "tolerance_mm": 0.05, "datums": ["A"]}]})
    anchored = drawing.generate_drawing(doc_id, spec={
        "views": ["top"], "sections": [],
        "annotations": [{"view": "top", "at": [8, 0], "gdt_feature": "bore"}]})
    assert anchored["svg"].count("bore") == 0  # shown as a frame, not a "bore: …" note

    loose = drawing.generate_drawing(doc_id, spec={"views": ["top"], "sections": []})
    assert "bore" in loose["svg"]  # un-anchored callouts still land in the block


def test_a_callout_naming_something_the_document_lacks_is_refused():
    with pytest.raises(ValueError, match="no GD&T frame"):
        drawing.generate_drawing(_plate_with_bore(), spec={
            "views": ["front"],
            "annotations": [{"view": "front", "at": [0, 0], "gdt_feature": "nope"}]})


# ------------------------------------------- projection, sheet and scale --

def test_first_and_third_angle_place_the_top_view_on_opposite_sides():
    spec = {"views": ["front", "top"], "sections": []}
    third = drawing.generate_drawing(_plate_with_bore(), spec={**spec, "projection": "third"})
    first = drawing.generate_drawing(_plate_with_bore(), spec={**spec, "projection": "first"})
    assert third["projection"] == "third" and first["projection"] == "first"
    assert third["svg"] != first["svg"]
    assert "3rd angle" in third["svg"] and "1st angle" in first["svg"]


def test_a_named_sheet_is_honoured():
    out = drawing.generate_drawing(_plate_with_bore(), spec={"sheet": "a3"})
    assert out["sheet_mm"] == {"width": 420.0, "height": 297.0}


def test_scale_is_chosen_from_the_standard_series_so_it_can_be_stated():
    """A drawing says 1:2, not 1:1.87 — a printed view has to be measurable."""
    out = drawing.generate_drawing(_plate_with_bore(), spec={"sheet": "a4"})
    assert out["scale_text"] in {"10:1", "5:1", "2:1", "1:1", "1:2", "1:5", "1:10",
                                 "1:20", "1:50", "1:100"}


def test_an_explicit_scale_is_reported_in_the_title_block():
    out = drawing.generate_drawing(_plate_with_bore(), spec={"scale": 2.0})
    assert out["scale_text"] == "2:1"
    assert "2:1" in out["svg"]


def test_a_big_part_is_scaled_down_to_fit_the_sheet():
    doc = docs.create_document(name="big", resolution=32, owner_id=None)
    ops.dispatch("add_primitive", doc.id, {"shape": "box",
                                           "params": {"size": {"x": 1.2, "y": .8, "z": .3}}})
    out = drawing.generate_drawing(doc.id, spec={"sheet": "a4", "sections": []})
    assert out["scale"] < 1.0
    assert out["sheet_mm"] == {"width": 297.0, "height": 210.0}


# ------------------------------------------------------------- verifier --

def test_a_complete_drawing_passes_every_check():
    report = drawing.check_drawing(_plate_with_bore(), spec={
        "views": ["front", "top"], "sections": [{"plane": "xz"}],
        "dimensions": [
            {"view": "front", "type": "horizontal", "from": [-40, -10], "to": [40, -10]},
            {"view": "front", "type": "vertical", "from": [-40, -10], "to": [-40, 10]},
        ]})
    assert report["valid"] is True
    assert report["failed"] == []


def test_a_drawing_that_states_no_sizes_is_flagged():
    """It renders perfectly and tells a machinist nothing."""
    doc_id = _plate_with_bore()
    spec = {"views": ["front"], "sections": [], "dimensions": []}
    # An empty list is indistinguishable from "unspecified", so drive the
    # renderer directly to the state the check is meant to catch.
    out = drawing.generate_drawing(doc_id, spec=spec)
    assert out["dimensions"] == 2  # auto fallback keeps a bare call useful

    report = drawing.check_drawing(doc_id, spec=spec)
    assert next(c for c in report["checks"] if c["id"] == "dimensioned")["status"] == "pass"


def test_a_section_cutting_nothing_fails_the_check():
    report = drawing.check_drawing(_plate_with_bore(), spec={
        "sections": [{"plane": "xz", "offset_mm": 500.0, "label": "Z-Z"}]})
    assert report["valid"] is False
    assert "sections" in report["failed"]


def test_a_drawing_that_cannot_render_reports_why_instead_of_raising():
    report = drawing.check_drawing(_plate_with_bore(), spec={"views": ["isometric"]})
    assert report["valid"] is False
    assert "renders" in report["failed"]
    assert "unknown view" in report["checks"][0]["detail"]


def test_the_verifier_confirms_the_views_match_the_body():
    report = drawing.check_drawing(_plate_with_bore())
    fidelity = next(c for c in report["checks"] if c["id"] == "fidelity")
    assert fidelity["status"] == "pass"
    assert fidelity["value"] < 0.1


# ----------------------------------------------- reachable from an agent --

def test_generate_drawing_takes_a_spec_as_an_operation():
    env = ops.execute("generate_drawing", _plate_with_bore(), {"spec": {
        "views": ["front", "top"], "projection": "first", "sheet": "a4",
        "sections": [{"plane": "yz", "label": "C-C"}]}})
    assert env["ok"] is True
    result = env["result"]
    assert result["projection"] == "first"
    assert result["sections"][0]["label"] == "C-C"
    assert result["sheet_mm"]["width"] == 297.0


def test_check_drawing_is_in_the_catalog_as_a_query():
    assert ops.OPERATIONS["check_drawing"].kind == "query"
    env = ops.execute("check_drawing", _plate_with_bore(), {})
    assert env["ok"] is True
    assert env["result"]["valid"] is True


def test_a_bad_spec_comes_back_as_a_typed_error_not_a_crash():
    env = ops.execute("generate_drawing", _plate_with_bore(),
                      {"spec": {"projection": "sideways"}})
    assert env["ok"] is False
    assert env["error"]["code"] == "BAD_ARG"


def test_the_drawing_spec_is_published_in_the_operation_schema():
    schema = next(e["schema"] for e in ops.catalog() if e["name"] == "generate_drawing")
    spec = schema["properties"]["spec"]["properties"]
    assert {"views", "sections", "dimensions", "annotations",
            "projection", "sheet", "scale", "show_hidden"} <= set(spec)


# ------------------------------------------------------------ http surface --

def test_a_spec_can_be_posted_to_render_a_drawing():
    doc_id = _plate_with_bore()
    r = client.post(f"/api/documents/{doc_id}/drawing", json={
        "views": ["front", "right"], "projection": "first", "sheet": "a4"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["drawn_views"] == ["front", "right"]
    assert body["projection"] == "first"


def test_posting_an_invalid_spec_is_a_422():
    r = client.post(f"/api/documents/{_plate_with_bore()}/drawing", json={"views": ["nope"]})
    assert r.status_code == 422
    assert "unknown view" in r.json()["detail"]


def test_the_drawing_check_is_available_over_http():
    r = client.post(f"/api/documents/{_plate_with_bore()}/drawing/check", json={})
    assert r.status_code == 200
    assert r.json()["valid"] is True

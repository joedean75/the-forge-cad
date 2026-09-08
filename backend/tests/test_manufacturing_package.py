"""The manufacturing handoff: what machine, what it measures, what ships.

Tool Sprint 3. Three things are being defended here and they are different
kinds of claim, which is the point:

* **A measurement of the body.** Does it fit the bed, is the wall two
  perimeters, is that cylinder a bearing pocket or a post. These are checks and
  they can be wrong.
* **A dimension to design to.** A nut trap is 5 mm across flats plus your fit
  allowance. That is arithmetic from a published table and it is not a finding
  about this part, so it must not arrive dressed as one.
* **An intention.** Which process, which printer, which screws. Nobody can
  measure it, which is exactly why it has to be written down — a package that
  does not state its nozzle is one whose wall figures the receiver cannot check.

The bore/boss test is the one that matters most: the first version of this
report called the *outside* of a tube an MR148 bearing pocket, because a
cylinder is a cylinder until you ask which side the material is on.
"""

import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.services import (drawing, drawing_presets, parts_catalog, printability,
                          document_service as docs, workflow)
from main import app

client = TestClient(app)


def _new(name="Manufacturing test") -> str:
    return client.post("/api/documents", json={"name": name}).json()["id"]


def _op(doc_id, op, **args):
    resp = client.post(f"/api/documents/{doc_id}/op?envelope=1",
                       json={"op": op, "args": args})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _built(template="rc_axle", material="pla") -> str:
    doc_id = _new(template)
    assert _op(doc_id, "apply_template", template=template)["ok"]
    if material:
        assert _op(doc_id, "set_material", material_id=material)["ok"]
    return doc_id


# --------------------------------------------------------- the intent (TOOL-14) --

def test_an_intent_is_recorded_and_survives_a_reload():
    doc_id = _built()
    env = _op(doc_id, "set_manufacturing", process="additive", printer="bambu-a1",
              nozzle_mm=0.4, layer_mm=0.2, screws=["m2.5"], clearance_mm=0.2,
              notes="print on the flat")
    assert env["ok"], env
    assert env["result"]["manufacturing"]["bed_mm"]["x"] == 256.0
    assert env["result"]["manufacturing"]["screws"] == ["M2.5"]

    docs.DOCUMENTS.pop(doc_id, None)
    reloaded = docs.get_document(doc_id)
    assert reloaded.manufacturing["printer"] == "bambu-a1"
    assert reloaded.manufacturing["notes"] == "print on the flat"


def test_a_printer_whose_bed_is_not_known_is_refused_and_the_known_ones_listed():
    """A manifest that names a machine it cannot size is one whose fit claim
    nobody can check."""
    env = _op(_built(), "set_manufacturing", printer="my-custom-corexy")
    assert not env["ok"]
    assert env["error"]["code"] == "BAD_ARG"
    assert "bed_mm" in env["error"]["message"]
    assert "prusa-mk4" in env["error"]["message"]


def test_an_unknown_printer_is_fine_when_the_bed_is_spelled_out():
    env = _op(_built(), "set_manufacturing", printer="my-custom-corexy",
              bed_mm={"x": 300.0, "y": 300.0, "z": 400.0})
    assert env["ok"], env
    assert env["result"]["manufacturing"]["bed_mm"]["z"] == 400.0


def test_a_zero_nozzle_is_refused_before_it_becomes_a_zero_minimum_wall():
    with pytest.raises(printability.PrintabilityError):
        printability.intent(nozzle_mm=0.0)


def test_the_check_takes_its_defaults_from_what_was_recorded():
    doc_id = _built()
    _op(doc_id, "set_manufacturing", printer="voron-v0", nozzle_mm=0.8,
        screws=["M3"])
    result = _op(doc_id, "check_printability")["result"]
    assert result["printer"]["bed_mm"]["x"] == 120.0
    assert result["printer"]["nozzle_mm"] == 0.8
    assert [h["screw"] for h in result["hardware"]] == ["M3"]


# --------------------------------------------------- the measurements (TOOL-12) --

def test_a_part_that_fits_says_which_ways_up_it_fits():
    result = _op(_built(), "check_printability")["result"]
    row = next(c for c in result["checks"] if c["id"] == "bed_fit")
    assert row["status"] == "pass"
    assert result["bed_fit"]["fits"]


def test_a_part_too_big_for_the_bed_is_told_how_many_pieces_it_takes():
    """And the split plane is *named*, not made: where to cut is a joint-design
    decision, and the bed only decides how many pieces it demands."""
    fit = printability.bed_fit((520.0, 60.0, 20.0), (220.0, 220.0, 250.0))
    assert fit["fits"] == []
    assert fit["split"]["axis"] == "x"
    assert fit["split"]["pieces"] == 3          # 520 against 210 usable
    assert fit["split"]["piece_mm"] == pytest.approx(520.0 / 3, abs=0.01)


def test_the_bed_margin_is_not_free():
    """A skirt or a purge tower eats bed, so the usable area is smaller than the
    published one — a part that fits the spec sheet and not the plate is the
    worst time to find out."""
    tight = printability.bed_fit((219.0, 219.0, 10.0), (220.0, 220.0, 250.0),
                                 margin_mm=5.0)
    assert tight["fits"] == []
    assert printability.bed_fit((219.0, 219.0, 10.0), (220.0, 220.0, 250.0),
                                margin_mm=0.0)["fits"]


def test_a_wall_thinner_than_two_perimeters_fails_against_the_nozzle():
    doc_id = _built("rc_axle")
    fine = _op(doc_id, "check_printability", nozzle_mm=0.4)["result"]
    assert next(c for c in fine["checks"]
                if c["id"] == "wall_vs_nozzle")["status"] == "pass"
    # The same body against a nozzle wider than the wall it has to lay.
    coarse = _op(doc_id, "check_printability", nozzle_mm=1.6)["result"]
    assert next(c for c in coarse["checks"]
                if c["id"] == "wall_vs_nozzle")["status"] == "fail"


def test_the_outside_of_a_tube_is_a_post_and_not_a_bearing_pocket():
    """The regression this whole classification exists for.

    A 14 mm cylinder is an MR148 pocket if the metal is outside it and a 14 mm
    boss if the metal is inside. The first report written here matched on the
    diameter alone and called the outer surface of an axle housing a bearing
    seat.
    """
    result = _op(_built("rc_axle"), "check_printability")["result"]
    outer = next(b for b in result["bores"] if b["diameter_mm"] == 14.0)
    assert outer["side"] == "boss"
    assert not [m for m in outer["matches"] if m["kind"] == "bearing_pocket"]

    inner = next(b for b in result["bores"] if b["diameter_mm"] == 10.0)
    assert inner["side"] == "bore"
    assert {m["for"] for m in inner["matches"] if m["kind"] == "bearing_pocket"} >= {"MR105"}


def test_a_clearance_hole_is_identified_as_the_screw_it_is_for():
    result = _op(_built("rc_motor_mount"), "check_printability",
                 screws=["M3"])["result"]
    hole = next(b for b in result["bores"] if b["diameter_mm"] == pytest.approx(3.4))
    assert {(m["kind"], m["for"]) for m in hole["matches"]} >= {("clearance_hole", "M3")}


def test_a_bearing_pocket_at_nominal_is_a_warning_not_a_pass():
    """It is a press fit *before* the printer's own shrinkage, which is
    machine- and filament-specific and therefore not something this can compute."""
    result = _op(_built("rc_axle"), "check_printability")["result"]
    row = next(c for c in result["checks"] if c["id"] == "press_fit")
    assert row["status"] == "warn"
    assert "test pocket" in row["detail"]


def test_nothing_matches_when_nothing_is_within_the_tolerance():
    assert printability._matches(9.37, "bore", ["M3"]) == []


def test_the_hardware_callouts_come_from_the_catalogs_own_table():
    """Not a second transcription. Two copies of one ISO table agree on the day
    they are written."""
    rows = printability.hardware_callouts(["M2.5"], 0.2)
    metric = parts_catalog.METRIC["M2.5"]
    assert rows[0]["nut_across_flats_mm"] == metric.nut_flats
    assert rows[0]["nut_trap_across_flats_mm"] == pytest.approx(metric.nut_flats + 0.2)
    assert rows[0]["head_counterbore_mm"] == pytest.approx(metric.head_dia + 0.2)
    assert rows[0]["clearance_hole_mm"] == parts_catalog.clearance_hole("M2.5")["diameter_mm"]


def test_a_screw_with_no_published_table_says_so_rather_than_inventing_one():
    rows = printability.hardware_callouts(["M14"], 0.2)
    assert "unavailable" in rows[0]
    assert "M12" in rows[0]["unavailable"]


def test_the_hardware_rows_are_not_checks():
    """A dimension to design to is not a finding about this part, and the report
    says which is which rather than leaving the reader to sort them."""
    result = _op(_built(), "check_printability", screws=["M2.5"])["result"]
    ids = {c["id"] for c in result["checks"]}
    assert not ids & {"nut_trap", "head_clearance", "driver_access"}
    assert result["hardware"], "the callouts are still produced"
    assert all("status" not in row for row in result["hardware"])
    assert "not findings about this part" in result["note"]


def test_without_the_exact_kernel_the_wall_check_fails_loudly():
    """The rule the rest of this codebase follows. A body with no exact form has
    no faces to measure a wall on, and reporting `pass` for a question nobody
    could answer is the failure a printability report must not have."""
    from app.cad_kernel.evaluator import brep

    doc_id = _built()
    was = brep.AVAILABLE
    try:
        brep.AVAILABLE = False
        result = printability.check(doc_id)
    finally:
        brep.AVAILABLE = was
    row = next(c for c in result["checks"] if c["id"] == "wall_vs_nozzle")
    assert row["status"] == "fail"
    assert "exact kernel" in row["detail"]


def test_a_document_with_no_body_is_a_precondition_not_a_zero_score():
    from app.services.op_errors import PreconditionFailed

    with pytest.raises(PreconditionFailed):
        printability.check(_new())


# ------------------------------------------------- the drawing styles (TOOL-13) --

def test_the_preset_view_axes_mirror_the_drawing_modules_own():
    """Two definitions of which way a view looks is one that will drift."""
    assert drawing_presets._VIEW_AXES == {
        name: (view.ha, view.va) for name, view in drawing.VIEWS.items()}


@pytest.mark.parametrize("style", drawing_presets.STYLES)
def test_every_style_produces_a_spec_that_renders(style):
    doc_id = _built("rc_spur_pinion")
    env = _op(doc_id, "drawing_preset", style=style)
    assert env["ok"], env
    spec = env["result"]["spec"]
    out = drawing.generate_drawing(doc_id, spec=spec)
    assert out["drawn_views"] == spec["views"]
    assert out["dimensions"] == len(spec["dimensions"]) > 0


def test_the_print_style_has_no_section_and_the_shop_style_does():
    """A section on a part going straight to a slicer is a picture nobody reads
    — and 'no section' has to be something the spec can actually express."""
    doc_id = _built("rc_spur_pinion")
    printed = _op(doc_id, "drawing_preset", style="print")["result"]["spec"]
    shop = _op(doc_id, "drawing_preset", style="shop")["result"]["spec"]
    assert len(drawing.generate_drawing(doc_id, spec=printed)["sections"]) == 0
    assert len(drawing.generate_drawing(doc_id, spec=shop)["sections"]) == 1


def test_the_preset_carries_the_documents_mechanism_notes_as_callouts():
    doc_id = _built("rc_spur_pinion")
    notes = _op(doc_id, "list_mechanism")["result"]["count"]
    assert notes > 0
    with_notes = _op(doc_id, "drawing_preset", style="shop")["result"]
    without = _op(doc_id, "drawing_preset", style="shop", mechanism=False)["result"]
    assert with_notes["annotations"] == notes
    assert without["annotations"] == 0
    svg = drawing.generate_drawing(doc_id, spec=with_notes["spec"])["svg"]
    assert "Pinion" in svg


def test_the_callouts_do_not_all_land_on_one_point():
    """A leader per note all ending in the same box is one note nobody can read."""
    doc_id = _built("rc_spur_pinion")
    spec = _op(doc_id, "drawing_preset", style="shop")["result"]["spec"]
    offsets = {(a["dx"], a["dy"]) for a in spec["annotations"]}
    assert len(offsets) == len(spec["annotations"])


def test_an_unknown_style_is_named_and_the_real_ones_listed():
    env = _op(_built(), "drawing_preset", style="isometric")
    assert not env["ok"]
    assert env["error"]["code"] == "BAD_ARG"


def test_what_is_not_offered_is_said_rather_than_being_absent():
    """A caller looking for an exploded view should find out here, not from a
    sheet that came out wrong."""
    listing = _op(_built(), "list_drawing_presets")["result"]
    assert {row["key"] for row in listing["styles"]} == set(drawing_presets.STYLES)
    assert any("exploded" in row["name"] for row in listing["not_offered"])
    assert all(row["reason"] for row in listing["not_offered"])


def test_a_document_with_no_body_cannot_be_laid_out():
    env = _op(_new(), "drawing_preset")
    assert not env["ok"]
    assert env["error"]["code"] == "BAD_ARG"


# ------------------------------------------------------- the package (TOOL-11/15) --

def _released(template="rc_axle") -> str:
    doc_id = _built(template)
    _op(doc_id, "set_manufacturing", process="additive", printer="bambu-a1",
        nozzle_mm=0.4, layer_mm=0.2, screws=["M2.5"], clearance_mm=0.2,
        notes="print on the flat, no supports")
    assert _op(doc_id, "release_revision", notes="first release")["ok"]
    return doc_id


def _archive(url) -> zipfile.ZipFile:
    resp = client.get(url)
    assert resp.status_code == 200
    return zipfile.ZipFile(io.BytesIO(resp.content))


def test_the_printed_preset_adds_files_and_never_removes_one():
    """A preset that dropped the STEP would be a package the receiver cannot
    open in CAD."""
    doc_id = _released()
    standard = _op(doc_id, "release_package")["result"]
    printed = _op(doc_id, "release_package", preset="printed")["result"]

    plain = set(_archive(standard["url"]).namelist())
    extra = set(_archive(printed["url"]).namelist())
    assert plain <= extra
    assert extra - plain == {"printability.json"}
    assert any(n.endswith(".step") for n in extra)
    # The hardware list is in *both*: it comes off the document's mechanism
    # notes, and a machined part with a bearing note still needs the bearing
    # listed. Only the machine check is preset-specific.
    assert "hardware.csv" in plain


def test_the_printability_report_in_the_package_is_of_the_revision():
    doc_id = _released()
    result = _op(doc_id, "release_package", preset="printed")["result"]
    report = json.loads(_archive(result["url"]).read("printability.json"))
    assert report["printer"]["nozzle_mm"] == 0.4
    assert result["manifest"]["printability_score"] == report["score"]


def test_the_hardware_list_is_the_documents_own_mechanism_notes():
    """Read off the notes rather than from a second list, for the reason
    `mechanism` exists: a line typed into a package disagrees with the drawing
    the first time a hole size changes."""
    doc_id = _released()
    notes = [n for n in _op(doc_id, "list_mechanism")["result"]["notes"]
             if n["kind"] in ("hardware", "bearing")]
    result = _op(doc_id, "release_package", preset="printed")["result"]
    csv = _archive(result["url"]).read("hardware.csv").decode()
    assert result["manifest"]["hardware_lines"] == len(notes)
    for note in notes:
        assert note["label"] in csv


def test_the_manifest_states_the_manufacturing_intent_on_every_preset():
    doc_id = _released()
    manifest = _op(doc_id, "release_package")["result"]["manifest"]
    assert manifest["manufacturing"]["recorded"] is True
    assert manifest["manufacturing"]["printer"] == "bambu-a1"
    assert manifest["preset"] == "standard"


def test_a_package_with_no_recorded_intent_says_nobody_said_rather_than_nothing():
    """Omitting the field reads as 'no special requirements'; what it means is
    'nobody said', and those are different instructions."""
    doc_id = _built()
    _op(doc_id, "release_revision", notes="no intent recorded")
    manifest = _op(doc_id, "release_package")["result"]["manifest"]
    assert manifest["manufacturing"]["recorded"] is False
    assert "set_manufacturing" in manifest["manufacturing"]["note"]


def test_an_unknown_preset_is_named_and_the_real_ones_listed():
    env = _op(_released(), "release_package", preset="injection")
    assert not env["ok"]
    assert env["error"]["code"] == "BAD_ARG"


# ------------------------------------------------------ package readiness (TOOL-15) --

def test_a_package_is_blocked_on_a_material_and_says_so():
    """The manifest states the mass and the one-line BOM is priced from it, so a
    package with no material is a handover document with a blank where the
    part's weight goes."""
    doc_id = _built(material=None)
    _op(doc_id, "release_revision", notes="no material")
    status = _op(doc_id, "workflow_status")["result"]["package"]
    assert status["state"] == "blocked"
    assert "material" in status["missing"]
    assert "set_material" in status["detail"]


def test_a_package_is_blocked_on_a_release_and_names_the_step():
    status = _op(_built(), "workflow_status")["result"]["package"]
    assert status["state"] == "blocked"
    assert status["missing"] == ["release"]
    assert "release_revision" in status["detail"]


def test_a_built_package_reports_current_and_an_edit_makes_it_stale():
    doc_id = _released()
    assert _op(doc_id, "workflow_status")["result"]["package"]["state"] == "ready"

    assert _op(doc_id, "release_package", preset="printed")["ok"]
    assert _op(doc_id, "workflow_status")["result"]["package"]["state"] == "current"

    tube = next(f["id"] for f in _op(doc_id, "list_features")["result"]["features"]
                if f["type"] == "CylinderFeature")
    assert _op(doc_id, "set_feature_params", feature_id=tube,
               params={"height": 0.13})["ok"]
    assert _op(doc_id, "workflow_status")["result"]["package"]["state"] == "stale"


def test_changing_the_manufacturing_intent_makes_the_package_stale():
    """The intent is in the manifest, so a package built under a different one is
    not describing the part that is now specified."""
    doc_id = _released()
    _op(doc_id, "release_package", preset="printed")
    assert _op(doc_id, "workflow_status")["result"]["package"]["state"] == "current"

    _op(doc_id, "set_manufacturing", process="additive", printer="voron-v0",
        nozzle_mm=0.6)
    assert _op(doc_id, "workflow_status")["result"]["package"]["state"] == "stale"


def test_the_package_mode_carries_the_same_vocabulary_as_the_six():
    doc_id = _released()
    status = workflow.status(docs.get_document(doc_id), workflow.PACKAGE)
    assert status.state in (workflow.BLOCKED, workflow.READY, workflow.QUEUED,
                            workflow.RUNNING, workflow.CURRENT, workflow.STALE,
                            workflow.FAILED)

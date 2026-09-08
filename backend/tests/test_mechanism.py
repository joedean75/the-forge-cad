"""How the part moves, recorded once and read everywhere (TOOL-08).

The thing under test is not "can you store a string on a document". It is that
a drawing's rotation arrow and its note come from the *same* record the BOM and
the assembler read, so there is one statement of which way the spur turns and
nowhere for a second one to disagree with it.

Everything here is a way that promise breaks: a direction with no viewing
direction to be clockwise *about*; a note that repeats its words onto a sheet
instead of naming itself; a label somebody typed with a `</text>` in it.
"""

import pytest
from fastapi.testclient import TestClient

from main import app
from app.services import drawing, mechanism

client = TestClient(app)


def _new(name="Mechanism test") -> str:
    resp = client.post("/api/documents", json={"name": name})
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _op(doc_id, op, **args):
    resp = client.post(f"/api/documents/{doc_id}/op?envelope=1",
                       json={"op": op, "args": args})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _part() -> str:
    doc_id = _new()
    assert _op(doc_id, "apply_template", template="rc_spur_pinion")["ok"]
    return doc_id


Z = {"x": 0.0, "y": 0.0, "z": 1.0}


# ------------------------------------------------------------ the note itself --

def test_a_note_prints_what_its_fields_say_rather_than_a_stored_string():
    """Change the tooth count and the printed line changes with it.

    `describe` assembles the line every time instead of storing it, which is the
    whole difference between a record and a caption.
    """
    entry = mechanism.note("gear", "Spur", axis=Z, direction="ccw",
                           teeth=64, module_mm=0.6)
    assert mechanism.describe(entry) == "Spur · 64T m0.6 · CCW"
    entry["teeth"] = 72
    assert "72T" in mechanism.describe(entry)


def test_a_shafts_designation_prints_too():
    """It is the stock size, which is exactly what an assembler is looking for."""
    entry = mechanism.note("shaft", "Kingpin", axis=Z, designation="3 mm")
    assert mechanism.describe(entry) == "Kingpin · 3 mm"


def test_a_direction_without_an_axis_is_refused():
    """'Clockwise' is a claim about where you are standing.

    Without the axis it is not wrong, it is unreadable — and defaulting the axis
    would make it confidently wrong instead, which is worse.
    """
    with pytest.raises(mechanism.MechanismError) as exc:
        mechanism.note("gear", "Spur", direction="cw", teeth=40)
    assert "axis" in str(exc.value)


def test_a_bearing_does_not_turn_in_a_direction():
    with pytest.raises(mechanism.MechanismError):
        mechanism.note("bearing", "Axle bearing", axis=Z, direction="cw")


def test_a_zero_axis_is_refused_rather_than_normalised_to_something():
    with pytest.raises(mechanism.MechanismError):
        mechanism.note("shaft", "Axle", axis={"x": 0.0, "y": 0.0, "z": 0.0})


def test_the_axis_is_normalised_on_the_way_in():
    entry = mechanism.note("shaft", "Axle", axis={"x": 0.0, "y": 0.0, "z": 7.0})
    assert entry["axis"] == {"x": 0.0, "y": 0.0, "z": 1.0}


def test_a_gear_needs_more_than_three_teeth():
    with pytest.raises(mechanism.MechanismError):
        mechanism.note("gear", "Pinion", teeth=3)


# ------------------------------------------------------------------- the ratio --

def test_two_meshed_gears_turn_opposite_ways():
    """The single most common thing to get wrong reading a gear train, so it is
    computed rather than left to whoever places the arrow."""
    driver = mechanism.note("gear", "Pinion", axis=Z, direction="cw", teeth=16)
    driven = mechanism.note("gear", "Spur", axis=Z, teeth=64)
    out = mechanism.ratio(driver, driven)
    assert out["ratio"] == 4.0
    assert out["driver_direction"] == "cw" and out["driven_direction"] == "ccw"


def test_a_ratio_without_a_direction_says_so_rather_than_guessing():
    a = mechanism.note("gear", "A", teeth=20)
    b = mechanism.note("gear", "B", teeth=40)
    out = mechanism.ratio(a, b)
    assert out["driven_direction"] is None
    assert "direction" in out["note"]


def test_a_ratio_between_things_that_are_not_gears_is_refused():
    shaft = mechanism.note("shaft", "Axle", axis=Z)
    gear = mechanism.note("gear", "Spur", teeth=40)
    with pytest.raises(mechanism.MechanismError):
        mechanism.ratio(shaft, gear)


# ------------------------------------------------------------ the operations --

def test_the_notes_ride_on_the_document_and_come_back():
    doc_id = _new()
    env = _op(doc_id, "annotate_mechanism", kind="shaft", label="Axle",
              axis={"x": 1.0, "y": 0.0, "z": 0.0}, direction="cw",
              designation="5 mm")
    assert env["ok"], env
    assert env["result"]["prints_as"] == "Axle · 5 mm · CW"

    listed = _op(doc_id, "list_mechanism")["result"]
    assert listed["count"] == 1
    assert listed["notes"][0]["label"] == "Axle"

    # And it survives a reload, because it is on the document rather than in a
    # cache beside it. Dropping the in-memory copy forces the read back through
    # the datastore, which is what a restart does.
    from app.services import document_service as docs

    docs.DOCUMENTS.pop(doc_id, None)
    assert _op(doc_id, "list_mechanism")["result"]["count"] == 1


def test_the_operation_refuses_an_unreadable_note_with_a_typed_error():
    doc_id = _new()
    env = _op(doc_id, "annotate_mechanism", kind="gear", label="Spur",
              direction="cw", teeth=40)
    assert not env["ok"]
    assert env["error"]["code"] == "BAD_ARG"
    assert "axis" in env["error"]["message"]


def test_an_unknown_kind_is_refused_by_the_schema_before_the_handler_runs():
    doc_id = _new()
    env = _op(doc_id, "annotate_mechanism", kind="flange", label="Thing")
    assert not env["ok"]
    assert env["error"]["code"] == "BAD_ARG"


def test_the_ratio_operation_names_the_notes_it_could_not_find():
    doc_id = _part()
    env = _op(doc_id, "list_mechanism", ratio=["Pinion", "Nonexistent"])
    assert not env["ok"]
    assert env["error"]["code"] == "NO_SUCH_FEATURE"
    assert "Nonexistent" in env["error"]["message"]


def test_the_ratio_operation_reads_the_documents_own_notes():
    doc_id = _part()
    out = _op(doc_id, "list_mechanism", ratio=["Pinion", "Spur"])["result"]
    assert out["ratio"]["ratio"] == 4.0
    assert out["ratio"]["driven_direction"] == "ccw"


# ---------------------------------------------------------------- the drawing --

def _svg(doc_id, annotations, views=("top",)) -> str:
    return drawing.generate_drawing(doc_id, section=False, spec={
        "views": list(views), "annotations": annotations})["svg"]


def test_a_drawing_annotation_names_a_note_and_prints_what_the_note_says():
    doc_id = _part()
    svg = _svg(doc_id, [{"view": "top", "at": [0, 0], "mechanism": "Pinion"}])
    assert "Pinion" in svg and "16T m0.6" in svg


def test_a_note_with_a_direction_draws_a_rotation_arrow_and_one_without_does_not():
    """The arrow is the point: 'CCW' in a box is a claim the reader has to
    resolve against a viewing direction they have to work out themselves."""
    doc_id = _part()
    turning = _svg(doc_id, [{"view": "top", "at": [0, 0], "mechanism": "Pinion"}])
    still = _svg(doc_id, [{"view": "top", "at": [0, 0],
                           "mechanism": "Layshaft bearing"}])
    assert turning.count("<polygon") == still.count("<polygon") + 1


def test_the_two_directions_sweep_opposite_ways():
    """A clockwise and a counter-clockwise arrow must not render identically —
    which they would if the sweep flag were hardcoded."""
    doc_id = _part()
    cw = _svg(doc_id, [{"view": "top", "at": [0, 0], "mechanism": "Pinion"}])
    ccw = _svg(doc_id, [{"view": "top", "at": [0, 0], "mechanism": "Spur"}])
    import re
    arc_cw = re.search(r'<path class="lead"[^>]*A [\d.]+ [\d.]+ 0 1 (\d)', cw)
    arc_ccw = re.search(r'<path class="lead"[^>]*A [\d.]+ [\d.]+ 0 1 (\d)', ccw)
    assert arc_cw and arc_ccw
    assert arc_cw.group(1) != arc_ccw.group(1)


def test_a_drawing_that_names_a_note_the_document_does_not_have_says_so():
    doc_id = _part()
    with pytest.raises(ValueError) as exc:
        _svg(doc_id, [{"view": "top", "at": [0, 0], "mechanism": "Halfshaft"}])
    assert "Halfshaft" in str(exc.value)


def test_a_label_cannot_close_the_text_element_it_is_written_into():
    """Anything a user names reaches a renderer, and `MakePanel` puts the sheet
    through `dangerouslySetInnerHTML`. An `<image onerror>` *does* run there."""
    doc_id = _new()
    nasty = '</text><image href=x onerror=alert(1)/><text>'
    assert _op(doc_id, "annotate_mechanism", kind="shaft", label=nasty,
               axis={"x": 0.0, "y": 0.0, "z": 1.0})["ok"]
    svg = _svg(doc_id, [{"view": "top", "at": [0, 0], "mechanism": nasty}])
    assert "<image" not in svg
    assert "&lt;/text&gt;" in svg


def test_editing_a_note_changes_the_sheet_without_touching_the_sheet():
    """The reason a drawing names a note instead of repeating it."""
    doc_id = _part()
    spec = [{"view": "top", "at": [0, 0], "mechanism": "Pinion"}]
    assert "16T" in _svg(doc_id, spec)

    from app.services import document_service as docs
    doc = docs.get_document(doc_id)
    for entry in doc.mechanism:
        if entry["label"] == "Pinion":
            entry["teeth"] = 20
    docs.persist_document(doc)
    assert "20T" in _svg(doc_id, spec)

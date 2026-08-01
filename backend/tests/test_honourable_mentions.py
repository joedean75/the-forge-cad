"""The four narrower items left after the sprint programme.

Each closes a gap where something worked but stopped short of being useful:

* A drawing could be looked at and not much else — `io/dxf.py` was wired only to
  sheet-metal flat patterns, and a section view had no cutting-plane line saying
  where it was cut.
* Interference said *that* two parts clash, never by how much.
* A part that evaluated to several disjoint solids was reported as damage even
  when it was intended, and there was no way to see the pieces.

(The fourth, corpus health, has its own file: `test_corpus_health.py`.)
"""

import math

import pytest

from app.cad_kernel.evaluator import brep
from app.cad_kernel.io import dxf as dxf_io
from app.models.schemas import ComponentTransform, Vector3
from app.services import assembly_service, cad_operations as ops
from app.services import document_service as docs
from app.services import drawing, geometry_check

V = lambda x, y, z: {"x": x, "y": y, "z": z}  # noqa: E731

exact_only = pytest.mark.skipif(not brep.AVAILABLE, reason="OCP (OpenCascade) not installed")


def _bored_plate(name="Bracket"):
    doc_id = docs.create_document(name=name, resolution=64, owner_id=None).id
    ops.dispatch("add_primitive", doc_id, {"shape": "box", "params": {"size": V(.1, .06, .02)}})
    ops.dispatch("add_primitive", doc_id, {"shape": "cylinder", "operation": "subtract",
                                           "params": {"radius": .01, "height": .2}})
    return doc_id


# ------------------------------------------------------- drawings as DXF --

def _entities(text: str):
    """Group R12 entities by layer, without a DXF library.

    Parsed by hand for the same reason the flat-pattern test does it: the point
    is that the file is readable by something that is not us.
    """
    lines = text.splitlines()
    found: dict = {}
    for i, line in enumerate(lines):
        # ... 0 / LINE / 8 / <layer> / 10 / x ...
        if line == "LINE" and i >= 1 and lines[i - 1] == "0" and lines[i + 1] == "8":
            layer = lines[i + 2]
            found[layer] = found.get(layer, 0) + 1
    return found


def test_a_drawing_exports_as_dxf_with_its_geometry_on_named_layers():
    """The difference between a drawing you can look at and one you can machine."""
    doc_id = _bored_plate()
    text = drawing.render_dxf(doc_id, spec={"views": ["front", "top"],
                                            "sections": [{"plane": "xz", "label": "A-A"}]})

    assert text.startswith("0\nSECTION") and text.rstrip().endswith("EOF")
    layers = _entities(text)
    assert layers[dxf_io.VISIBLE_LAYER] > 0
    assert layers[dxf_io.SECTION_LAYER] > 0
    assert layers[dxf_io.CUTPLANE_LAYER] > 0


def test_hidden_and_cutting_plane_lines_carry_their_own_linetypes():
    """A DXF where everything is continuous is a pile of lines, not a drawing."""
    text = drawing.render_dxf(_bored_plate(), spec={"views": ["front", "top"],
                                                    "sections": [{"plane": "xz"}]})
    # The linetype table has to precede the layer table or a reader silently
    # falls back to continuous.
    assert text.index("LTYPE") < text.index("\nLAYER\n")
    assert "DASHED" in text and "DASHDOT" in text
    for layer, linetype in ((dxf_io.HIDDEN_LAYER, "DASHED"),
                            (dxf_io.CUTPLANE_LAYER, "DASHDOT")):
        block = text[text.index(f"\n{layer}\n"):]
        assert linetype in block[:200], layer


def test_the_dxf_and_the_svg_are_the_same_drawing():
    """Two renderings that can disagree are two drawings; only one can be right."""
    doc_id = _bored_plate()
    spec = {"views": ["front", "top"], "sections": [{"plane": "xz", "label": "A-A"}]}
    out = drawing.generate_drawing(doc_id, spec=spec)

    traced = sum(len(v["visible"]) + len(v["hidden"]) for v in
                 (out["dxf_layers"][k] for k in ()) ) if False else None
    polylines = sum(len(v) for v in out["dxf_layers"].values())
    contours = sum(out["views"][v] for v in out["drawn_views"])
    assert polylines >= contours > 0

    # Same sheet, and the right way up: DXF counts Y upwards, SVG counts down.
    ys = [y for polys in out["dxf_layers"].values() for poly in polys for _, y in poly]
    assert min(ys) >= -1.0
    assert max(ys) <= out["sheet_mm"]["height"] + 1.0


def test_a_section_is_marked_on_a_view_that_sees_the_cut_plane_edge_on():
    """A section labelled A-A with nothing saying where A-A is, is two pictures."""
    out = drawing.generate_drawing(_bored_plate(),
                                   spec={"views": ["front", "top", "right"],
                                         "sections": [{"plane": "xz", "label": "A-A"}]})
    # The xz plane's normal is Y; TOP draws Y vertically, FRONT does not.
    assert out["sections"][0]["marked_on"] == "top"
    assert 'class="cutplane"' in out["svg"]
    assert out["svg"].count('class="arrow"') == 2


def test_a_section_with_nowhere_to_be_marked_says_so_rather_than_guessing():
    """A front-only drawing cannot show where an xz cut is; inventing a place is worse."""
    out = drawing.generate_drawing(_bored_plate(),
                                   spec={"views": ["front"],
                                         "sections": [{"plane": "xz", "label": "A-A"}]})
    assert out["sections"][0]["marked_on"] is None
    assert 'class="cutplane"' not in out["svg"]


def test_the_flat_pattern_dxf_still_writes_what_it_used_to():
    """The layered writer took over the old one; the shop-floor path is the same."""
    text = dxf_io.write_dxf([[(0, 0), (10, 0), (10, 5), (0, 5)]],
                            [{"start": (2, 0), "end": (2, 5)}])
    layers = _entities(text)
    assert layers[dxf_io.CUT_LAYER] == 4        # closed loop
    assert layers[dxf_io.BEND_LAYER] == 1       # open line, not closed back on itself


def test_a_layer_with_no_definition_is_refused():
    with pytest.raises(ValueError, match="no layer definition"):
        dxf_io.write_layers({"SCRIBBLE": [[(0, 0), (1, 1)]]})


# ------------------------------------------------- exact interference --

def _cube(name, size=.04):
    doc_id = docs.create_document(name=name, resolution=48, owner_id=None).id
    ops.dispatch("add_primitive", doc_id, {"shape": "box", "params": {"size": V(size, size, size)}})
    return doc_id


def _two_cubes(offset_mm, size=.04):
    asm = assembly_service.create_assembly("clash")
    assembly_service.add_component(asm.id, _cube("A", size))
    asm = assembly_service.add_component(asm.id, _cube("B", size))
    moved = asm.components[-1]
    assembly_service.update_component(asm.id, moved.id, ComponentTransform(
        translate=Vector3(x=offset_mm / 1000.0, y=0, z=0),
        rotate_deg=Vector3(x=0, y=0, z=0), scale=1.0))
    return asm.id


@exact_only
def test_a_clash_is_measured_in_cubic_millimetres_not_sample_hits():
    """Two 40 mm cubes 30 mm apart share a 10x40x40 slab. The depth alone cannot
    tell a nick to be relieved from half a boss in the wrong place."""
    from app.services import interference

    report = interference.check_assembly(_two_cubes(30.0), clearance_mm=1.0)

    assert report["interferences"] == 1
    pair = report["pairs"][0]
    assert pair["penetration_mm"] == pytest.approx(10.0, abs=.5)
    assert pair["overlap_mm3"] == pytest.approx(10 * 40 * 40, rel=1e-3)
    assert report["overlap_mm3"] == pytest.approx(16000.0, rel=1e-3)


@exact_only
def test_a_deeper_clash_is_a_bigger_volume():
    """The figure has to track the geometry, not merely be present."""
    from app.services import interference

    shallow = interference.check_assembly(_two_cubes(38.0))["pairs"][0]["overlap_mm3"]
    deep = interference.check_assembly(_two_cubes(20.0))["pairs"][0]["overlap_mm3"]
    assert shallow == pytest.approx(2 * 40 * 40, rel=1e-3)
    assert deep == pytest.approx(20 * 40 * 40, rel=1e-3)
    assert deep > shallow


@exact_only
def test_parts_that_merely_sit_close_are_not_asked_for_a_volume():
    """`Common` on two bodies that do not touch is an OCC boolean returning a zero."""
    from app.services import interference

    report = interference.check_assembly(_two_cubes(40.5), clearance_mm=1.0)
    assert report["interferences"] == 0
    assert report["pairs"] and report["pairs"][0]["status"] == "clearance"
    assert "overlap_mm3" not in report["pairs"][0]
    assert report["overlap_mm3"] is None


# ----------------------------------------------------- multi-body parts --

def _two_blocks(name="two blocks"):
    doc_id = docs.create_document(name=name, resolution=64, owner_id=None).id
    ops.dispatch("add_primitive", doc_id, {"shape": "box",
                                           "params": {"size": V(.03, .03, .03),
                                                      "center": V(-.05, 0, 0)}})
    ops.dispatch("add_primitive", doc_id, {"shape": "box", "operation": "union",
                                           "params": {"size": V(.03, .03, .03),
                                                      "center": V(.05, 0, 0)}})
    return doc_id


def test_the_separate_solids_of_a_part_can_be_seen_and_measured():
    """"2 disconnected bodies" cannot be told from "the part fell in half"."""
    result = ops.execute("bodies", _two_blocks(), {"resolution": 64})["result"]

    assert result["count"] == 2
    assert result["total_volume_mm3"] == pytest.approx(2 * 27000, rel=.02)
    assert result["largest_fraction"] == pytest.approx(0.5, abs=.02)
    left, right = sorted(result["bodies"], key=lambda b: b["center_mm"][0])
    assert left["center_mm"][0] == pytest.approx(-50, abs=1)
    assert right["center_mm"][0] == pytest.approx(50, abs=1)
    for body in result["bodies"]:
        assert body["size_mm"] == pytest.approx([30, 30, 30], abs=1)


def test_one_solid_reports_as_one_body():
    result = ops.execute("bodies", _bored_plate(), {"resolution": 64})["result"]
    assert result["count"] == 1
    assert result["largest_fraction"] == 1.0
    assert result["message"] == "one connected body"


def test_an_intended_multi_body_part_stops_being_reported_as_damage():
    """The spec is where intent lives, so that is where the count is declared.

    Without it, two bodies is a warning and the guessed cause ("a subtraction may
    have severed the part") is simply wrong for a part that was meant to be two.
    """
    doc_id = _two_blocks()
    before = ops.execute("check_geometry", doc_id, {"resolution": 64})["result"]
    row = next(c for c in before["checks"] if c["id"] == "connectivity")
    assert row["status"] == "warn" and "severed" in row["detail"]

    ops.execute("set_spec", doc_id, {"requirements": [{"kind": "bodies", "value": 2}]})
    after = ops.execute("check_geometry", doc_id, {"resolution": 64})["result"]
    row = next(c for c in after["checks"] if c["id"] == "connectivity")
    assert row["status"] == "pass"
    assert "as specified" in row["detail"]
    assert ops.execute("verify_against_spec", doc_id, {})["result"]["score"] == 1.0


def test_a_part_that_falls_short_of_its_declared_count_still_warns():
    """Declaring an intent is not a way to switch the check off."""
    doc_id = _bored_plate()
    ops.execute("set_spec", doc_id, {"requirements": [{"kind": "bodies", "value": 2}]})
    report = ops.execute("check_geometry", doc_id, {"resolution": 64})["result"]
    row = next(c for c in report["checks"] if c["id"] == "connectivity")
    assert row["status"] == "warn"
    assert "the spec asks for 2" in row["detail"]


def test_mass_and_bounds_were_already_right_across_several_bodies():
    """Nothing here changes the sums — worth pinning, since the fix was about
    *seeing* the bodies rather than about measuring them."""
    doc_id = _two_blocks()
    doc = docs.get_document(doc_id)
    assert doc.derived.volume * 1e9 == pytest.approx(2 * 27000, rel=.02)
    box = ops.execute("bounding_box", doc_id, {})["result"]
    assert box["size_mm"]["x"] == pytest.approx(130, abs=1)
    assert math.isclose(ops.execute("mass_properties", doc_id, {})["result"]["volume_cm3"],
                        54.0, rel_tol=.02)


def test_the_bodies_operation_is_published_as_a_query():
    catalog = {e["name"]: e for e in ops.catalog()}
    assert catalog["bodies"]["kind"] == "query"
    assert catalog["bodies"]["schema"].get("required", []) == []

"""The flat pattern has to be manufacturable, not just reportable.

Covers the handoff to a shop: the blank outline, the DXF layer split, and the
fact that the cutting pipeline cuts the *blank* rather than a section through the
folded part.
"""

import pytest
from fastapi.testclient import TestClient

from main import app
from app.cad_kernel.io.dxf import BEND_LAYER, CUT_LAYER, polyline_to_segments, write_dxf
from app.cad_kernel.ops.sheet_metal import SheetMetalFeature, unfold_tree
from app.services import cad_operations as ops
from app.services import document_service as ds
from app.services.cam import profile_cut as pc


client = TestClient(app)

T, L, W, R = 0.0015, 0.10, 0.06, 0.002


def _bracket_doc(leg=0.02, angle=90):
    doc = ds.create_document(name="Bracket", units="mm", resolution=64, owner_id=None)
    ops.dispatch("add_sheet_metal", doc.id,
                 {"thickness": T, "length": L, "width": W, "bend_radius": R})
    ops.dispatch("add_flange", doc.id, {"edge": "x+", "length": leg, "angle": angle})
    return ds.get_document(doc.id)


# ------------------------------------------------------------------ outline --

def test_outline_is_a_closed_rectangle_of_the_blank():
    f = SheetMetalFeature(thickness=T, length=L, width=W, bend_radius=R,
                          flanges=[{"edge": "x+", "length": 0.02, "angle": 90}])
    out = f.flat_pattern_outline()
    pts = out["outline_mm"]
    assert pts[0] == pts[-1], "outline must close"
    assert len(pts) == 5
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    assert max(xs) == pytest.approx(out["blank_size_mm"]["x"])
    assert max(ys) == pytest.approx(out["blank_size_mm"]["y"])
    assert min(xs) == 0 and min(ys) == 0, "blank is placed at the origin for nesting"


def test_bend_line_spans_the_blank_perpendicular_to_its_flange():
    f = SheetMetalFeature(thickness=T, length=L, width=W, bend_radius=R,
                          flanges=[{"edge": "x+", "length": 0.02, "angle": 90}])
    out = f.flat_pattern_outline()
    (line,) = out["bend_lines_mm"]
    # An x-edge flange bends about a line of constant x, spanning the full height.
    assert line["start"][0] == pytest.approx(line["end"][0])
    assert line["start"][0] == pytest.approx(L * 1000.0)
    assert {line["start"][1], line["end"][1]} == {0.0, out["blank_size_mm"]["y"]}


def test_a_y_edge_flange_bends_about_the_other_axis():
    f = SheetMetalFeature(thickness=T, length=L, width=W, bend_radius=R,
                          flanges=[{"edge": "y+", "length": 0.02, "angle": 90}])
    (line,) = f.flat_pattern_outline()["bend_lines_mm"]
    assert line["start"][1] == pytest.approx(line["end"][1])


def test_a_flat_plate_has_no_bend_lines():
    out = SheetMetalFeature(thickness=T, length=L, width=W).flat_pattern_outline()
    assert out["bend_lines_mm"] == []


# ---------------------------------------------------------------------- DXF --

def test_polyline_to_segments_closes_an_open_loop():
    segs = polyline_to_segments([(0, 0), (1, 0), (1, 1)])
    assert len(segs) == 3
    assert tuple(segs[-1][1]) == (0, 0)


def test_dxf_separates_cut_geometry_from_bend_lines():
    """Bend lines on the cut layer would slice the blank in half."""
    f = SheetMetalFeature(thickness=T, length=L, width=W, bend_radius=R,
                          flanges=[{"edge": "x+", "length": 0.02, "angle": 90}])
    out = f.flat_pattern_outline()
    dxf = write_dxf([out["outline_mm"]], out["bend_lines_mm"])

    assert dxf.startswith("0\nSECTION")
    assert dxf.rstrip().endswith("EOF")
    assert dxf.count("LINE") >= 5
    # Four outline edges on CUT, one bend line on BEND (plus the layer table).
    assert dxf.count(f"\n{CUT_LAYER}\n") == 4 + 1
    assert dxf.count(f"\n{BEND_LAYER}\n") == 1 + 1


def test_dxf_endpoint_serves_a_downloadable_blank():
    doc = _bracket_doc()
    resp = client.get(f"/api/documents/{doc.id}/flat-pattern.dxf")
    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]
    assert resp.headers["content-disposition"].endswith("_flat.dxf")
    assert "EOF" in resp.text and CUT_LAYER in resp.text


def test_dxf_endpoint_explains_itself_on_a_non_sheet_metal_part():
    doc = ds.create_document(name="Solid", units="mm", resolution=32, owner_id=None)
    ops.dispatch("add_primitive", doc.id, {"shape": "box"})
    resp = client.get(f"/api/documents/{doc.id}/flat-pattern.dxf")
    assert resp.status_code == 422
    assert "sheet-metal" in resp.json()["detail"]


# ------------------------------------------------------- cutting the blank --

def test_unfold_tree_replaces_the_bent_body_with_its_blank():
    f = SheetMetalFeature(thickness=T, length=L, width=W, bend_radius=R,
                          flanges=[{"edge": "x+", "length": 0.02, "angle": 90}])
    tree = {"version": "brep-v1", "root": f.to_dict()}
    flat = unfold_tree(tree)

    assert flat["root"]["params"]["flanges"] == []
    assert flat["root"]["params"]["length"] == pytest.approx(f.flat_pattern()["blank_size"]["x"])
    # The original must not be touched.
    assert tree["root"]["params"]["flanges"] != []


def test_unfold_tree_passes_through_trees_without_sheet_metal():
    tree = {"version": "brep-v1", "root": {"type": "BoxFeature", "id": "b1",
                                           "params": {"size": {"x": 1, "y": 1, "z": 1},
                                                      "center": {"x": 0, "y": 0, "z": 0}}}}
    assert unfold_tree(tree) is tree


def test_profile_cut_cuts_the_blank_not_the_folded_silhouette():
    leg = 0.02
    doc = _bracket_doc(leg=leg)
    blank_x = ops.dispatch("flat_pattern", doc.id, {})["blank_size_mm"]["x"]
    folded_x = ops.dispatch("bounding_box", doc.id, {})["size_mm"]["x"]
    assert blank_x > folded_x, "fixture must actually distinguish the two"

    cut = pc.profile_cut(doc.id, kerf_mm=0.2)
    assert cut["size_mm"]["u"] == pytest.approx(blank_x, abs=0.6)
    assert cut["path_count"] >= 1


def test_cut_length_is_the_blank_perimeter():
    doc = _bracket_doc()
    fp = ops.dispatch("flat_pattern", doc.id, {})
    x, y = fp["blank_size_mm"]["x"], fp["blank_size_mm"]["y"]
    cut = pc.profile_cut(doc.id, kerf_mm=0.2)
    assert cut["cut_length_mm"] == pytest.approx(2 * (x + y), rel=0.05)


def test_nesting_inherits_the_unfolded_blank():
    """Nesting delegates to profile_cut, so it must lay out blanks."""
    from app.services.cam import nesting

    doc = _bracket_doc()
    blank_x = ops.dispatch("flat_pattern", doc.id, {})["blank_size_mm"]["x"]
    out = nesting.nest(doc.id, machine_id="tci-dynamicline-1530", quantity=2)
    part = out.get("part_size_mm") or out.get("size_mm") or {}
    if part:
        assert max(part.values()) == pytest.approx(blank_x, abs=1.0)


def test_flat_pattern_operation_reports_both_numbers_and_geometry():
    doc = _bracket_doc()
    fp = ops.dispatch("flat_pattern", doc.id, {})
    for key in ("blank_size_mm", "bends", "outline_mm", "bend_lines_mm", "thickness_mm"):
        assert key in fp, f"missing {key}"

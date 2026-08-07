"""2D engineering drawings (feature 01): multi-view dimensioned SVG."""

from fastapi.testclient import TestClient

from conftest import base_box
from main import app

client = TestClient(app)


def _bracket() -> str:
    r = client.post("/api/documents", json={"prompt": "", "resolution": 48})
    doc_id = r.json()["id"]
    # A plate with a through-hole so the top view has an inner contour.
    base_box(client, doc_id, size=(0.08, 0.05, 0.02))
    client.post(f"/api/documents/{doc_id}/op", json={
        "op": "add_primitive", "args": {"shape": "cylinder", "operation": "subtract",
                                        "params": {"radius": 0.008, "height": 0.05, "center": {"x": 0.02, "y": 0, "z": 0}}}})
    return doc_id


def test_drawing_has_views_dimensions_and_title_block():
    doc_id = _bracket()
    r = client.get(f"/api/documents/{doc_id}/drawing")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["views"]["front"] >= 1
    assert out["views"]["top"] >= 2  # outline + through-hole
    svg = out["svg"]
    assert svg.startswith("<svg")
    assert "FRONT" in svg and "TOP" in svg and "RIGHT" in svg
    assert "SECTION" in svg
    assert 'class="dim"' in svg  # dimension lines present
    assert "MATERIAL" in svg and "Aluminum 6061" in svg  # title block
    # Overall width dimension reflects the 80 mm plate.
    assert out["bbox_mm"]["width"] > 70

    raw = client.get(f"/api/documents/{doc_id}/drawing.svg")
    assert raw.status_code == 200
    assert raw.headers["content-type"].startswith("image/svg+xml")


def test_drawing_operation_for_agents():
    doc_id = _bracket()
    r = client.post(f"/api/documents/{doc_id}/op", json={"op": "generate_drawing", "args": {}})
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert result["svg_bytes"] > 0 and result["views"]["front"] >= 1
    names = {o["name"] for o in client.get("/api/operations").json()["operations"]}
    assert {"generate_drawing", "compute_fit", "set_tolerances"} <= names


# ── 02 GD&T & tolerances ──

def test_iso286_fit_matches_textbook():
    # 20 H7/g6 is a standard clearance fit: hole 20 +0.021/0, shaft 20 -0.007/-0.020.
    r = client.get("/api/documents/meta/fit", params={"nominal_mm": 20, "fit": "H7/g6"})
    assert r.status_code == 200, r.text
    f = r.json()
    assert f["type"] == "clearance"
    assert abs(f["hole"]["max"] - 20.021) < 0.002
    assert abs(f["shaft"]["min"] - 19.980) < 0.002
    assert 0.005 < f["min_clearance"] < 0.010  # ~0.007
    assert 0.038 < f["max_clearance"] < 0.044  # ~0.041


def test_tolerances_and_gdt_render_on_drawing():
    doc_id = _bracket()
    r = client.put(f"/api/documents/{doc_id}/tolerances", json={
        "general_tolerance": "m",
        "tolerances": [{"label": "Bore", "nominal_mm": 16, "kind": "fit", "spec": "H7/g6"}],
        "gdt": [{"feature": "Top face", "symbol": "flatness", "tolerance_mm": 0.05, "datums": ["A"]}]})
    assert r.status_code == 200, r.text
    assert r.json()["general_tolerance"] == "m"

    svg = client.get(f"/api/documents/{doc_id}/drawing").json()["svg"]
    assert "ISO 2768-m" in svg
    assert "Bore" in svg and "H7/g6" in svg
    assert "Top face" in svg  # GD&T frame rendered


# ------------------------------------------------- dimensional truthfulness --

def _sizes(doc_id):
    """(model bbox, traced silhouette) in mm for one document."""
    out = client.get(f"/api/documents/{doc_id}/drawing").json()
    return out["bbox_mm"], out["traced_mm"]


def test_the_traced_views_match_the_body_they_depict():
    """A dimension read off the print has to be the dimension of the part.

    The views used to be a boolean occupancy mask contoured at 0.5, which pinned
    the outline to cell boundaries; where a sample landed exactly on the surface
    `sdf <= 0` counted it inside and the silhouette gained half a cell. A 35 mm
    plate-and-boss came out 35.33 mm. Contouring the signed field interpolates
    the crossing instead.
    """
    doc_id = _bracket()
    model, traced = _sizes(doc_id)
    for axis in ("width", "depth", "height"):
        assert abs(traced[axis] - model[axis]) < 0.05, f"{axis}: {traced} vs {model}"


def test_a_body_whose_top_face_lands_on_a_sample_plane_is_not_oversized():
    """The exact case that produced the half-cell error: a boss flush with the grid top."""
    doc_id = client.post("/api/documents", json={"resolution": 48}).json()["id"]
    # The first union replaces the blank document's placeholder base; the second
    # unions onto it. Editing the placeholder instead would let it be replaced.
    client.post(f"/api/documents/{doc_id}/op", json={
        "op": "add_primitive", "args": {"shape": "box", "params": {
            "size": {"x": .08, "y": .05, "z": .01}, "center": {"x": 0, "y": 0, "z": 0}}}})
    client.post(f"/api/documents/{doc_id}/op", json={
        "op": "add_primitive", "args": {"shape": "cylinder", "params": {
            "radius": .012, "height": .03, "center": {"x": .02, "y": 0, "z": .015}}}})

    # Plate spans z -5..+5, boss top at +30 → 35 mm overall, with the boss top
    # landing exactly on a sample plane.
    model, traced = _sizes(doc_id)
    assert abs(model["height"] - 35.0) < 0.01
    assert abs(traced["height"] - 35.0) < 0.05


def test_the_printed_dimension_is_the_models_own_figure():
    doc_id = _bracket()
    out = client.get(f"/api/documents/{doc_id}/drawing").json()
    # 80 mm wide plate: the text against the dimension line, not a traced length.
    assert f'>{out["bbox_mm"]["width"]:.1f}<' in out["svg"]


def test_drawing_dimensions_agree_with_the_bounding_box_operation():
    """Two answers to 'how big is it' must not disagree."""
    doc_id = _bracket()
    bbox = client.post(f"/api/documents/{doc_id}/op",
                       json={"op": "bounding_box", "args": {}}).json()["result"]["size_mm"]
    model, _ = _sizes(doc_id)
    assert abs(model["width"] - bbox["x"]) < 0.01
    assert abs(model["depth"] - bbox["y"]) < 0.01
    assert abs(model["height"] - bbox["z"]) < 0.01

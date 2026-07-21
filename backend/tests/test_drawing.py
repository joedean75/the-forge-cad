"""2D engineering drawings (feature 01): multi-view dimensioned SVG."""

from fastapi.testclient import TestClient

from main import app

client = TestClient(app)


def _bracket() -> str:
    r = client.post("/api/documents", json={"prompt": "", "resolution": 48})
    doc_id = r.json()["id"]
    root = client.get(f"/api/documents/{doc_id}").json()["feature_tree"]["root"]["id"]
    # A plate with a through-hole so the top view has an inner contour.
    client.post(f"/api/documents/{doc_id}/op", json={
        "op": "set_feature_params",
        "args": {"feature_id": root, "params": {"size": {"x": 0.08, "y": 0.05, "z": 0.02}, "center": {"x": 0, "y": 0, "z": 0}}}})
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

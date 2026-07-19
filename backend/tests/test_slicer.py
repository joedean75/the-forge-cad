"""FDM slicer (roadmap: Machine = 3D-print pipeline): SDF slicing → G-code."""

from fastapi.testclient import TestClient

from main import app

client = TestClient(app)


def _small_part() -> str:
    r = client.post("/api/documents", json={"resolution": 48})
    doc_id = r.json()["id"]
    op = client.post(
        f"/api/documents/{doc_id}/op",
        json={"op": "add_primitive",
              "args": {"shape": "box", "operation": "union",
                       "params": {"size": {"x": 0.02, "y": 0.015, "z": 0.008},
                                  "center": {"x": 0, "y": 0, "z": 0.004}}}},
    )
    assert op.status_code == 200, op.text
    return doc_id


def test_slice_endpoint_produces_stats_preview_and_gcode():
    doc_id = _small_part()
    r = client.post(f"/api/documents/{doc_id}/slice", json={"layer_height": 0.3, "infill": 0.2})
    assert r.status_code == 200, r.text
    body = r.json()
    s = body["stats"]
    assert s["layers"] >= 2
    assert s["filament_mm"] > 0 and s["filament_g"] > 0
    assert s["estimated_minutes"] > 0
    assert body["preview"] and body["preview"][0]["walls"], "layer preview must carry wall polylines"

    g = client.get(f"/api/documents/{doc_id}/gcode")
    assert g.status_code == 200
    text = g.text
    # Printable RepRap-flavor G-code: relative E, heatup, homing, extrusion moves.
    assert "M83" in text and "M109" in text and "G28" in text
    assert any(line.startswith("G1 X") and "E" in line for line in text.splitlines())
    # Motion stays on a 220 mm bed (part is centered at 110,110).
    for line in text.splitlines():
        if line.startswith(("G0 X", "G1 X")):
            x = float(line.split("X")[1].split()[0])
            assert 0 <= x <= 220


def test_slice_part_operation_for_agents():
    doc_id = _small_part()
    r = client.post(
        f"/api/documents/{doc_id}/op",
        json={"op": "slice_part", "args": {"settings": {"layer_height": 0.4}}},
    )
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert "sliced into" in result["message"]
    assert result["stats"]["layers"] >= 2
    # Catalog advertises both new tools to agents.
    names = {o["name"] for o in client.get("/api/operations").json()["operations"]}
    assert {"slice_part", "print_part"} <= names


def test_top_and_bottom_layers_are_solid_skinned():
    """Production prints need closed surfaces: the first/last layers must be
    dense skin, while the mid-height of a tall part stays sparse infill."""
    doc_id = _small_part()
    r = client.post(f"/api/documents/{doc_id}/slice",
                    json={"layer_height": 0.2, "infill": 0.1, "skin_layers": 3})
    assert r.status_code == 200, r.text
    preview = r.json()["preview"]
    first, last = preview[0], preview[-1]
    mids = [p for p in preview if 4 <= p["layer"] <= r.json()["stats"]["layers"] - 3]
    assert first["skin"], "bottom layer must carry solid skin"
    assert last["skin"], "top layer must carry solid skin"
    assert mids and any(m["infill"] for m in mids), "interior layers keep sparse infill"
    assert all(not m["skin"] for m in mids), "fully-interior layers need no skin"


def test_gcode_404_before_slicing():
    doc_id = _small_part()
    assert client.get(f"/api/documents/{doc_id}/gcode").status_code == 404

"""Advanced modeling ops (revolve/mirror/sweep/loft) + 2D profile cutting CAM."""

from fastapi.testclient import TestClient

from conftest import base_box
from main import app

client = TestClient(app)


def _blank() -> str:
    return client.post("/api/documents", json={"resolution": 48}).json()["id"]


def _op(doc_id, op, args):
    r = client.post(f"/api/documents/{doc_id}/op", json={"op": op, "args": args})
    assert r.status_code == 200, r.text
    return r.json()


def test_revolve_makes_a_turned_part():
    doc_id = _blank()
    # Replace the placeholder base with a revolve: a ring (rect profile off-axis).
    r = _op(doc_id, "add_revolve", {
        "profile": {"type": "rect", "width": 0.01, "height": 0.03, "cx": 0.015, "cy": 0.0},
        "axis": "z", "angle": 360, "operation": "union"})
    assert r["result"]["feature_id"]
    doc = client.get(f"/api/documents/{doc_id}").json()
    assert doc["derived"]["error"] is None
    # Ring outer diameter ~ 40 mm (cx 15 + hw 5 = 20 mm radius).
    bx = doc["derived"]["bbox"]
    assert abs((bx["max"]["x"] - bx["min"]["x"]) - 0.04) < 3e-3


def test_mirror_creates_symmetry():
    doc_id = _blank()
    # Off-center box then mirror across yz → symmetric pair spanning ±.
    base_box(client, doc_id, size=(0.02, 0.02, 0.02), center=(0.03, 0, 0))
    _op(doc_id, "add_mirror", {"plane": "yz", "offset": 0.0})
    bx = client.get(f"/api/documents/{doc_id}").json()["derived"]["bbox"]
    # Now spans from -0.04 to +0.04 in x (two boxes centered at ±0.03, half 0.01).
    assert bx["min"]["x"] < -0.03 and bx["max"]["x"] > 0.03


def test_sweep_pipe_and_loft_adapter_evaluate():
    doc_id = _blank()
    _op(doc_id, "add_sweep", {"path": [[-0.03, 0, 0], [0.03, 0, 0], [0.03, 0, 0.04]],
                              "radius": 0.005, "operation": "union"})
    assert client.get(f"/api/documents/{doc_id}").json()["derived"]["error"] is None

    doc2 = _blank()
    _op(doc2, "add_loft", {"profile_a": {"type": "rect", "width": 0.04, "height": 0.04},
                           "profile_b": {"type": "circle", "radius": 0.012},
                           "plane": "xy", "offset_a": 0, "offset_b": 0.04, "operation": "union"})
    assert client.get(f"/api/documents/{doc2}").json()["derived"]["error"] is None


def test_profile_cut_generates_kerf_compensated_gcode():
    doc_id = _blank()
    base_box(client, doc_id, size=(0.08, 0.05, 0.003))
    _op(doc_id, "add_primitive", {"shape": "cylinder", "operation": "subtract",
        "params": {"radius": 0.006, "height": 0.02, "center": {"x": 0.02, "y": 0, "z": 0}}})

    r = client.get(f"/api/documents/{doc_id}/profile-cut", params={"kerf_mm": 0.2})
    assert r.status_code == 200, r.text
    cut = r.json()
    assert cut["path_count"] >= 2  # outer outline + the hole
    assert cut["cut_length_mm"] > 0
    g = client.get(f"/api/documents/{doc_id}/profile-cut.gcode")
    assert g.status_code == 200
    assert "M3" in g.text and "M5" in g.text and "G1" in g.text  # beam on/off + cut moves


def test_new_ops_registered():
    names = {o["name"] for o in client.get("/api/operations").json()["operations"]}
    assert {"add_revolve", "add_mirror", "add_sweep", "add_loft", "profile_cut"} <= names

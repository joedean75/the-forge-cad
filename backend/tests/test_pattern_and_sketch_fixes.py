"""Bug fixes: pattern a selected feature (target) + sketch zeroes on the face."""

from fastapi.testclient import TestClient

from conftest import base_box
from main import app
from app.services import feature_ops
from app.services.datum import resolve_face_frame

client = TestClient(app)


def _plate_with_hole():
    doc_id = client.post("/api/documents", json={"resolution": 48}).json()["id"]
    root = base_box(client, doc_id, size=(0.1, 0.06, 0.02))
    r = client.post(f"/api/documents/{doc_id}/op", json={
        "op": "add_primitive", "args": {"shape": "cylinder", "operation": "subtract",
                                        "params": {"radius": 0.005, "height": 0.05, "center": {"x": -0.04, "y": 0, "z": 0}}}})
    return doc_id, root, r.json()["result"]["feature_id"]


def test_pattern_targets_a_single_feature_not_the_whole_body():
    doc_id, root, hole = _plate_with_hole()
    mass_before = client.get(f"/api/documents/{doc_id}").json()["derived"]["mass"]

    # Pattern just the hole → four holes, more material removed.
    r = client.post(f"/api/documents/{doc_id}/op", json={
        "op": "add_modifier", "args": {"modifier": "pattern", "target": hole,
                                       "params": {"kind": "linear", "count": 4, "spacing": 0.025, "axis": {"x": 1, "y": 0, "z": 0}}}})
    assert r.status_code == 200, r.text
    doc = client.get(f"/api/documents/{doc_id}").json()
    assert doc["derived"]["mass"] < mass_before  # more holes = less mass

    # The Pattern wraps the cylinder, and the root is still the boolean.
    def find(n, t):
        if n["type"] == t:
            return n
        for c in (n.get("params") or {}).get("children") or []:
            f = find(c, t)
            if f:
                return f
    pat = find(doc["feature_tree"]["root"], "PatternFeature")
    assert pat is not None
    assert pat["params"]["children"][0]["type"] == "CylinderFeature"
    assert doc["feature_tree"]["root"]["type"] == "BooleanFeature"


def test_pattern_unknown_target_is_404():
    doc_id, root, hole = _plate_with_hole()
    r = client.post(f"/api/documents/{doc_id}/op", json={
        "op": "add_modifier", "args": {"modifier": "pattern", "target": "nope"}})
    assert r.status_code == 404


def test_sketch_on_face_zeroes_on_the_face_center():
    # Off-center base box, centered at x = 50 mm.
    doc_id = client.post("/api/documents", json={"resolution": 48}).json()["id"]
    root = base_box(client, doc_id, size=(0.04, 0.04, 0.02), center=(0.05, 0, 0))
    tree = client.get(f"/api/documents/{doc_id}").json()["feature_tree"]

    plane, offset, origin = resolve_face_frame(tree, root, "top")
    assert plane == "xy"
    assert abs(offset - 0.01) < 1e-6            # top face at +10 mm
    assert abs(origin[0] - 0.05) < 1e-4         # in-plane origin = face centre x = 50 mm
    assert abs(origin[1] - 0.0) < 1e-4

    # Placing a sketch on that face bakes plane_origin = the face centre, so a
    # profile drawn at sketch (0,0) lands at x = 50 mm, not the world origin.
    from app.services.cad_operations import _resolve_on_face
    params = {}
    _resolve_on_face(tree, {"on_face": {"feature_id": root, "face": "top"}}, params)
    assert abs(params["plane_origin"][0] - 0.05) < 1e-4  # centered on the face
    assert abs(params["plane_origin"][1] - 0.0) < 1e-4
    assert params["plane"] == "xy" and abs(params["offset"] - 0.01) < 1e-6

    # And the profile is genuinely centered there: an ExtrudeFeature at the face
    # origin with a small profile evaluates to a solid whose centre is at x≈50 mm.
    from app.cad_kernel.evaluator.sdf import ExtrudeSDF
    sdf = ExtrudeSDF({"type": "circle", "radius": 0.008, "cx": 0, "cy": 0},
                     "xy", 0.01, 0.01, plane_origin=params["plane_origin"])
    b = sdf.bounds()
    assert abs((b.min[0] + b.max[0]) / 2 - 0.05) < 1e-6  # profile centered on the face

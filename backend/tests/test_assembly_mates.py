"""Assembly mates (04) + interference/clearance detection (08)."""

from fastapi.testclient import TestClient

from main import app

client = TestClient(app)


def _part(size, center) -> str:
    r = client.post("/api/documents", json={"resolution": 48})
    doc_id = r.json()["id"]
    root = client.get(f"/api/documents/{doc_id}").json()["feature_tree"]["root"]["id"]
    client.post(f"/api/documents/{doc_id}/op", json={
        "op": "set_feature_params",
        "args": {"feature_id": root, "params": {"size": {"x": size[0], "y": size[1], "z": size[2]},
                                                "center": {"x": center[0], "y": center[1], "z": center[2]}}}})
    return doc_id


def _assembly(part_ids):
    a = client.post("/api/assemblies", json={"name": "rig"}).json()
    aid = a["id"]
    comp_ids = []
    for pid in part_ids:
        a = client.post(f"/api/assemblies/{aid}/components", json={"part_id": pid}).json()
        comp_ids.append(a["components"][-1]["id"])
    return aid, comp_ids


def test_coincident_mate_stacks_parts():
    # A 40mm-tall base centered at origin, and a 20mm block to stack on top.
    base = _part((0.06, 0.06, 0.04), (0, 0, 0))
    block = _part((0.02, 0.02, 0.02), (0, 0, 0))
    aid, (cb, cm) = _assembly([base, block])

    # Coincident: block's -Z (min) face onto base's +Z (max) face → sits on top.
    r = client.post(f"/api/assemblies/{aid}/mates", json={
        "type": "coincident", "base": cb, "moved": cm,
        "axis": "z", "base_face": "max", "moved_face": "min", "offset_mm": 0})
    assert r.status_code == 200, r.text
    a = r.json()
    moved = next(c for c in a["components"] if c["id"] == cm)
    # base top = +0.02 m; block half-height 0.01 → block center z = 0.03.
    assert abs(moved["transform"]["translate"]["z"] - 0.03) < 1e-4
    # Centered on the base in x/y.
    assert abs(moved["transform"]["translate"]["x"]) < 1e-6
    assert abs(moved["transform"]["translate"]["y"]) < 1e-6


def test_concentric_mate_makes_parts_coaxial():
    base = _part((0.06, 0.06, 0.02), (0.03, 0.0, 0.0))  # offset in x
    pin = _part((0.01, 0.01, 0.05), (0, 0, 0))
    aid, (cb, cm) = _assembly([base, pin])
    r = client.post(f"/api/assemblies/{aid}/mates", json={
        "type": "concentric", "base": cb, "moved": cm, "axis": "z", "offset_mm": 0})
    a = r.json()
    moved = next(c for c in a["components"] if c["id"] == cm)
    # Pin centered on base center in x/y (base center x = 0.03).
    assert abs(moved["transform"]["translate"]["x"] - 0.03) < 1e-4
    assert abs(moved["transform"]["translate"]["y"]) < 1e-4


def test_interference_detects_overlap_and_clearance():
    a1 = _part((0.04, 0.04, 0.04), (0, 0, 0))
    a2 = _part((0.04, 0.04, 0.04), (0, 0, 0))
    aid, (c1, c2) = _assembly([a1, a2])
    # Overlap them: move c2 only 10mm on x (parts are 40mm wide → 30mm overlap).
    client.put(f"/api/assemblies/{aid}/components/{c2}", json={
        "transform": {"translate": {"x": 0.01, "y": 0, "z": 0}, "rotate_deg": {"x": 0, "y": 0, "z": 0}, "scale": 1}})
    r = client.get(f"/api/assemblies/{aid}/interference")
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["interferences"] >= 1
    assert not rep["clear"]
    assert rep["pairs"][0]["penetration_mm"] > 0

    # Separate them well beyond the threshold → clear.
    client.put(f"/api/assemblies/{aid}/components/{c2}", json={
        "transform": {"translate": {"x": 0.2, "y": 0, "z": 0}, "rotate_deg": {"x": 0, "y": 0, "z": 0}, "scale": 1}})
    rep2 = client.get(f"/api/assemblies/{aid}/interference").json()
    assert rep2["interferences"] == 0
    assert rep2["clear"]

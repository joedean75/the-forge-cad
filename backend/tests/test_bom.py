"""Bill of Materials (feature 05): grouped parts, mass and cost rollups."""

from fastapi.testclient import TestClient

from main import app

client = TestClient(app)


def _part():
    r = client.post("/api/documents", json={"resolution": 48})
    doc_id = r.json()["id"]
    root = client.get(f"/api/documents/{doc_id}").json()["feature_tree"]["root"]["id"]
    client.post(f"/api/documents/{doc_id}/op", json={
        "op": "set_feature_params",
        "args": {"feature_id": root, "params": {"size": {"x": 0.03, "y": 0.03, "z": 0.03}, "center": {"x": 0, "y": 0, "z": 0}}}})
    return doc_id


def test_bom_groups_instances_and_rolls_up_mass():
    base = _part()
    wheel = _part()
    client.post(f"/api/documents/{wheel}/material", json={"material_id": "pla"})
    a = client.post("/api/assemblies", json={"name": "cart"}).json()
    aid = a["id"]
    client.post(f"/api/assemblies/{aid}/components", json={"part_id": base})
    for _ in range(4):  # four wheel instances
        client.post(f"/api/assemblies/{aid}/components", json={"part_id": wheel})

    bom = client.get(f"/api/assemblies/{aid}/bom").json()
    assert bom["component_count"] == 5
    assert bom["line_count"] == 2  # two distinct parts
    wheel_line = next(l for l in bom["lines"] if l["part_id"] == wheel)
    assert wheel_line["quantity"] == 4
    assert wheel_line["material"] == "PLA"
    # Extended mass is per-unit * qty.
    assert abs(wheel_line["ext_mass_g"] - wheel_line["unit_mass_g"] * 4) < 0.1
    assert bom["total_mass_g"] > 0

    csv = client.get(f"/api/assemblies/{aid}/bom.csv")
    assert csv.status_code == 200
    assert "Part Number" in csv.text and "TOTAL" in csv.text

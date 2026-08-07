"""Production-workflow features: materials (06), parameters (03), mass props (10)."""

from fastapi.testclient import TestClient

from conftest import base_box, body_id
from main import app
from app.services import document_service as ds

client = TestClient(app)


def _box_doc(size=(0.05, 0.04, 0.03)) -> str:
    r = client.post("/api/documents", json={"resolution": 48})
    doc_id = r.json()["id"]
    base_box(client, doc_id, size=size)
    return doc_id


# ── 06 Materials ──

def test_material_catalog_and_assignment_changes_mass():
    mats = client.get("/api/documents/meta/materials").json()["materials"]
    ids = {m["id"] for m in mats}
    assert {"al-6061", "steel-304", "ti-6al4v", "pla"} <= ids

    doc_id = _box_doc()
    al = client.get(f"/api/documents/{doc_id}").json()["derived"]["mass"]
    r = client.post(f"/api/documents/{doc_id}/material", json={"material_id": "steel-304"})
    assert r.status_code == 200, r.text
    steel = r.json()["derived"]["mass"]
    # Steel (8000) is ~2.96x denser than the al-6061 default (2700).
    assert steel > al
    assert abs(steel / al - 8000 / 2700) < 0.05


# ── 03 Parameters ──

def test_parameters_drive_feature_dimensions():
    doc_id = _box_doc()
    root = body_id(client.get(f"/api/documents/{doc_id}").json()["feature_tree"])
    # Bind the box width to a parameter expression (mm authored, metres applied).
    client.post(f"/api/documents/{doc_id}/op", json={
        "op": "set_feature_params",
        "args": {"feature_id": root, "params": {"size": {"x": "=width", "y": "=width/2", "z": 0.03}}}})
    r = client.put(f"/api/documents/{doc_id}/parameters", json={
        "parameters": [{"name": "width", "expr": "80", "unit": "mm"}]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["parameters"][0]["value"] == 80.0
    bbox = body["derived"]["bbox"]
    assert abs((bbox["max"]["x"] - bbox["min"]["x"]) - 0.08) < 1e-4  # width = 80 mm
    assert abs((bbox["max"]["y"] - bbox["min"]["y"]) - 0.04) < 1e-4  # width/2 = 40 mm

    # Change one parameter → geometry follows.
    r2 = client.put(f"/api/documents/{doc_id}/parameters", json={
        "parameters": [{"name": "width", "expr": "120", "unit": "mm"}]})
    bbox2 = r2.json()["derived"]["bbox"]
    assert abs((bbox2["max"]["x"] - bbox2["min"]["x"]) - 0.12) < 1e-4


def test_bad_parameter_expression_is_422():
    doc_id = _box_doc()
    r = client.put(f"/api/documents/{doc_id}/parameters", json={
        "parameters": [{"name": "w", "expr": "nope * 2", "unit": "mm"}]})
    assert r.status_code == 422


# ── 10 Mass properties ──

def test_mass_properties_reports_cog_and_inertia():
    doc_id = _box_doc(size=(0.06, 0.04, 0.02))
    client.post(f"/api/documents/{doc_id}/material", json={"material_id": "al-6061"})
    mp = client.get(f"/api/documents/{doc_id}/mass-properties").json()
    # Box centered at origin → COG at ~origin.
    assert all(abs(v) < 1.0 for v in mp["center_of_mass_mm"])
    # Volume ~ 60*40*20 mm = 48 cm^3.
    assert abs(mp["volume_cm3"] - 48.0) < 2.0
    assert mp["mass_kg"] > 0
    # Inertia tensor is 3x3, diagonal-dominant for an axis-aligned box.
    I = mp["inertia_kg_mm2"]
    assert len(I) == 3 and len(I[0]) == 3
    assert all(I[i][i] > 0 for i in range(3))


# ── 07 Configurations ──

def test_configurations_produce_size_variants():
    doc_id = _box_doc()
    root = body_id(client.get(f"/api/documents/{doc_id}").json()["feature_tree"])
    client.post(f"/api/documents/{doc_id}/op", json={
        "op": "set_feature_params",
        "args": {"feature_id": root, "params": {"size": {"x": "=w", "y": "=w", "z": 0.02}}}})
    client.put(f"/api/documents/{doc_id}/parameters", json={"parameters": [{"name": "w", "expr": "40", "unit": "mm"}]})

    r = client.put(f"/api/documents/{doc_id}/configurations", json={
        "configurations": [{"name": "S", "overrides": {"w": "40"}}, {"name": "L", "overrides": {"w": "100"}}],
        "active": "L"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["active_configuration"] == "L"
    bx = body["derived"]["bbox"]
    assert abs((bx["max"]["x"] - bx["min"]["x"]) - 0.10) < 1e-4  # L → 100 mm

    r2 = client.post(f"/api/documents/{doc_id}/configurations/activate", json={"name": "S"})
    bx2 = r2.json()["derived"]["bbox"]
    assert abs((bx2["max"]["x"] - bx2["min"]["x"]) - 0.04) < 1e-4  # S → 40 mm

    # Back to base parameters.
    r3 = client.post(f"/api/documents/{doc_id}/configurations/activate", json={"name": None})
    assert r3.json()["active_configuration"] is None


def test_operations_registered_for_agents():
    names = {o["name"] for o in client.get("/api/operations").json()["operations"]}
    assert {"list_materials", "set_material", "set_parameters", "get_parameters", "mass_properties",
            "set_configurations", "activate_configuration", "list_configurations"} <= names

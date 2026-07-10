"""Tests for the DesignDocument rearchitecture endpoints."""

from fastapi.testclient import TestClient

from main import app


client = TestClient(app)


def _create_doc(prompt: str = "") -> str:
    resp = client.post("/api/documents", json={"prompt": prompt, "resolution": 64})
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def test_create_document_from_prompt():
    doc_id = _create_doc("box 0.1x0.05x0.03 m")
    resp = client.get(f"/api/documents/{doc_id}")
    assert resp.status_code == 200
    doc = resp.json()
    assert doc["id"] == doc_id
    assert doc["derived"]["mesh_url"].startswith("data:")
    assert doc["derived"]["volume"] > 0


def test_list_documents():
    doc_id = _create_doc("sphere radius 0.05 m")
    resp = client.get("/api/documents")
    assert resp.status_code == 200
    docs = resp.json()
    assert any(d["id"] == doc_id for d in docs)


def test_update_document_feature_tree():
    doc_id = _create_doc("box 0.1x0.1x0.1 m")
    get_resp = client.get(f"/api/documents/{doc_id}")
    tree = get_resp.json()["feature_tree"]
    tree["root"]["params"]["size"]["x"] = 0.2

    resp = client.put(
        f"/api/documents/{doc_id}",
        json={"feature_tree": tree, "resolution": 64},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["provenance"] == "mixed"


def test_document_simulate_endpoint():
    doc_id = _create_doc("box 0.1x0.05x0.03 m")
    resp = client.post(f"/api/documents/{doc_id}/simulate?resolution=64")
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert "max_stress" in result
    assert "safety_factor" in result


def test_document_dfm_endpoint():
    doc_id = _create_doc("box 0.1x0.05x0.03 m")
    resp = client.post(f"/api/documents/{doc_id}/dfm?build_axis=z&resolution=64")
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["process"] == "LPBF"
    assert 0 <= result["printability_score"] <= 100
    assert len(result["checks"]) > 0
    assert result["quote"]["total"] >= 0


def test_document_step_export():
    doc_id = _create_doc("box 0.1x0.05x0.03 m")
    resp = client.get(f"/api/documents/{doc_id}/step?resolution=64")
    assert resp.status_code == 200, resp.text
    assert "ISO-10303-21" in resp.text
    assert "MANIFOLD_SOLID_BREP" in resp.text


def test_delete_document():
    doc_id = _create_doc("box 0.05x0.05x0.05 m")
    resp = client.delete(f"/api/documents/{doc_id}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "deleted"
    assert client.get(f"/api/documents/{doc_id}").status_code == 404

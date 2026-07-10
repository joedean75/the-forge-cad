"""Tests for the CAD router list/get endpoints."""

from fastapi.testclient import TestClient

from main import app


client = TestClient(app)


def test_list_and_get_candidates():
    resp = client.post("/api/text-to-cad/generate", json={"prompt": "cube 35 mm", "resolution": 64})
    assert resp.status_code == 200
    candidate_id = resp.json()["id"]

    list_resp = client.get("/api/cad/candidates")
    assert list_resp.status_code == 200
    ids = [c["id"] for c in list_resp.json()]
    assert candidate_id in ids

    get_resp = client.get(f"/api/cad/candidates/{candidate_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == candidate_id


def test_list_and_get_assemblies():
    resp = client.post("/api/assembly/generate", json={"prompt": "truss span 0.8 m", "resolution": 64})
    assert resp.status_code == 200
    assembly_id = resp.json()["id"]

    list_resp = client.get("/api/cad/assemblies")
    assert list_resp.status_code == 200
    ids = [a["id"] for a in list_resp.json()]
    assert assembly_id in ids

    get_resp = client.get(f"/api/cad/assemblies/{assembly_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == assembly_id

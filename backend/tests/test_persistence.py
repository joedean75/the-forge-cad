"""Tests for disk persistence of candidates and assemblies."""

from fastapi.testclient import TestClient

from main import app
from app.services.persistence import list_candidates, list_assemblies


client = TestClient(app)


def test_candidate_persists_across_restarts():
    resp = client.post("/api/text-to-cad/generate", json={"prompt": "cube 40 mm", "resolution": 64})
    assert resp.status_code == 200
    candidate_id = resp.json()["id"]
    assert candidate_id in list_candidates()

    # A fresh app instance (via a new TestClient) should restore the candidate.
    fresh = TestClient(app)
    export = fresh.get(f"/api/export/{candidate_id}/step")
    assert export.status_code == 200
    assert "ISO-10303-21" in export.text


def test_assembly_persists_across_restarts():
    resp = client.post("/api/assembly/generate", json={"prompt": "frame 0.8 m", "resolution": 64})
    assert resp.status_code == 200
    assembly_id = resp.json()["id"]
    assert assembly_id in list_assemblies()

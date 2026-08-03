"""Tests for durable persistence of documents and assemblies.

The point of these is the *restart* path: derived products (meshes, STEP) are
never persisted, so a fresh app instance has to restore the record from the
datastore and re-evaluate the solid lazily on first access.
"""

from fastapi.testclient import TestClient

from main import app
from app.services.persistence import list_assembly_docs, list_documents


client = TestClient(app)


def test_document_persists_across_restarts():
    resp = client.post("/api/documents", json={"prompt": "cube 40 mm", "resolution": 64})
    assert resp.status_code == 200
    doc_id = resp.json()["id"]
    assert doc_id in list_documents()

    # A fresh app instance (new TestClient) must restore the document and
    # re-derive geometry that was deliberately not persisted.
    fresh = TestClient(app)
    got = fresh.get(f"/api/documents/{doc_id}")
    assert got.status_code == 200
    assert "/mesh/" in got.json()["derived"]["mesh_url"]

    export = fresh.get(f"/api/documents/{doc_id}/step")
    assert export.status_code == 200
    assert "ISO-10303-21" in export.text


def test_assembly_persists_across_restarts():
    resp = client.post("/api/assemblies", json={"name": "Persisted rig"})
    assert resp.status_code == 200
    assembly_id = resp.json()["id"]
    assert assembly_id in list_assembly_docs()

    fresh = TestClient(app)
    got = fresh.get(f"/api/assemblies/{assembly_id}")
    assert got.status_code == 200
    assert got.json()["name"] == "Persisted rig"

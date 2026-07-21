"""Revision & release management (feature 09): lifecycle states + released revisions."""

from fastapi.testclient import TestClient

from main import app

client = TestClient(app)


def _doc() -> str:
    return client.post("/api/documents", json={"resolution": 48}).json()["id"]


def test_lifecycle_state_transitions():
    doc_id = _doc()
    assert client.get(f"/api/documents/{doc_id}").json()["lifecycle_state"] == "draft"
    r = client.post(f"/api/documents/{doc_id}/state", json={"state": "in_review"})
    assert r.status_code == 200
    assert r.json()["lifecycle_state"] == "in_review"
    assert client.post(f"/api/documents/{doc_id}/state", json={"state": "bogus"}).status_code == 422


def test_release_assigns_sequential_letters_and_snapshots():
    doc_id = _doc()
    a = client.post(f"/api/documents/{doc_id}/revisions", json={"notes": "first release"}).json()
    b = client.post(f"/api/documents/{doc_id}/revisions", json={"notes": "fixed hole"}).json()
    assert a["letter"] == "A"
    assert b["letter"] == "B"

    doc = client.get(f"/api/documents/{doc_id}").json()
    assert doc["lifecycle_state"] == "released"

    revs = client.get(f"/api/documents/{doc_id}/revisions").json()
    assert [r["letter"] for r in revs] == ["A", "B"]
    assert revs[0]["notes"] == "first release"
    assert all(r["state"] == "released" for r in revs)


def test_revision_snapshot_is_immutable_to_later_edits():
    doc_id = _doc()
    root = client.get(f"/api/documents/{doc_id}").json()["feature_tree"]["root"]["id"]
    client.post(f"/api/documents/{doc_id}/revisions", json={"notes": "rev A geometry"})
    # Edit the live part after releasing.
    client.post(f"/api/documents/{doc_id}/op", json={
        "op": "add_primitive", "args": {"shape": "cylinder", "operation": "union",
                                        "params": {"radius": 0.01, "height": 0.05, "center": {"x": 0, "y": 0, "z": 0}}}})
    # The released revision A still holds the original single-feature tree.
    from app.services import document_service as ds
    doc = ds.get_document(doc_id)
    rev_a = doc.revisions[0]
    assert rev_a.feature_tree["root"]["id"] == root  # unchanged snapshot


def test_revision_ops_registered():
    names = {o["name"] for o in client.get("/api/operations").json()["operations"]}
    assert {"release_revision", "set_lifecycle_state", "list_revisions"} <= names

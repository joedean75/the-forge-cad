"""Team collaboration: version history/branching + role-based document sharing."""

from fastapi.testclient import TestClient

from conftest import http_part
from main import app

client = TestClient(app)


def _user(email):
    r = client.post("/api/auth/signup", json={"email": email, "password": "supersecret1"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _doc(hdr):
    return http_part(client, (0.05, 0.05, 0.05), resolution=32, headers=hdr)


def _add_cyl(doc_id, hdr):
    return client.post(
        f"/api/documents/{doc_id}/op",
        json={"op": "add_primitive", "args": {"shape": "cylinder", "operation": "union"}},
        headers=hdr,
    )


# -- version history ---------------------------------------------------------

def test_snapshot_list_and_restore():
    alice = _user("ver-alice@example.com")
    doc_id = _doc(alice)
    original_tree = client.get(f"/api/documents/{doc_id}", headers=alice).json()["feature_tree"]

    # Snapshot the current state, then mutate the geometry.
    v = client.post(f"/api/documents/{doc_id}/versions", json={"label": "baseline"}, headers=alice)
    assert v.status_code == 200, v.text
    version_id = v.json()["id"]

    _add_cyl(doc_id, alice)
    mutated = client.get(f"/api/documents/{doc_id}", headers=alice).json()["feature_tree"]
    assert mutated != original_tree

    versions = client.get(f"/api/documents/{doc_id}/versions", headers=alice).json()
    assert any(x["id"] == version_id and x["label"] == "baseline" for x in versions)

    # Restore returns geometry to the snapshot; the restore itself is snapshotted.
    restored = client.post(f"/api/documents/{doc_id}/versions/{version_id}/restore", headers=alice)
    assert restored.status_code == 200, restored.text
    assert restored.json()["feature_tree"] == original_tree
    after = client.get(f"/api/documents/{doc_id}/versions", headers=alice).json()
    assert len(after) == len(versions) + 1  # auto-save-before-restore added


def test_branch_creates_independent_owned_document():
    alice = _user("branch-alice@example.com")
    doc_id = _doc(alice)
    version_id = client.post(f"/api/documents/{doc_id}/versions", json={"label": "b"}, headers=alice).json()["id"]

    br = client.post(
        f"/api/documents/{doc_id}/versions/{version_id}/branch",
        json={"name": "Branch A", "resolution": 32},
        headers=alice,
    )
    assert br.status_code == 200, br.text
    branch_id = br.json()["id"]
    assert branch_id != doc_id
    assert br.json()["name"] == "Branch A"
    ids = {d["id"] for d in client.get("/api/documents", headers=alice).json()}
    assert branch_id in ids and doc_id in ids


# -- sharing / roles ---------------------------------------------------------

def test_viewer_can_read_but_not_edit():
    alice = _user("share-owner@example.com")
    bob = _user("share-viewer@example.com")
    doc_id = _doc(alice)

    shared = client.post(
        f"/api/documents/{doc_id}/collaborators",
        json={"email": "share-viewer@example.com", "role": "viewer"},
        headers=alice,
    )
    assert shared.status_code == 200, shared.text

    # Bob sees it in his listing, marked shared + viewer.
    listing = client.get("/api/documents", headers=bob).json()
    entry = next((d for d in listing if d["id"] == doc_id), None)
    assert entry and entry["role"] == "viewer" and entry["shared"] is True

    assert client.get(f"/api/documents/{doc_id}", headers=bob).status_code == 200
    # Viewer cannot mutate, delete, or manage sharing.
    assert _add_cyl(doc_id, bob).status_code == 403
    assert client.put(f"/api/documents/{doc_id}", json={"name": "x"}, headers=bob).status_code == 403
    assert client.delete(f"/api/documents/{doc_id}", headers=bob).status_code == 403
    assert client.post(
        f"/api/documents/{doc_id}/collaborators",
        json={"email": "someone@example.com", "role": "viewer"},
        headers=bob,
    ).status_code == 403


def test_editor_can_mutate():
    alice = _user("edit-owner@example.com")
    bob = _user("edit-editor@example.com")
    doc_id = _doc(alice)
    client.post(
        f"/api/documents/{doc_id}/collaborators",
        json={"email": "edit-editor@example.com", "role": "editor"},
        headers=alice,
    )
    assert _add_cyl(doc_id, bob).status_code == 200
    # …but an editor still cannot delete (owner-only).
    assert client.delete(f"/api/documents/{doc_id}", headers=bob).status_code == 403


def test_unshared_and_revoked_access():
    alice = _user("rev-owner@example.com")
    bob = _user("rev-collab@example.com")
    carol = _user("rev-stranger@example.com")
    doc_id = _doc(alice)

    # A stranger can't see it at all.
    assert client.get(f"/api/documents/{doc_id}", headers=carol).status_code == 404

    # Share then revoke bob.
    bob_id = client.get("/api/auth/me", headers=bob).json()["id"]
    client.post(
        f"/api/documents/{doc_id}/collaborators",
        json={"email": "rev-collab@example.com", "role": "viewer"},
        headers=alice,
    )
    assert client.get(f"/api/documents/{doc_id}", headers=bob).status_code == 200
    client.delete(f"/api/documents/{doc_id}/collaborators/{bob_id}", headers=alice)
    assert client.get(f"/api/documents/{doc_id}", headers=bob).status_code == 404


def test_share_with_unknown_email_is_rejected():
    alice = _user("unknown-share@example.com")
    doc_id = _doc(alice)
    r = client.post(
        f"/api/documents/{doc_id}/collaborators",
        json={"email": "nobody@nowhere.example", "role": "viewer"},
        headers=alice,
    )
    assert r.status_code == 400

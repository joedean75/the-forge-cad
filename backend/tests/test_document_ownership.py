"""Multi-tenancy: a document is private to the account that created it.

These are the load-bearing guarantees for a subscription product — one tenant
must never read, mutate, or even learn of another tenant's designs.
"""

from fastapi.testclient import TestClient

from conftest import http_part
from main import app
from app.services import document_service

client = TestClient(app)


def _user(email):
    r = client.post("/api/auth/signup", json={"email": email, "password": "supersecret1"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _make_doc(hdr):
    return http_part(client, (0.05, 0.05, 0.05), resolution=32, headers=hdr)


def test_document_is_owned_by_creator():
    alice = _user("alice-own@example.com")
    doc_id = _make_doc(alice)
    doc = client.get(f"/api/documents/{doc_id}", headers=alice).json()
    assert doc["owner_id"]  # stamped with an owner
    # Owner can read it back.
    assert client.get(f"/api/documents/{doc_id}", headers=alice).status_code == 200


def test_other_tenant_cannot_read_or_list():
    alice = _user("alice-list@example.com")
    bob = _user("bob-list@example.com")
    doc_id = _make_doc(alice)

    # Bob's listing must not include Alice's document.
    bob_ids = {d["id"] for d in client.get("/api/documents", headers=bob).json()}
    assert doc_id not in bob_ids
    # Alice's listing does.
    alice_ids = {d["id"] for d in client.get("/api/documents", headers=alice).json()}
    assert doc_id in alice_ids

    # Direct fetch is 404 (existence not leaked), not 403.
    assert client.get(f"/api/documents/{doc_id}", headers=bob).status_code == 404


def test_other_tenant_cannot_mutate():
    alice = _user("alice-mut@example.com")
    bob = _user("bob-mut@example.com")
    doc_id = _make_doc(alice)

    # Every mutating surface is blocked for a non-owner.
    op = client.post(
        f"/api/documents/{doc_id}/op",
        json={"op": "add_primitive", "args": {"shape": "cylinder", "operation": "union"}},
        headers=bob,
    )
    assert op.status_code == 404, op.text
    assert client.put(f"/api/documents/{doc_id}", json={"name": "hijacked"}, headers=bob).status_code == 404
    assert client.post(f"/api/documents/{doc_id}/evaluate", headers=bob).status_code == 404
    assert client.delete(f"/api/documents/{doc_id}", headers=bob).status_code == 404

    # …and the document is untouched: the owner still sees the original name.
    assert client.get(f"/api/documents/{doc_id}", headers=alice).json()["name"] != "hijacked"


def test_owner_can_mutate_own_document():
    alice = _user("alice-selfmut@example.com")
    doc_id = _make_doc(alice)
    op = client.post(
        f"/api/documents/{doc_id}/op",
        json={"op": "add_primitive", "args": {"shape": "cylinder", "operation": "union"}},
        headers=alice,
    )
    assert op.status_code == 200, op.text
    assert op.json()["ok"]


def test_legacy_unowned_document_is_shared():
    # Documents that predate multi-tenancy carry no owner and stay accessible so
    # they are not stranded by the migration.
    legacy = document_service.create_document(name="Legacy Part", owner_id=None)
    assert legacy.owner_id is None
    carol = _user("carol-legacy@example.com")
    assert client.get(f"/api/documents/{legacy.id}", headers=carol).status_code == 200

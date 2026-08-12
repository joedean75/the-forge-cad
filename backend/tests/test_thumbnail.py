"""The library card: a picture of the body, and enough facts to tell parts apart.

Two halves of the same complaint. The library showed an icon and a name, so two
documents were indistinguishable until one of them was opened — and the listing
already had the answers in the row it was reading.
"""

import numpy as np
from fastapi.testclient import TestClient

from conftest import http_part, plate_tree, whole_tree_document
from app.cad_kernel.core.feature_tree import walk_nodes
from main import app
from app.services import document_service as docs
from app.services import persistence, thumbnail

client = TestClient(app)


def _create(size=(0.1, 0.06, 0.02)) -> dict:
    doc_id = http_part(client, size)
    return client.get(f"/api/documents/{doc_id}").json()


def _summary(doc_id: str) -> dict:
    rows = client.get("/api/documents").json()
    matches = [r for r in rows if r["id"] == doc_id]
    assert matches, f"{doc_id} not in the listing"
    return matches[0]


# --- the picture -----------------------------------------------------------

def test_a_mesh_renders_to_a_png():
    # A unit tetrahedron is enough: the renderer's contract is "triangles in,
    # PNG out", and every shape below it is the same code path.
    vertices = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]], dtype=float)
    faces = np.array([[0, 1, 2], [0, 1, 3], [0, 2, 3], [1, 2, 3]], dtype=np.int64)
    payload = thumbnail.render(vertices, faces, size=64)
    assert payload is not None
    assert payload[:8] == b"\x89PNG\r\n\x1a\n"


def test_an_empty_mesh_draws_nothing_rather_than_a_blank_png():
    assert thumbnail.render(np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64)) is None


def test_two_different_parts_do_not_render_the_same_picture():
    """The whole point: a thumbnail nobody can tell apart is an icon with a cost."""
    plate = _create()
    rod = _create((0.04, 0.04, 0.12))
    a = thumbnail.for_document(plate["id"], plate["derived"]["mesh_hash"])
    b = thumbnail.for_document(rod["id"], rod["derived"]["mesh_hash"])
    assert a and b and a != b


def test_the_thumbnail_endpoint_is_addressed_by_the_geometry_hash():
    doc = _create()
    mesh_hash = doc["derived"]["mesh_hash"]
    resp = client.get(f"/api/documents/{doc['id']}/thumbnail/{mesh_hash}")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == thumbnail.MIME
    # Immutable, and *private*: the endpoint is access-checked, so a shared proxy
    # caching it publicly would hand one tenant's geometry to another.
    assert "immutable" in resp.headers["cache-control"]
    assert "private" in resp.headers["cache-control"]
    assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_a_hash_the_document_does_not_have_is_a_404_not_the_current_picture():
    """Answering with a different picture under a hash cached forever poisons
    every cache that sees it — the same rule the mesh endpoint follows."""
    doc = _create()
    resp = client.get(f"/api/documents/{doc['id']}/thumbnail/deadbeefdeadbeef")
    assert resp.status_code == 404


def test_the_picture_follows_an_edit_rather_than_going_stale():
    doc = _create()
    first = doc["derived"]["mesh_hash"]
    tree = doc["feature_tree"]
    box = next(n for n in walk_nodes(tree) if n.get("type") == "BoxFeature")
    box["params"]["size"]["x"] = 0.25
    edited = client.put(
        f"/api/documents/{doc['id']}",
        json={"feature_tree": tree, "resolution": 64},
    ).json()
    second = edited["derived"]["mesh_hash"]
    assert second and second != first
    # The old hash is not this document's geometry any more, so the new picture
    # arrives under a new URL rather than replacing the contents of the old one.
    assert client.get(f"/api/documents/{doc['id']}/thumbnail/{second}").status_code == 200
    assert thumbnail.for_document(doc["id"], first) is None


def test_a_thumbnail_is_derived_and_stays_out_of_the_document():
    doc = _create()
    client.get(f"/api/documents/{doc['id']}/thumbnail/{doc['derived']['mesh_hash']}")
    stored = persistence.load_document(doc["id"])
    assert "thumbnail" not in repr(stored)


# --- the facts on the card -------------------------------------------------

def test_the_listing_says_what_the_part_is():
    doc = _create()
    client.post(f"/api/documents/{doc['id']}/material", json={"material_id": "steel-1018"})
    row = _summary(doc["id"])
    assert row["lifecycle_state"] == "draft"
    assert row["material_id"] == "steel-1018"
    assert row["mass_kg"] and row["mass_kg"] > 0
    assert row["mesh_hash"]
    assert row["created_at"]


def test_the_listing_carries_the_released_revision_letter():
    doc = _create()
    resp = client.post(f"/api/documents/{doc['id']}/revisions", json={"note": "first cut"})
    assert resp.status_code == 200, resp.text
    letter = resp.json()["letter"]
    assert _summary(doc["id"])["revision"] == letter


def test_the_listing_carries_the_prompt_the_part_was_asked_for():
    """Often the only thing anyone remembers about a generated part, which is
    why the library searches it alongside the name."""
    # A prompt only reaches a document through text-to-CAD, which needs a model.
    # The listing feature under test is independent of how it got there, so the
    # tree is installed directly — see `conftest.whole_tree_document`.
    doc = whole_tree_document(plate_tree(), prompt="a 60x30x5 mm plate")
    assert _summary(doc.id)["source_prompt"] == "a 60x30x5 mm plate"


def test_the_sql_and_in_memory_listing_paths_agree():
    """`list_documents` overlays live documents on the SQL rows. Two paths
    producing different cards is how a field silently only works after a
    restart."""
    doc = _create((0.08, 0.08, 0.02))
    client.post(f"/api/documents/{doc['id']}/material", json={"material_id": "al-6061"})
    fields = ("lifecycle_state", "material_id", "mass_kg", "revision",
              "mesh_hash", "created_at", "source_prompt")

    live = {f: getattr(_one(docs.list_documents(), doc["id"]), f) for f in fields}
    docs.DOCUMENTS.pop(doc["id"], None)          # force the SQL path
    stored = {f: getattr(_one(docs.list_documents(), doc["id"]), f) for f in fields}

    assert live == stored


def _one(summaries, doc_id):
    for s in summaries:
        if s.id == doc_id:
            return s
    raise AssertionError(f"{doc_id} missing from the listing")

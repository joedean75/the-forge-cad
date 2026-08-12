"""Tests for the DesignDocument rearchitecture endpoints."""

from fastapi.testclient import TestClient

from conftest import http_part
from app.cad_kernel.core.feature_tree import walk_nodes
from main import app


client = TestClient(app)


def _create_doc(size=(0.1, 0.06, 0.02)) -> str:
    return http_part(client, size)


def test_create_document_from_prompt():
    doc_id = _create_doc((0.1, 0.05, 0.03))
    resp = client.get(f"/api/documents/{doc_id}")
    assert resp.status_code == 200
    doc = resp.json()
    assert doc["id"] == doc_id
    assert "/mesh/" in doc["derived"]["mesh_url"]
    assert doc["derived"]["volume"] > 0


def test_list_documents():
    doc_id = _create_doc((0.1, 0.1, 0.1))
    resp = client.get("/api/documents")
    assert resp.status_code == 200
    docs = resp.json()
    assert any(d["id"] == doc_id for d in docs)


def test_update_document_feature_tree():
    doc_id = _create_doc((0.1, 0.1, 0.1))
    get_resp = client.get(f"/api/documents/{doc_id}")
    tree = get_resp.json()["feature_tree"]
    # The root is the union the datum planes sit in, not the box — see
    # `conftest.body_id`. It used to be the box only because a prompt produced
    # a single-feature tree.
    box = next(n for n in walk_nodes(tree) if n.get("type") == "BoxFeature")
    box["params"]["size"]["x"] = 0.2

    resp = client.put(
        f"/api/documents/{doc_id}",
        json={"feature_tree": tree, "resolution": 64},
    )
    assert resp.status_code == 200, resp.text
    # `mixed` means "generated, then edited by hand". This document was built
    # by hand, so editing it leaves it `manual` — it used to read `mixed` only
    # because a prompt made it `text_to_cad` first.
    assert resp.json()["provenance"] == "manual"
    updated = next(n for n in walk_nodes(resp.json()["feature_tree"])
                   if n.get("type") == "BoxFeature")
    assert updated["params"]["size"]["x"] == 0.2


def test_editing_a_generated_document_marks_it_mixed():
    """The transition the test above used to cover incidentally."""
    from conftest import plate_tree, whole_tree_document

    doc = whole_tree_document(plate_tree(), prompt="a 60x30x5 mm plate")
    assert doc.provenance == "text_to_cad"

    tree = client.get(f"/api/documents/{doc.id}").json()["feature_tree"]
    box = next(n for n in walk_nodes(tree) if n.get("type") == "BoxFeature")
    box["params"]["size"]["x"] = 0.09
    resp = client.put(f"/api/documents/{doc.id}",
                      json={"feature_tree": tree, "resolution": 48})
    assert resp.status_code == 200, resp.text
    assert resp.json()["provenance"] == "mixed"


def test_document_simulate_endpoint():
    doc_id = _create_doc((0.1, 0.05, 0.03))
    resp = client.post(f"/api/documents/{doc_id}/simulate?resolution=64")
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert "max_stress" in result
    assert "safety_factor" in result


def test_document_dfm_endpoint():
    doc_id = _create_doc((0.1, 0.05, 0.03))
    resp = client.post(f"/api/documents/{doc_id}/dfm?build_axis=z&resolution=64")
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["process"] == "LPBF"
    assert 0 <= result["printability_score"] <= 100
    assert len(result["checks"]) > 0
    assert result["quote"]["total"] >= 0


def test_document_step_export():
    doc_id = _create_doc((0.1, 0.05, 0.03))
    resp = client.get(f"/api/documents/{doc_id}/step?resolution=64")
    assert resp.status_code == 200, resp.text
    assert "ISO-10303-21" in resp.text
    assert "MANIFOLD_SOLID_BREP" in resp.text


def test_delete_document():
    doc_id = _create_doc((0.05, 0.05, 0.05))
    resp = client.delete(f"/api/documents/{doc_id}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "deleted"
    assert client.get(f"/api/documents/{doc_id}").status_code == 404


def test_null_feature_ids_are_healed_so_edits_resolve():
    """LLM-generated trees arrive with id: null on every node; the server must
    assign ids (create + load), or every id-addressed edit 404s — regression
    test for 'feature not found' on editing an LLM-created part."""
    from app.services import cad_operations
    from app.services import document_service as ds

    doc = ds.create_document(name="null-ids", resolution=48)
    doc.feature_tree = {"version": "brep-v1", "root": {
        "type": "BooleanFeature", "id": None, "name": "S",
        "params": {"operation": "subtract", "children": [
            {"type": "BoxFeature", "id": None, "name": "F1",
             "params": {"size": {"x": 0.075, "y": 0.05, "z": 0.005}, "center": {"x": 0, "y": 0, "z": 0}}},
            {"type": "CylinderFeature", "id": None, "name": "H",
             "params": {"radius": 0.005, "height": 0.02, "center": {"x": 0, "y": 0, "z": 0},
                        "axis": {"x": 0, "y": 0, "z": 1}}}]}}}
    ds.evaluate_document(doc, resolution=48)

    root = doc.feature_tree["root"]
    box = root["params"]["children"][0]
    assert root["id"] and box["id"], "every node must carry a server-assigned id"

    # The id the server handed out must be directly editable.
    cad_operations.dispatch(
        "set_feature_params", doc.id,
        {"feature_id": box["id"], "params": {"size": {"x": 0.08, "y": 0.05, "z": 0.005}}},
    )
    edited = next(c for c in ds.get_document(doc.id).feature_tree["root"]["params"]["children"]
                  if c["id"] == box["id"])
    assert abs(edited["params"]["size"]["x"] - 0.08) < 1e-9

    # Reload-from-disk path heals too (docs persisted before this fix).
    ds.DOCUMENTS.pop(doc.id, None)
    reloaded = ds.get_document(doc.id)
    assert reloaded is not None
    assert reloaded.feature_tree["root"]["id"]

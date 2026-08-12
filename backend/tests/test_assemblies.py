import re
"""Tests for the assembly-document endpoints (compose parts into a whole)."""

from fastapi.testclient import TestClient

from conftest import http_part
from main import app


client = TestClient(app)


def _create_part(size=(0.1, 0.05, 0.03)) -> str:
    return http_part(client, size)


def _create_assembly(name: str = "Test asm") -> dict:
    resp = client.post("/api/assemblies", json={"name": name})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_create_and_list_assembly():
    asm = _create_assembly("Widget")
    assert asm["name"] == "Widget"
    assert asm["components"] == []
    resp = client.get("/api/assemblies")
    assert resp.status_code == 200
    assert any(a["id"] == asm["id"] and a["component_count"] == 0 for a in resp.json())


def test_insert_part_resolves_mesh_and_bbox():
    part = _create_part()
    asm = _create_assembly()
    resp = client.post(f"/api/assemblies/{asm['id']}/components", json={"part_id": part})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["components"]) == 1
    comp = body["components"][0]
    assert comp["part_id"] == part
    assert re.fullmatch(r"/api/documents/[^/]+/mesh/[0-9a-f]+", comp["mesh_url"])
    assert comp["bbox"] is not None
    assert comp["missing"] is False
    assert body["bbox"] is not None  # assembly envelope resolved


def test_place_and_remove_component():
    part = _create_part()
    asm = _create_assembly()
    asm = client.post(f"/api/assemblies/{asm['id']}/components", json={"part_id": part}).json()
    cid = asm["components"][0]["id"]

    # move the component
    transform = {
        "translate": {"x": 0.05, "y": 0.0, "z": 0.0},
        "rotate_deg": {"x": 0.0, "y": 0.0, "z": 90.0},
        "scale": 1.0,
    }
    resp = client.put(f"/api/assemblies/{asm['id']}/components/{cid}", json={"transform": transform})
    assert resp.status_code == 200, resp.text
    moved = resp.json()["components"][0]
    assert moved["transform"]["translate"]["x"] == 0.05
    assert moved["transform"]["rotate_deg"]["z"] == 90.0

    # remove it
    resp = client.delete(f"/api/assemblies/{asm['id']}/components/{cid}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["components"] == []


def test_deleted_part_marks_component_missing():
    part = _create_part()
    asm = _create_assembly()
    asm = client.post(f"/api/assemblies/{asm['id']}/components", json={"part_id": part}).json()
    client.delete(f"/api/documents/{part}")
    resp = client.get(f"/api/assemblies/{asm['id']}")
    assert resp.status_code == 200
    assert resp.json()["components"][0]["missing"] is True


def test_insert_missing_part_is_404():
    asm = _create_assembly()
    resp = client.post(f"/api/assemblies/{asm['id']}/components", json={"part_id": "does-not-exist"})
    assert resp.status_code == 404


def test_delete_assembly():
    asm = _create_assembly()
    resp = client.delete(f"/api/assemblies/{asm['id']}")
    assert resp.status_code == 200
    assert client.get(f"/api/assemblies/{asm['id']}").status_code == 404

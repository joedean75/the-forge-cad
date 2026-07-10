"""Tests for the unified design-generation endpoint."""

from fastapi.testclient import TestClient

from main import app

client = TestClient(app)


def test_generate_design_part():
    resp = client.post(
        "/api/design/generate",
        json={"prompt": "a rally style rc chassis on a 185x185x185 3d printer build plate", "resolution": 64},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["kind"] == "part"
    assert data["part"] is not None
    assert "chassis" in data["part"]["name"].lower()
    assert data["part"]["mesh_url"].startswith("data:")


def test_generate_design_assembly():
    resp = client.post(
        "/api/design/generate",
        json={"prompt": "a truss frame spanning 2 meters", "resolution": 64},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["kind"] == "assembly"
    assert data["assembly"] is not None
    assert len(data["assembly"]["nodes"]) > 0
    assert len(data["assembly"]["struts"]) > 0


def test_generate_design_requires_prompt():
    resp = client.post("/api/design/generate", json={"prompt": "   "})
    assert resp.status_code == 400

"""Tests for the agent platform surface (roadmap feature 04)."""

from fastapi.testclient import TestClient

from main import app

client = TestClient(app)


def test_platform_manifest_is_public_and_complete():
    r = client.get("/api/platform")
    assert r.status_code == 200
    m = r.json()
    assert "X-Api-Key" in m["auth"]["api_key"]
    for key in ("operation_catalog", "run_operation", "copilot", "import", "quotes", "openapi"):
        assert key in m["endpoints"]
    assert "copilot" in m["mcp"]["tools"]


def test_operation_catalog_lists_every_registered_op():
    from app.services import cad_operations

    r = client.get("/api/operations")
    assert r.status_code == 200
    names = {o["name"] for o in r.json()["operations"]}
    assert names == set(cad_operations.OPERATIONS.keys())
    for o in r.json()["operations"]:
        assert o["kind"] in ("query", "mutate") and o["description"]


def test_api_key_drives_the_operation_api_end_to_end():
    """The platform promise: an agent with only an API key can do real CAD."""
    # Sign up (Pro plan is needed for API keys — self-serve in local mode).
    s = client.post("/api/auth/signup", json={"email": "agent@forge.local", "password": "password123"})
    assert s.status_code == 200, s.text
    jwt = {"Authorization": f"Bearer {s.json()['token']}"}
    up = client.post("/api/auth/subscribe", json={"plan_id": "pro"}, headers=jwt)
    assert up.status_code == 200, up.text
    k = client.post("/api/auth/api-keys", json={"name": "agent"}, headers=jwt)
    assert k.status_code == 200, k.text
    key = {"X-Api-Key": k.json()["api_key"]}

    # Create a part and mutate it using ONLY the API key.
    d = client.post("/api/documents", json={"prompt": "box 0.1x0.06x0.02 m", "resolution": 64}, headers=key)
    assert d.status_code == 200, d.text
    doc_id = d.json()["id"]
    op = client.post(
        f"/api/documents/{doc_id}/op",
        json={"op": "add_primitive",
              "args": {"shape": "cylinder", "operation": "subtract",
                       "params": {"radius": 0.01, "height": 0.05, "center": {"x": 0, "y": 0, "z": 0}}}},
        headers=key,
    )
    assert op.status_code == 200, op.text
    assert op.json()["ok"] is True

    # And quote it.
    q = client.post(f"/api/documents/{doc_id}/quotes", json={"quantity": 1}, headers=key)
    assert q.status_code == 200 and q.json()["quotes"]


def test_bogus_api_key_is_rejected():
    r = client.post("/api/documents", json={"resolution": 64}, headers={"X-Api-Key": "fk_not_real"})
    assert r.status_code in (401, 403)

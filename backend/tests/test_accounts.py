"""Subscription foundation: accounts, auth, entitlements, quota metering."""

import importlib

import pytest
from fastapi.testclient import TestClient

from conftest import http_part
from main import app
from app.services import accounts, plans

client = TestClient(app)


def _signup(email, password="supersecret1"):
    return client.post("/api/auth/signup", json={"email": email, "password": password})


# -- account service ---------------------------------------------------------

def test_password_is_hashed_not_stored_plaintext():
    acc = accounts.create_account("hash-check@example.com", "supersecret1")
    assert acc.password_hash != "supersecret1"
    assert len(acc.password_hash) == 64  # sha256 hex
    assert accounts.authenticate_password("hash-check@example.com", "supersecret1").id == acc.id
    with pytest.raises(accounts.AuthError):
        accounts.authenticate_password("hash-check@example.com", "wrong")


def test_quota_metering_blocks_when_exhausted():
    acc = accounts.create_account("quota@example.com", "supersecret1")  # free plan
    quota = plans.get_plan("free").monthly_op_quota
    for _ in range(quota):
        acc.record_op()
    with pytest.raises(accounts.AuthError) as ei:
        acc.record_op()
    assert ei.value.status == 402


def test_api_keys_require_pro_and_authenticate():
    acc = accounts.create_account("keys@example.com", "supersecret1")
    with pytest.raises(accounts.AuthError):
        accounts.issue_api_key(acc)  # free plan → not entitled
    accounts.set_plan(acc, "pro")
    raw = accounts.issue_api_key(acc, "ci")
    assert raw.startswith("dps_live_")
    assert accounts.authenticate_api_key(raw).id == acc.id
    with pytest.raises(accounts.AuthError):
        accounts.authenticate_api_key("dps_live_bogus")


# -- HTTP surface ------------------------------------------------------------

def test_signup_login_and_me():
    r = _signup("flow@example.com")
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    assert r.json()["account"]["plan"]["id"] == "free"

    r2 = client.post("/api/auth/login", json={"email": "flow@example.com", "password": "supersecret1"})
    assert r2.status_code == 200
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email"] == "flow@example.com"


def test_duplicate_signup_rejected():
    _signup("dup@example.com")
    assert _signup("dup@example.com").status_code == 409


def test_short_password_rejected():
    r = client.post("/api/auth/signup", json={"email": "weak@example.com", "password": "short"})
    assert r.status_code == 400


def test_plans_catalog_public():
    r = client.get("/api/auth/plans")
    assert r.status_code == 200
    ids = {p["id"] for p in r.json()["plans"]}
    assert {"free", "pro", "enterprise"} <= ids
    assert "billing" in r.json()


def test_step_export_gated_by_plan():
    # A free-plan user is denied STEP export; upgrading to pro grants it.
    r = _signup("stepgate@example.com")
    token = r.json()["token"]
    hdr = {"Authorization": f"Bearer {token}"}
    doc_id = http_part(client, (0.05, 0.05, 0.05), resolution=48, headers=hdr)

    denied = client.get(f"/api/documents/{doc_id}/step?resolution=48", headers=hdr)
    assert denied.status_code == 402, denied.text

    client.post("/api/auth/subscribe", json={"plan_id": "pro"}, headers=hdr)
    allowed = client.get(f"/api/documents/{doc_id}/step?resolution=48", headers=hdr)
    assert allowed.status_code == 200
    assert "ISO-10303" in allowed.text or "STEP" in allowed.text.upper()


def test_api_key_authenticates_operation_endpoint():
    r = _signup("agent@example.com")
    token = r.json()["token"]
    hdr = {"Authorization": f"Bearer {token}"}
    client.post("/api/auth/subscribe", json={"plan_id": "pro"}, headers=hdr)
    key = client.post("/api/auth/api-keys", json={"name": "mcp"}, headers=hdr).json()["api_key"]

    doc_id = http_part(client, (0.05, 0.05, 0.05), resolution=48,
                       headers={"X-API-Key": key})
    # Drive a mutating op purely with the API key (the agent path).
    op = client.post(
        f"/api/documents/{doc_id}/op",
        json={"op": "add_primitive", "args": {"shape": "cylinder", "operation": "union"}},
        headers={"X-API-Key": key},
    )
    assert op.status_code == 200, op.text
    assert op.json()["ok"]

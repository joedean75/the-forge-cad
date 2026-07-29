"""OAuth sign-in: opt-in configuration, state/PKCE handling, handoff redemption,
and account linking rules."""

from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from main import app
from app.services import accounts, oauth

client = TestClient(app)


def _configure_github(monkeypatch):
    monkeypatch.setenv("OAUTH_GITHUB_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("OAUTH_GITHUB_CLIENT_SECRET", "test-client-secret")


def _start_login(follow=False):
    return client.get("/api/auth/oauth/github/start", follow_redirects=follow)


def _state_from(location: str) -> str:
    return parse_qs(urlparse(location).query)["state"][0]


# -- opt-in configuration ----------------------------------------------------

def test_no_providers_when_unconfigured(monkeypatch):
    monkeypatch.delenv("OAUTH_GITHUB_CLIENT_ID", raising=False)
    monkeypatch.delenv("OAUTH_GOOGLE_CLIENT_ID", raising=False)
    assert client.get("/api/auth/oauth/providers").json() == {"providers": []}
    assert _start_login().status_code == 503  # configured check


def test_unknown_provider_is_404():
    assert client.get("/api/auth/oauth/facebook/start", follow_redirects=False).status_code == 404


# -- authorization redirect --------------------------------------------------

def test_start_redirects_with_state_and_pkce(monkeypatch):
    _configure_github(monkeypatch)
    resp = _start_login()
    assert resp.status_code == 302
    url = urlparse(resp.headers["location"])
    assert url.hostname == "github.com"
    q = parse_qs(url.query)
    assert q["client_id"] == ["test-client-id"]
    assert q["response_type"] == ["code"]
    assert q["code_challenge_method"] == ["S256"]
    assert len(q["state"][0]) > 30 and len(q["code_challenge"][0]) > 30
    # No secrets in the browser-visible URL.
    assert "test-client-secret" not in resp.headers["location"]


# -- callback ----------------------------------------------------------------

def test_callback_rejects_unknown_state(monkeypatch):
    _configure_github(monkeypatch)
    resp = client.get(
        "/api/auth/oauth/github/callback?code=x&state=forged-by-attacker",
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "oauth_error=" in resp.headers["location"]
    assert "oauth_code=" not in resp.headers["location"]


def test_callback_provider_error_redirects_with_message(monkeypatch):
    _configure_github(monkeypatch)
    resp = client.get(
        "/api/auth/oauth/github/callback?error=access_denied", follow_redirects=False
    )
    assert resp.status_code == 302
    assert "oauth_error=" in resp.headers["location"]


def test_full_flow_creates_account_and_redeems_once(monkeypatch):
    _configure_github(monkeypatch)
    monkeypatch.setattr(
        oauth, "_fetch_identity", lambda p, code, verifier: ("gh-9001", "oauth-new@example.com")
    )

    state = _state_from(_start_login().headers["location"])
    cb = client.get(
        f"/api/auth/oauth/github/callback?code=provider-code&state={state}",
        follow_redirects=False,
    )
    assert cb.status_code == 302
    q = parse_qs(urlparse(cb.headers["location"]).query)
    handoff = q["oauth_code"][0]

    # The session JWT is never in the redirect — only the one-time code.
    assert "token" not in q and len(handoff) > 30

    resp = client.post("/api/auth/oauth/exchange", json={"code": handoff})
    assert resp.status_code == 200
    body = resp.json()
    assert body["account"]["email"] == "oauth-new@example.com"
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {body['token']}"})
    assert me.status_code == 200 and me.json()["email"] == "oauth-new@example.com"

    # Handoff codes are single-use.
    assert client.post("/api/auth/oauth/exchange", json={"code": handoff}).status_code == 401

    # State is single-use too: replaying the callback fails.
    replay = client.get(
        f"/api/auth/oauth/github/callback?code=provider-code&state={state}",
        follow_redirects=False,
    )
    assert "oauth_error=" in replay.headers["location"]


def test_oauth_links_existing_password_account(monkeypatch):
    _configure_github(monkeypatch)
    existing = accounts.create_account("linked@example.com", "supersecret1")
    monkeypatch.setattr(
        oauth, "_fetch_identity", lambda p, code, verifier: ("gh-linked-1", "linked@example.com")
    )

    state = _state_from(_start_login().headers["location"])
    cb = client.get(
        f"/api/auth/oauth/github/callback?code=c&state={state}", follow_redirects=False
    )
    handoff = parse_qs(urlparse(cb.headers["location"]).query)["oauth_code"][0]
    body = client.post("/api/auth/oauth/exchange", json={"code": handoff}).json()

    assert body["account"]["id"] == existing.id  # linked, not duplicated
    assert existing.oauth_ids["github"] == "gh-linked-1"
    # Password login still works after linking.
    assert accounts.authenticate_password("linked@example.com", "supersecret1").id == existing.id


# -- linking safety rules ----------------------------------------------------

def test_subject_cannot_be_rebound_to_another_account():
    a = accounts.create_account("bind-a@example.com", "supersecret1")
    b = accounts.create_account("bind-b@example.com", "supersecret1")
    accounts.link_oauth(a, "github", "gh-shared-sub")
    with pytest.raises(accounts.AuthError) as ei:
        accounts.link_oauth(b, "github", "gh-shared-sub")
    assert ei.value.status == 409


def test_account_keeps_first_provider_identity():
    acc = accounts.create_account("stable@example.com", "supersecret1")
    accounts.link_oauth(acc, "github", "gh-first")
    accounts.link_oauth(acc, "github", "gh-first")  # idempotent re-link is fine
    with pytest.raises(accounts.AuthError):
        accounts.link_oauth(acc, "github", "gh-second")


def test_oauth_account_has_no_usable_password():
    acc = accounts.create_oauth_account("no-password@example.com")
    with pytest.raises(accounts.AuthError):
        accounts.authenticate_password("no-password@example.com", "")
    with pytest.raises(accounts.AuthError):
        accounts.authenticate_password("no-password@example.com", "supersecret1")
    assert acc.password_hash  # still a real hash, not a sentinel


# -- verified-email enforcement ----------------------------------------------

def test_google_identity_requires_verified_email():
    with pytest.raises(accounts.AuthError) as ei:
        oauth._google_identity({"sub": "g-1", "email": "x@example.com", "email_verified": False})
    assert ei.value.status == 403
    assert oauth._google_identity(
        {"sub": "g-1", "email": "X@Example.com", "email_verified": True}
    ) == ("g-1", "x@example.com")


def test_github_identity_requires_verified_email():
    user = {"id": 42}
    with pytest.raises(accounts.AuthError) as ei:
        oauth._github_identity(user, [{"email": "x@example.com", "verified": False, "primary": True}])
    assert ei.value.status == 403
    # Primary verified email wins over other verified emails.
    subject, email = oauth._github_identity(
        user,
        [
            {"email": "alt@example.com", "verified": True, "primary": False},
            {"email": "Main@Example.com", "verified": True, "primary": True},
        ],
    )
    assert (subject, email) == ("42", "main@example.com")


# -- handoff hygiene ---------------------------------------------------------

def test_expired_state_is_rejected(monkeypatch):
    _configure_github(monkeypatch)
    state = _state_from(_start_login().headers["location"])
    # Age the pending login past its TTL.
    oauth._PENDING[state].expires_at = 0
    oauth._purge_expired()
    resp = client.get(
        f"/api/auth/oauth/github/callback?code=c&state={state}", follow_redirects=False
    )
    assert "oauth_error=" in resp.headers["location"]


def test_exchange_rejects_garbage_codes():
    assert client.post("/api/auth/oauth/exchange", json={"code": ""}).status_code == 401
    assert client.post("/api/auth/oauth/exchange", json={"code": "nope"}).status_code == 401

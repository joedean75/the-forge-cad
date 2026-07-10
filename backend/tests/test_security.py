"""Security controls: SSRF guard, auth rate limiting, headers, enumeration."""

import pytest
from fastapi.testclient import TestClient

from main import app
from app import rate_limit
from app.services.machine import url_guard
from app.services.machine.url_guard import UrlNotAllowed, validate_url

client = TestClient(app)


# -- SSRF guard --------------------------------------------------------------

def test_guard_blocks_non_http_scheme(monkeypatch):
    monkeypatch.delenv("MOONRAKER_URLS", raising=False)
    monkeypatch.delenv("DAPS_MACHINE_URL_ALLOWLIST", raising=False)
    with pytest.raises(UrlNotAllowed):
        validate_url("file:///etc/passwd")
    with pytest.raises(UrlNotAllowed):
        validate_url("gopher://internal/")


def test_guard_blocks_loopback_and_metadata(monkeypatch):
    monkeypatch.delenv("MOONRAKER_URLS", raising=False)
    monkeypatch.delenv("DAPS_MACHINE_URL_ALLOWLIST", raising=False)
    monkeypatch.delenv("DAPS_ALLOW_PRIVATE_MACHINE_URLS", raising=False)
    for bad in ("http://127.0.0.1:7125", "http://169.254.169.254/latest/meta-data/", "http://[::1]:80"):
        with pytest.raises(UrlNotAllowed):
            validate_url(bad)


def test_guard_private_requires_optin(monkeypatch):
    monkeypatch.delenv("MOONRAKER_URLS", raising=False)
    monkeypatch.delenv("DAPS_MACHINE_URL_ALLOWLIST", raising=False)
    monkeypatch.delenv("DAPS_ALLOW_PRIVATE_MACHINE_URLS", raising=False)
    with pytest.raises(UrlNotAllowed):
        validate_url("http://192.168.1.50:7125")
    monkeypatch.setenv("DAPS_ALLOW_PRIVATE_MACHINE_URLS", "1")
    assert validate_url("http://192.168.1.50:7125").startswith("http://192.168")


def test_guard_allowlist_is_strict(monkeypatch):
    monkeypatch.setenv("MOONRAKER_URLS", "Shop|http://printer.local:7125")
    # Allowlisted host passes without DNS/private checks.
    assert validate_url("http://printer.local:7125/foo") == "http://printer.local:7125/foo"
    # Anything else is rejected, even a public host.
    with pytest.raises(UrlNotAllowed):
        validate_url("http://example.com/")


def test_machine_endpoint_rejects_ssrf(monkeypatch):
    # Reaches the guard through the HTTP surface (dev account passes auth).
    monkeypatch.delenv("MOONRAKER_URLS", raising=False)
    monkeypatch.delenv("DAPS_ALLOW_PRIVATE_MACHINE_URLS", raising=False)
    r = client.post("/api/machine/status", json={"url": "http://169.254.169.254/"})
    assert r.status_code == 400


# -- Auth rate limiting ------------------------------------------------------

def test_login_is_rate_limited(monkeypatch):
    monkeypatch.setattr(rate_limit, "_ENABLED", True)
    rate_limit.reset()
    saw_429 = False
    for _ in range(15):
        r = client.post("/api/auth/login", json={"email": "rl@example.com", "password": "whatever8"})
        if r.status_code == 429:
            saw_429 = True
            assert r.headers.get("Retry-After")
            break
    rate_limit.reset()
    assert saw_429


# -- Security headers --------------------------------------------------------

def test_security_headers_present():
    r = client.get("/api/health")
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.headers.get("X-Frame-Options") == "DENY"
    assert "content-security-policy" in {k.lower() for k in r.headers.keys()}


# -- Account enumeration -----------------------------------------------------

def test_login_does_not_reveal_whether_email_exists():
    client.post("/api/auth/signup", json={"email": "enum@example.com", "password": "supersecret1"})
    unknown = client.post("/api/auth/login", json={"email": "nobody-here@example.com", "password": "supersecret1"})
    wrong = client.post("/api/auth/login", json={"email": "enum@example.com", "password": "wrongpassword"})
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["detail"] == wrong.json()["detail"]

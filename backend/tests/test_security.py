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
    monkeypatch.delenv("FORGE_MACHINE_URL_ALLOWLIST", raising=False)
    with pytest.raises(UrlNotAllowed):
        validate_url("file:///etc/passwd")
    with pytest.raises(UrlNotAllowed):
        validate_url("gopher://internal/")


def test_guard_blocks_loopback_and_metadata(monkeypatch):
    monkeypatch.delenv("MOONRAKER_URLS", raising=False)
    monkeypatch.delenv("FORGE_MACHINE_URL_ALLOWLIST", raising=False)
    monkeypatch.delenv("FORGE_ALLOW_PRIVATE_MACHINE_URLS", raising=False)
    for bad in ("http://127.0.0.1:7125", "http://169.254.169.254/latest/meta-data/", "http://[::1]:80"):
        with pytest.raises(UrlNotAllowed):
            validate_url(bad)


def test_guard_private_requires_optin(monkeypatch):
    monkeypatch.delenv("MOONRAKER_URLS", raising=False)
    monkeypatch.delenv("FORGE_MACHINE_URL_ALLOWLIST", raising=False)
    monkeypatch.delenv("FORGE_ALLOW_PRIVATE_MACHINE_URLS", raising=False)
    with pytest.raises(UrlNotAllowed):
        validate_url("http://192.168.1.50:7125")
    monkeypatch.setenv("FORGE_ALLOW_PRIVATE_MACHINE_URLS", "1")
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
    monkeypatch.delenv("FORGE_ALLOW_PRIVATE_MACHINE_URLS", raising=False)
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


def test_metadata_is_refused_even_with_the_self_hosted_optin(monkeypatch):
    """`FORGE_ALLOW_PRIVATE_MACHINE_URLS` now covers loopback as well as the LAN,
    because the shipped default printer is `http://localhost:7125` and a policy
    that could never allow it meant a 400 on every fresh install. Link-local must
    not come along for the ride: no deployment's printer is the cloud metadata
    service, so 169.254.169.254 stays shut whatever the flag says."""
    monkeypatch.delenv("MOONRAKER_URLS", raising=False)
    monkeypatch.delenv("FORGE_MACHINE_URL_ALLOWLIST", raising=False)
    monkeypatch.setenv("FORGE_ALLOW_PRIVATE_MACHINE_URLS", "1")

    # The self-hosted case now works…
    assert validate_url("http://127.0.0.1:7125").startswith("http://127.0.0.1")
    assert validate_url("http://192.168.1.50:7125").startswith("http://192.168")
    # …and the dangerous one does not.
    for never in ("http://169.254.169.254/latest/meta-data/",
                  "http://169.254.170.2/v2/credentials",
                  "http://224.0.0.1/", "http://0.0.0.0/"):
        with pytest.raises(UrlNotAllowed):
            validate_url(never)


def test_the_default_printer_is_reachable_on_a_self_hosted_box(monkeypatch):
    """The panel ships one printer out of the box; with the opt-in set it has to
    actually resolve, or the default is decoration."""
    from app.routers.machine import _configured_printers

    monkeypatch.delenv("MOONRAKER_URLS", raising=False)
    monkeypatch.setenv("FORGE_ALLOW_PRIVATE_MACHINE_URLS", "1")
    for printer in _configured_printers():
        assert validate_url(printer["url"]) == printer["url"]


def test_a_refusal_says_how_to_allow_it(monkeypatch):
    """The loopback branch used to say only 'disallowed (internal) address',
    which tells an operator nothing about the flag that fixes it."""
    monkeypatch.delenv("MOONRAKER_URLS", raising=False)
    monkeypatch.delenv("FORGE_MACHINE_URL_ALLOWLIST", raising=False)
    monkeypatch.delenv("FORGE_ALLOW_PRIVATE_MACHINE_URLS", raising=False)
    with pytest.raises(UrlNotAllowed, match="FORGE_ALLOW_PRIVATE_MACHINE_URLS"):
        validate_url("http://localhost:7125")


def test_the_csp_hashes_the_spa_inline_script():
    """`index.html` carries one inline script — the `window.onerror` handler that
    paints a readable failure into `#root` when the bundle itself throws. Under
    a bare `script-src 'self'` the browser refuses it, so the only safety net for
    a white screen was itself dead, and only in production: the Vite dev server
    sends no CSP, so dev can never see it.

    The hash is computed from the served file rather than pinned, so editing the
    handler cannot silently re-break it — that is what this asserts.
    """
    import base64
    import hashlib
    import re

    import main

    main._csp.cache_clear()
    csp = main._csp()
    assert "script-src 'self'" in csp
    assert "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0], \
        "script-src must not fall back to unsafe-inline"

    page = main.dist_dir / "index.html"
    if not page.exists():
        pytest.skip("frontend not built")
    for script in re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
                             page.read_text(), re.S):
        digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
        assert f"'sha256-{digest}'" in csp, "an inline script is not hashed into the CSP"


def test_the_csp_no_longer_allows_data_urls_to_be_fetched():
    """The mesh stopped being a `data:` STL URI when P1 made it a hash-keyed URL,
    and nothing in the client has fetched one since. `img-src` keeps `data:` and
    `blob:` — thumbnails are authed fetches turned into blob URLs."""
    import main

    main._csp.cache_clear()
    csp = main._csp()
    connect = csp.split("connect-src")[1].split(";")[0]
    assert "data:" not in connect
    assert "blob:" in csp.split("img-src")[1].split(";")[0]

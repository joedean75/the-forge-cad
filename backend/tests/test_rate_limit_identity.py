"""The rate limiter's client identity must not be attacker-controlled.

Regression cover for a bypass where ``X-Forwarded-For`` was trusted blindly:
rotating the header gave every request its own bucket, so the login limiter
never fired. See app/rate_limit.py.
"""

import time

import pytest
from fastapi import HTTPException

from app import rate_limit as R


class _Req:
    """Minimal stand-in for a Starlette Request."""

    def __init__(self, xff=None, peer="203.0.113.7", path="/api/auth/login"):
        self.headers = {"x-forwarded-for": xff} if xff else {}
        self.url = type("U", (), {"path": path})()
        self.client = type("C", (), {"host": peer})()


@pytest.fixture(autouse=True)
def _clean_limiter(monkeypatch):
    monkeypatch.setattr(R, "_ENABLED", True)
    R.reset()
    yield
    R.reset()


def test_spoofed_forwarded_for_cannot_mint_fresh_buckets(monkeypatch):
    monkeypatch.delenv("FORGE_TRUSTED_PROXY_HOPS", raising=False)
    dep = R.rate_limit(max_hits=5, window_seconds=60.0)

    allowed = 0
    blocked = 0
    for i in range(12):
        try:
            dep(_Req(xff=f"10.0.0.{i}"))
            allowed += 1
        except HTTPException as exc:
            assert exc.status_code == 429
            blocked += 1

    assert allowed == 5, "rotating X-Forwarded-For must not raise the effective limit"
    assert blocked == 7
    assert len(R._hits) == 1, "all spoofed requests belong to one real-peer bucket"


def test_forwarded_for_is_read_from_the_right_when_a_proxy_is_declared(monkeypatch):
    """With one trusted hop, the client is the last entry — not the forgeable first."""
    monkeypatch.setenv("FORGE_TRUSTED_PROXY_HOPS", "1")
    req = _Req(xff="1.1.1.1, 2.2.2.2, 9.9.9.9")
    assert R._client_ip(req) == "9.9.9.9"


def test_two_hops_reads_two_from_the_right(monkeypatch):
    monkeypatch.setenv("FORGE_TRUSTED_PROXY_HOPS", "2")
    assert R._client_ip(_Req(xff="1.1.1.1, 2.2.2.2, 9.9.9.9")) == "2.2.2.2"


def test_header_ignored_entirely_with_no_declared_proxy(monkeypatch):
    monkeypatch.delenv("FORGE_TRUSTED_PROXY_HOPS", raising=False)
    assert R._client_ip(_Req(xff="1.1.1.1", peer="198.51.100.4")) == "198.51.100.4"


def test_short_chain_falls_back_to_the_leftmost_entry(monkeypatch):
    """Fewer hops than advertised means a proxy was bypassed — don't index off the end."""
    monkeypatch.setenv("FORGE_TRUSTED_PROXY_HOPS", "3")
    assert R._client_ip(_Req(xff="1.1.1.1")) == "1.1.1.1"


def test_distinct_real_peers_still_get_distinct_budgets(monkeypatch):
    monkeypatch.delenv("FORGE_TRUSTED_PROXY_HOPS", raising=False)
    dep = R.rate_limit(max_hits=2, window_seconds=60.0)
    dep(_Req(peer="198.51.100.1"))
    dep(_Req(peer="198.51.100.1"))
    with pytest.raises(HTTPException):
        dep(_Req(peer="198.51.100.1"))
    dep(_Req(peer="198.51.100.2"))  # a genuinely different client is unaffected


def test_lapsed_buckets_are_swept_so_the_key_map_stays_bounded(monkeypatch):
    """One-shot callers must not leave a permanent key each.

    A bucket is only trimmed when that same client returns, so without a sweep
    every distinct client seen would live in the map forever — attacker-supplied
    keys included.
    """
    monkeypatch.delenv("FORGE_TRUSTED_PROXY_HOPS", raising=False)
    # Sweep on every call, and shrink the widest registered window — the sweep is
    # deliberately conservative across all limiters (auth registers 60s/300s at
    # import), so without this nothing would be reclaimable inside a test.
    monkeypatch.setattr(R, "_SWEEP_INTERVAL", 0.0)
    monkeypatch.setattr(R, "_max_window", 0.001)
    dep = R.rate_limit(max_hits=10, window_seconds=0.001)

    for i in range(50):
        dep(_Req(peer=f"198.51.100.{i}", path=f"/p{i}"))

    time.sleep(0.02)  # every window opened above has now lapsed
    monkeypatch.setattr(R, "_max_window", 0.001)
    dep(_Req(peer="198.51.100.99", path="/p99"))

    # 50 one-shot callers must not leave 50 permanent keys behind.
    assert len(R._hits) <= 2, f"lapsed buckets should be reclaimed, kept {len(R._hits)}"

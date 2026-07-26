"""Lightweight in-process rate limiting.

A sliding-window limiter keyed by client IP, used to blunt brute-force and
credential-stuffing against the auth endpoints. This is a first layer suitable
for a single instance; a horizontally-scaled deployment should front it with a
shared store (Redis) or an edge/WAF rate limit — the dependency shape here stays
the same, only the backing store changes.

Two properties this file has to get right, because both were once wrong:

1. **The client identity must not be attacker-controlled.** ``X-Forwarded-For``
   is appended to by each proxy, so the *left*-most entry is whatever the client
   sent. Trusting it lets anyone mint a fresh bucket per request
   (``-H "X-Forwarded-For: $RANDOM"``) and bypass the limit entirely. We only
   read the header when the deployment declares how many proxies sit in front
   (``FORGE_TRUSTED_PROXY_HOPS``), and then we count from the right.
2. **The bucket map must not grow without bound.** Keys are derived from client
   identity, so an unevicted map is a remote memory-exhaustion vector. Empty
   buckets are swept periodically.

.. warning::
   The ASGI server must not do its own forwarded-header rewriting, or
   ``request.client`` arrives already spoofed and nothing here can recover the
   real peer. uvicorn/gunicorn enable ``--proxy-headers`` by default; the
   production image passes ``--forwarded-allow-ips=`` to turn it off, and a dev
   server should be started with ``--no-proxy-headers``. Header parsing is owned
   here so there is exactly one place that decides what is trusted.
"""

from __future__ import annotations

import os
import time
from collections import defaultdict, deque
from typing import Deque, Dict

from fastapi import HTTPException, Request

# Disable in tests / when explicitly turned off.
_ENABLED = os.environ.get("FORGE_RATE_LIMIT", "1").lower() not in ("0", "false", "no")


def _trusted_hops() -> int:
    """Number of trusted reverse proxies in front of the app.

    0 (the default) means "no proxy" — the header is ignored entirely and the
    socket peer is authoritative. Behind one PaaS load balancer (Render, Fly,
    Heroku) set ``FORGE_TRUSTED_PROXY_HOPS=1``.
    """
    try:
        return max(0, int(os.environ.get("FORGE_TRUSTED_PROXY_HOPS", "0")))
    except ValueError:
        return 0


# Sweep empty buckets no more often than this (seconds); cheap amortized cleanup
# that keeps a long-lived process from accumulating one entry per client seen.
_SWEEP_INTERVAL = 60.0
_last_sweep = 0.0

# Largest window any registered limiter uses. A bucket whose newest hit is older
# than this can no longer affect any decision, so it is safe to drop — which is
# what makes the sweep effective: a bucket is otherwise only trimmed when that
# same client comes back, so one-shot callers would accumulate forever.
_max_window = 0.0

_hits: Dict[str, Deque[float]] = defaultdict(deque)


def _client_ip(request: Request) -> str:
    """The caller's address, trusting ``X-Forwarded-For`` only as configured.

    With N trusted hops, the client address is the Nth entry from the right —
    everything to its left was supplied by the client and is forgeable.
    """
    peer = request.client.host if request.client else "unknown"
    hops = _trusted_hops()
    if hops <= 0:
        return peer

    fwd = request.headers.get("x-forwarded-for")
    if not fwd:
        return peer
    parts = [p.strip() for p in fwd.split(",") if p.strip()]
    if not parts:
        return peer
    # parts[-1] is the address seen by the closest proxy, parts[-hops] by the
    # outermost trusted one. A shorter chain than advertised means someone
    # bypassed a proxy, so fall back to the leftmost entry we do have.
    idx = max(0, len(parts) - hops)
    return parts[idx]


def _sweep(now: float) -> None:
    """Drop buckets that can no longer affect a decision, bounding the key map."""
    global _last_sweep
    if now - _last_sweep < _SWEEP_INTERVAL:
        return
    _last_sweep = now
    cutoff = now - _max_window
    stale = [k for k, v in _hits.items() if not v or v[-1] < cutoff]
    for key in stale:
        del _hits[key]


def rate_limit(max_hits: int, window_seconds: float):
    """Dependency factory: allow at most `max_hits` requests per window per IP."""
    global _max_window
    _max_window = max(_max_window, window_seconds)

    def dep(request: Request) -> None:
        if not _ENABLED:
            return
        now = time.time()
        key = f"{request.url.path}:{_client_ip(request)}"
        bucket = _hits[key]
        cutoff = now - window_seconds
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= max_hits:
            retry = max(1, int(bucket[0] + window_seconds - now))
            raise HTTPException(
                status_code=429,
                detail="too many requests — slow down and try again shortly",
                headers={"Retry-After": str(retry)},
            )
        bucket.append(now)
        # Sweep after recording so an empty bucket created by this call's
        # defaultdict lookup is not immediately reclaimed.
        _sweep(now)

    return dep


def reset() -> None:
    """Clear all counters (used by tests)."""
    global _last_sweep
    _hits.clear()
    _last_sweep = 0.0
    _sweep_now()


def _sweep_now() -> None:
    """Force a sweep on the next request regardless of the interval."""
    global _last_sweep
    _last_sweep = 0.0

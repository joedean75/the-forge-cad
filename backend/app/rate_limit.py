"""Lightweight in-process rate limiting.

A sliding-window limiter keyed by client IP, used to blunt brute-force and
credential-stuffing against the auth endpoints. This is a first layer suitable
for a single instance; a horizontally-scaled deployment should front it with a
shared store (Redis) or an edge/WAF rate limit — the dependency shape here stays
the same, only the backing store changes.
"""

from __future__ import annotations

import os
import time
from collections import defaultdict, deque
from typing import Deque, Dict

from fastapi import HTTPException, Request

# Disable in tests / when explicitly turned off.
_ENABLED = os.environ.get("FORGE_RATE_LIMIT", "1").lower() not in ("0", "false", "no")

_hits: Dict[str, Deque[float]] = defaultdict(deque)


def _client_ip(request: Request) -> str:
    # Honor a single proxy hop if present; fall back to the socket peer.
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def rate_limit(max_hits: int, window_seconds: float):
    """Dependency factory: allow at most `max_hits` requests per window per IP."""

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

    return dep


def reset() -> None:
    """Clear all counters (used by tests)."""
    _hits.clear()

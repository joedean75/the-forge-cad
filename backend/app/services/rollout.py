"""Rollout mode: the switches a training loop needs and a product must not have.

Two things in the request path are correct for a paying customer and wrong for a
dataset.

`security.meter_operation` charges every mutating operation against a monthly
quota. An RL loop doing a few hundred thousand edits exhausts even the
enterprise allowance, and the failure arrives as a 402 in the middle of a
rollout.

`security.capped_resolution` clamps meshing resolution to the account's plan —
free 64, pro 192, enterprise 256. That means the *same document* yields
different geometry depending on who asked for it, which quietly destroys the
one property a dataset needs: that a recorded observation can be reproduced.

Both are opt-in bypasses rather than defaults, and both are scoped to a
`with` block rather than flipped globally, so nothing about the product path
changes by accident. `FORGE_ROLLOUT=1` sets it process-wide for a harness that
never serves a request.

Deliberately *not* a way to escape ownership or access control. Metering is a
billing counter; `document_service.can_access` is the security boundary and is
untouched here.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

_ACTIVE: ContextVar[bool] = ContextVar("forge_rollout_active", default=False)
_RESOLUTION: ContextVar[Optional[int]] = ContextVar("forge_rollout_resolution", default=None)


def _env_enabled() -> bool:
    return os.getenv("FORGE_ROLLOUT", "").strip().lower() in ("1", "true", "yes", "on")


def active() -> bool:
    """True inside a `rollout()` block, or when FORGE_ROLLOUT is set."""
    return _ACTIVE.get() or _env_enabled()


def pinned_resolution() -> Optional[int]:
    """The resolution every evaluation should use, if one was pinned."""
    if not active():
        return None
    pinned = _RESOLUTION.get()
    if pinned is not None:
        return int(pinned)
    from_env = os.getenv("FORGE_ROLLOUT_RESOLUTION", "").strip()
    return int(from_env) if from_env.isdigit() else None


@contextmanager
def rollout(resolution: Optional[int] = None) -> Iterator[None]:
    """Run a block with metering off and, optionally, resolution pinned.

    Pinning is the half that matters for reproducibility: without it the plan's
    cap silently decides geometry fidelity, so two runs of the same trajectory
    on different accounts produce different meshes and neither is wrong.
    """
    tokens = (_ACTIVE.set(True), _RESOLUTION.set(resolution))
    try:
        yield
    finally:
        _ACTIVE.reset(tokens[0])
        _RESOLUTION.reset(tokens[1])

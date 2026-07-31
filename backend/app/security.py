"""FastAPI auth dependencies: identity, entitlements, quota, resolution caps.

Two credential forms are accepted so both humans and agents are first-class:

  * ``Authorization: Bearer <jwt>``  — browser sessions (login)
  * ``X-API-Key: dps_live_...``      — programmatic / MCP-agent access

`AUTH_REQUIRED` (env `FORGE_AUTH_REQUIRED`, default off in dev) controls whether an
unauthenticated request is rejected or transparently attributed to a shared local
"dev" account, so the app still runs credential-free during development while the
production posture is a one-flag flip.
"""

from __future__ import annotations

import os
from typing import Optional

from fastapi import Depends, Header, HTTPException

from app.services import accounts, plans

AUTH_REQUIRED = os.environ.get("FORGE_AUTH_REQUIRED", "").lower() in ("1", "true", "yes")

_DEV_EMAIL = "dev@forge.local"
_DEV_PASSWORD = "dev-password-0"


def _dev_account() -> accounts.Account:
    """A shared, generously-provisioned account used when auth is not required."""
    acc = accounts.get_account(accounts._EMAIL_INDEX.get(_DEV_EMAIL, ""))
    if acc is None:
        try:
            acc = accounts.create_account(_DEV_EMAIL, _DEV_PASSWORD)
            accounts.set_plan(acc, "enterprise")
        except accounts.AuthError:
            acc = accounts.authenticate_password(_DEV_EMAIL, _DEV_PASSWORD)
    return acc


def current_account(
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None),
) -> accounts.Account:
    """Resolve the calling account, or raise 401 when auth is required."""
    try:
        if x_api_key:
            return accounts.authenticate_api_key(x_api_key)
        if authorization and authorization.lower().startswith("bearer "):
            return accounts.account_from_token(authorization.split(" ", 1)[1])
    except accounts.AuthError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))

    if AUTH_REQUIRED:
        raise HTTPException(status_code=401, detail="authentication required")
    return _dev_account()


def optional_account(
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None),
) -> Optional[accounts.Account]:
    try:
        return current_account(authorization=authorization, x_api_key=x_api_key)
    except HTTPException:
        return None


def require_entitlement(name: str):
    """Dependency factory gating an endpoint on a plan entitlement."""

    def _dep(acc: accounts.Account = Depends(current_account)) -> accounts.Account:
        if not acc.has_entitlement(name):
            raise HTTPException(
                status_code=402,
                detail=f"'{name}' requires a higher plan; your plan is '{acc.plan_id}'",
            )
        return acc

    return _dep


def meter_operation(acc: accounts.Account, n: int = 1) -> None:
    """Charge N mutating operations against the account's quota (raises 402)."""
    from app.services import rollout

    if rollout.active():
        return  # a training rollout is not billable; see services/rollout.py
    try:
        acc.record_op(n)
    except accounts.AuthError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))


def capped_resolution(acc: accounts.Account, requested: Optional[int], default: int = 128) -> int:
    """Clamp a requested meshing/solve resolution to the plan's cap.

    Inside a rollout with a pinned resolution the cap is bypassed, because the
    plan deciding geometry fidelity means the same document evaluates
    differently for different accounts — fine for a product, fatal for a
    dataset whose observations have to be reproducible.
    """
    from app.services import rollout

    pinned = rollout.pinned_resolution()
    if pinned is not None:
        return int(pinned)
    res = requested if requested else default
    return min(int(res), acc.plan.resolution_cap)

"""Authentication, account, and subscription endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from app.rate_limit import rate_limit
from app.security import current_account
from app.services import accounts, billing, plans

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Credential endpoints are the brute-force surface: cap attempts per IP.
_login_limit = rate_limit(max_hits=10, window_seconds=60.0)
_signup_limit = rate_limit(max_hits=5, window_seconds=300.0)


class Credentials(BaseModel):
    email: str
    password: str


class PlanChange(BaseModel):
    plan_id: str


class ApiKeyRequest(BaseModel):
    name: str = "default"


def _handle(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except accounts.AuthError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))


@router.post("/signup")
def signup(body: Credentials, _: None = Depends(_signup_limit)):
    acc = _handle(accounts.create_account, body.email, body.password)
    return {"token": accounts.issue_token(acc), "account": acc.public_view()}


@router.post("/login")
def login(body: Credentials, _: None = Depends(_login_limit)):
    acc = _handle(accounts.authenticate_password, body.email, body.password)
    return {"token": accounts.issue_token(acc), "account": acc.public_view()}


@router.get("/me")
def me(acc: accounts.Account = Depends(current_account)):
    return acc.public_view()


@router.get("/plans")
def list_plans():
    return {"plans": plans.plan_summaries(), "billing": billing.provider_status()}


@router.post("/subscribe")
def subscribe(body: PlanChange, acc: accounts.Account = Depends(current_account)):
    return _handle(billing.change_plan, acc, body.plan_id)


@router.post("/billing-portal")
def billing_portal(acc: accounts.Account = Depends(current_account)):
    """Hosted Stripe portal where the customer manages or cancels their plan."""
    return {"url": _handle(billing.create_portal_session, acc)}


@router.post("/webhook/stripe")
async def stripe_webhook(request: Request, stripe_signature: str = Header(default="")):
    """Stripe → us: the authoritative source for granting/revoking paid plans.

    Payment is only trusted here (signature-verified), never on the browser
    redirect back from Checkout.
    """
    payload = await request.body()
    return _handle(billing.handle_webhook, payload, stripe_signature)


@router.post("/api-keys")
def create_api_key(body: ApiKeyRequest, acc: accounts.Account = Depends(current_account)):
    raw = _handle(accounts.issue_api_key, acc, body.name)
    # The plaintext key is returned exactly once.
    return {"api_key": raw, "warning": "store this now — it is not shown again",
            "account": acc.public_view()}


@router.delete("/api-keys/{key_id}")
def revoke_api_key(key_id: str, acc: accounts.Account = Depends(current_account)):
    removed = accounts.revoke_api_key(acc, key_id)
    if not removed:
        raise HTTPException(status_code=404, detail="api key not found")
    return {"revoked": key_id, "account": acc.public_view()}

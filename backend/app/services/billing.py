"""Pluggable billing provider — real Stripe wiring, opt-in.

Follows the same opt-in pattern as `services/llm_client`: with no provider
configured the app is fully functional and plan changes apply immediately
(self-serve / trial), so development needs no payment credentials. When
`STRIPE_API_KEY` is set, upgrades to a paid plan go through a real **Stripe
Checkout Session** and the plan is only granted when Stripe confirms payment via
a signature-verified **webhook** — the redirect the browser follows back is never
trusted as proof of payment.

All Stripe REST calls go through `httpx` (already a dependency; no `stripe` SDK),
and everything provider-specific lives here so the rest of the app only ever
calls `change_plan` / `handle_webhook` / `create_portal_session`.

Config (all via env, `backend/.env`):
  STRIPE_API_KEY            secret key (sk_live_… / sk_test_…)   — enables live mode
  STRIPE_WEBHOOK_SECRET     whsec_…  — required to accept webhooks
  STRIPE_PRICE_PRO          price_…  recurring price id for the Pro plan
  STRIPE_PRICE_ENTERPRISE   price_…  recurring price id for the Enterprise plan
  PUBLIC_BASE_URL           base URL for post-checkout redirects (default localhost:5173)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from typing import Optional

import httpx

from app.services import accounts, plans

_LOGGER = logging.getLogger(__name__)

_STRIPE_API = "https://api.stripe.com/v1"
# Reject webhook timestamps outside this window to blunt replay attacks.
_WEBHOOK_TOLERANCE_SECONDS = 300


def _api_key() -> Optional[str]:
    return os.environ.get("STRIPE_API_KEY") or None


def _webhook_secret() -> Optional[str]:
    return os.environ.get("STRIPE_WEBHOOK_SECRET") or None


def _base_url() -> str:
    return os.environ.get("PUBLIC_BASE_URL", "http://localhost:5173").rstrip("/")


def _price_id(plan_id: str) -> Optional[str]:
    """The configured Stripe price id for a plan, or None if not set."""
    return os.environ.get(f"STRIPE_PRICE_{plan_id.upper()}") or None


def is_live() -> bool:
    return bool(_api_key())


def provider_status() -> dict:
    if not is_live():
        return {
            "provider": "local",
            "live": False,
            "note": "No payment provider configured — plan changes apply immediately (dev/self-serve).",
        }
    return {
        "provider": "stripe",
        "live": True,
        "note": None if _webhook_secret()
        else "STRIPE_API_KEY set but STRIPE_WEBHOOK_SECRET missing — webhooks will be rejected.",
    }


# ---------------------------------------------------------------------------
# Stripe REST helpers
# ---------------------------------------------------------------------------

def _stripe_post(path: str, form: dict) -> dict:
    """POST form-encoded data to the Stripe API and return the parsed JSON.

    Raises AuthError(502) on transport/HTTP failure so the caller surfaces a
    clean error instead of a raw stack trace.
    """
    key = _api_key()
    if not key:
        raise accounts.AuthError("Stripe is not configured", status=501)
    try:
        resp = httpx.post(
            f"{_STRIPE_API}/{path}",
            data=form,
            auth=(key, ""),
            timeout=20.0,
        )
    except httpx.HTTPError as exc:
        raise accounts.AuthError(f"could not reach Stripe: {exc}", status=502)
    if resp.status_code >= 400:
        detail = resp.json().get("error", {}).get("message", resp.text) if resp.content else resp.text
        raise accounts.AuthError(f"Stripe error: {detail}", status=502)
    return resp.json()


def create_checkout(acc: "accounts.Account", plan_id: str) -> Optional[str]:
    """Return a hosted-checkout URL when Stripe is live, else None.

    `None` signals the caller to fall back to an immediate local plan switch.
    """
    if not is_live():
        return None
    price = _price_id(plan_id)
    if not price:
        raise accounts.AuthError(
            f"no Stripe price configured for the '{plan_id}' plan "
            f"(set STRIPE_PRICE_{plan_id.upper()})",
            status=501,
        )
    form = {
        "mode": "subscription",
        "line_items[0][price]": price,
        "line_items[0][quantity]": "1",
        "success_url": f"{_base_url()}/?checkout=success&plan={plan_id}",
        "cancel_url": f"{_base_url()}/?checkout=cancelled",
        "client_reference_id": acc.id,
        "customer_email": acc.email,
        "metadata[account_id]": acc.id,
        "metadata[plan_id]": plan_id,
        # Carry identity onto the Subscription too, so later lifecycle events
        # (renewals, cancellations) can be attributed without the session.
        "subscription_data[metadata][account_id]": acc.id,
        "subscription_data[metadata][plan_id]": plan_id,
    }
    if acc.stripe_customer_id:
        form["customer"] = acc.stripe_customer_id
        del form["customer_email"]  # Stripe rejects both together
    session = _stripe_post("checkout/sessions", form)
    return session.get("url")


def create_portal_session(acc: "accounts.Account") -> str:
    """Return a Stripe billing-portal URL where the customer manages/cancels."""
    if not is_live():
        raise accounts.AuthError("billing portal requires Stripe to be configured", status=501)
    if not acc.stripe_customer_id:
        raise accounts.AuthError("no Stripe customer on file for this account", status=400)
    session = _stripe_post(
        "billing_portal/sessions",
        {"customer": acc.stripe_customer_id, "return_url": f"{_base_url()}/"},
    )
    return session["url"]


# ---------------------------------------------------------------------------
# Plan change entry point
# ---------------------------------------------------------------------------

def change_plan(acc: "accounts.Account", plan_id: str) -> dict:
    """Change an account's plan.

    Returns `{"status": "active", "plan": ...}` for a completed local change, or
    `{"status": "checkout", "url": ...}` when a hosted payment step is required.
    In live mode the plan is NOT granted here for paid upgrades — only the webhook
    grants it, after Stripe confirms payment.
    """
    if plan_id not in plans.PLANS:
        raise accounts.AuthError(f"unknown plan '{plan_id}'", status=400)

    target = plans.get_plan(plan_id)
    # Paid upgrades in live mode require a confirmed checkout.
    if target.price_usd_month > 0 and is_live():
        url = create_checkout(acc, plan_id)
        if url:
            return {"status": "checkout", "url": url}

    # Free tier, downgrades, or local dev mode: apply immediately.
    accounts.set_plan(acc, plan_id)
    return {"status": "active", "plan": acc.public_view()["plan"]}


# ---------------------------------------------------------------------------
# Webhook — the source of truth for granting/revoking paid plans
# ---------------------------------------------------------------------------

def verify_webhook(payload: bytes, sig_header: str, secret: Optional[str] = None) -> dict:
    """Verify a Stripe webhook signature and return the parsed event.

    Implements Stripe's scheme: the `Stripe-Signature` header is
    `t=<ts>,v1=<hexmac>[,v1=…]` where the MAC is HMAC-SHA256 over
    `"{ts}.{raw_body}"` keyed by the endpoint secret. Rejects stale timestamps to
    blunt replay, and uses a constant-time compare.
    """
    secret = secret or _webhook_secret()
    if not secret:
        raise accounts.AuthError("webhooks are not configured", status=503)
    if not sig_header:
        raise accounts.AuthError("missing Stripe-Signature header", status=400)

    parts = dict(
        item.split("=", 1) for item in sig_header.split(",") if "=" in item
    )
    timestamp = parts.get("t")
    provided = [v for k, v in (p.split("=", 1) for p in sig_header.split(",") if "=" in p) if k == "v1"]
    if not timestamp or not provided:
        raise accounts.AuthError("malformed Stripe-Signature header", status=400)

    try:
        ts = int(timestamp)
    except ValueError:
        raise accounts.AuthError("invalid webhook timestamp", status=400)
    if abs(time.time() - ts) > _WEBHOOK_TOLERANCE_SECONDS:
        raise accounts.AuthError("webhook timestamp outside tolerance", status=400)

    signed = f"{timestamp}.".encode() + payload
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, sig) for sig in provided):
        raise accounts.AuthError("webhook signature verification failed", status=400)

    try:
        return json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise accounts.AuthError(f"invalid webhook payload: {exc}", status=400)


def handle_webhook(payload: bytes, sig_header: str) -> dict:
    """Verify then apply a Stripe event. Returns a small ack for logging."""
    event = verify_webhook(payload, sig_header)
    etype = event.get("type", "")
    obj = event.get("data", {}).get("object", {})
    handled = _apply_event(etype, obj)
    return {"received": True, "type": etype, "handled": handled}


def _account_for(obj: dict) -> Optional["accounts.Account"]:
    """Resolve the account an event object refers to, by metadata or customer id."""
    meta = obj.get("metadata") or {}
    acc_id = meta.get("account_id") or obj.get("client_reference_id")
    if acc_id:
        acc = accounts.get_account(acc_id)
        if acc:
            return acc
    customer = obj.get("customer")
    if isinstance(customer, str):
        return accounts.account_by_stripe_customer(customer)
    return None


def _apply_event(etype: str, obj: dict) -> bool:
    """Grant/revoke plans in response to subscription-lifecycle events."""
    acc = _account_for(obj)
    if acc is None:
        _LOGGER.info("Stripe event %s: no matching account", etype)
        return False

    if etype == "checkout.session.completed":
        plan_id = (obj.get("metadata") or {}).get("plan_id")
        accounts.link_stripe(acc, obj.get("customer"), obj.get("subscription"))
        if plan_id in plans.PLANS:
            accounts.set_plan(acc, plan_id)
            _LOGGER.info("Granted plan %s to account %s via checkout", plan_id, acc.id)
            return True
        return False

    if etype in ("customer.subscription.deleted",):
        # Subscription ended → drop to free.
        accounts.set_plan(acc, plans.DEFAULT_PLAN)
        accounts.link_stripe(acc, obj.get("customer"), None)
        _LOGGER.info("Account %s subscription cancelled → free", acc.id)
        return True

    if etype == "customer.subscription.updated":
        plan_id = (obj.get("metadata") or {}).get("plan_id")
        if plan_id in plans.PLANS:
            accounts.set_plan(acc, plan_id)
            return True
        return False

    return False

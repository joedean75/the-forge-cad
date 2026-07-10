"""Stripe billing: webhook signature verification + plan lifecycle.

The webhook is the authoritative grant path — payment is trusted only after a
signature check, never on the browser redirect. These tests exercise that
security boundary without touching the real Stripe API.
"""

import hashlib
import hmac
import json
import time

import pytest

from app.services import accounts, billing, plans


def _sign(payload: bytes, secret: str, ts: int | None = None) -> str:
    ts = ts if ts is not None else int(time.time())
    signed = f"{ts}.".encode() + payload
    sig = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def _event(etype: str, obj: dict) -> bytes:
    return json.dumps({"type": etype, "data": {"object": obj}}).encode()


# -- provider status ---------------------------------------------------------

def test_local_mode_when_no_key(monkeypatch):
    monkeypatch.delenv("STRIPE_API_KEY", raising=False)
    assert billing.is_live() is False
    st = billing.provider_status()
    assert st["provider"] == "local" and st["live"] is False


def test_local_change_plan_is_immediate(monkeypatch):
    monkeypatch.delenv("STRIPE_API_KEY", raising=False)
    acc = accounts.create_account("local-billing@example.com", "supersecret1")
    res = billing.change_plan(acc, "pro")
    assert res["status"] == "active"
    assert acc.plan_id == "pro"


def test_paid_upgrade_requires_checkout_in_live_mode(monkeypatch):
    # Live mode + a configured price → change_plan returns a checkout, and does
    # NOT grant the plan locally.
    monkeypatch.setenv("STRIPE_API_KEY", "sk_test_x")
    monkeypatch.setenv("STRIPE_PRICE_PRO", "price_pro")
    monkeypatch.setattr(billing, "_stripe_post", lambda path, form: {"url": "https://checkout.stripe/x"})
    acc = accounts.create_account("live-billing@example.com", "supersecret1")
    res = billing.change_plan(acc, "pro")
    assert res["status"] == "checkout"
    assert res["url"].startswith("https://checkout.stripe")
    assert acc.plan_id == "free"  # not granted until the webhook confirms


def test_missing_price_is_501(monkeypatch):
    monkeypatch.setenv("STRIPE_API_KEY", "sk_test_x")
    monkeypatch.delenv("STRIPE_PRICE_ENTERPRISE", raising=False)
    acc = accounts.create_account("noprice@example.com", "supersecret1")
    with pytest.raises(accounts.AuthError) as ei:
        billing.change_plan(acc, "enterprise")
    assert ei.value.status == 501


# -- webhook signature -------------------------------------------------------

def test_webhook_rejects_bad_signature():
    payload = _event("checkout.session.completed", {})
    with pytest.raises(accounts.AuthError):
        billing.verify_webhook(payload, "t=123,v1=deadbeef", secret="whsec_test")


def test_webhook_rejects_stale_timestamp():
    payload = _event("checkout.session.completed", {})
    stale = _sign(payload, "whsec_test", ts=int(time.time()) - 10_000)
    with pytest.raises(accounts.AuthError):
        billing.verify_webhook(payload, stale, secret="whsec_test")


def test_webhook_accepts_valid_signature():
    payload = _event("ping", {})
    header = _sign(payload, "whsec_test")
    event = billing.verify_webhook(payload, header, secret="whsec_test")
    assert event["type"] == "ping"


# -- webhook lifecycle -------------------------------------------------------

def test_checkout_completed_grants_plan(monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    acc = accounts.create_account("grant@example.com", "supersecret1")
    assert acc.plan_id == "free"

    obj = {
        "metadata": {"account_id": acc.id, "plan_id": "pro"},
        "customer": "cus_123",
        "subscription": "sub_456",
    }
    payload = _event("checkout.session.completed", obj)
    ack = billing.handle_webhook(payload, _sign(payload, "whsec_test"))

    assert ack["handled"] is True
    assert acc.plan_id == "pro"
    assert acc.stripe_customer_id == "cus_123"
    assert acc.stripe_subscription_id == "sub_456"
    # Reverse lookup by customer id now resolves the account.
    assert accounts.account_by_stripe_customer("cus_123").id == acc.id


def test_subscription_deleted_downgrades(monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    acc = accounts.create_account("cancel@example.com", "supersecret1")
    accounts.set_plan(acc, "pro")
    accounts.link_stripe(acc, "cus_cancel", "sub_cancel")

    obj = {"customer": "cus_cancel"}
    payload = _event("customer.subscription.deleted", obj)
    billing.handle_webhook(payload, _sign(payload, "whsec_test"))

    assert acc.plan_id == plans.DEFAULT_PLAN


def test_webhook_endpoint_rejects_unsigned(monkeypatch):
    from fastapi.testclient import TestClient
    from main import app

    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    client = TestClient(app)
    r = client.post("/api/auth/webhook/stripe", content=b"{}", headers={"Stripe-Signature": "bogus"})
    assert r.status_code == 400

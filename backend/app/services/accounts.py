"""Accounts, authentication and usage metering — the subscription foundation.

An Account is a tenant: credentials, a subscription plan, API keys for
programmatic/agent access, and a rolling monthly usage counter for billing.
Passwords and API keys are stored only as PBKDF2-HMAC hashes (stdlib — no extra
deps). State is process-local (`_ACCOUNTS`) and persisted to
`backend/data/accounts/` so it survives restarts.

This module is provider-agnostic: `services/billing.py` maps a plan change onto a
real payment provider (Stripe) when configured, and degrades to local plan
switching when not.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from app.services import persistence, plans

_PBKDF2_ROUNDS = 200_000
_PERIOD_SECONDS = 30 * 24 * 3600  # rolling 30-day billing period

# id -> Account (rebuilt from disk on startup)
_ACCOUNTS: Dict[str, "Account"] = {}
# lowercased email -> account id
_EMAIL_INDEX: Dict[str, str] = {}
# sha256(api_key) -> account id (fast key lookup)
_APIKEY_INDEX: Dict[str, str] = {}
# stripe customer id -> account id (webhook reverse lookup)
_STRIPE_INDEX: Dict[str, str] = {}
# "provider:subject" -> account id (OAuth sign-in lookup)
_OAUTH_INDEX: Dict[str, str] = {}


class AuthError(Exception):
    """Raised for bad credentials, quota exhaustion, or missing entitlement."""

    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Hashing helpers
# ---------------------------------------------------------------------------

def _hash_secret(secret: str, salt: str) -> str:
    dk = hashlib.pbkdf2_hmac("sha256", secret.encode(), salt.encode(), _PBKDF2_ROUNDS)
    return dk.hex()


def _verify_secret(secret: str, salt: str, expected_hex: str) -> bool:
    return hmac.compare_digest(_hash_secret(secret, salt), expected_hex)


def _apikey_fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

@dataclass
class ApiKey:
    id: str
    name: str
    prefix: str          # first chars, shown in the UI for identification
    fingerprint: str     # sha256 of the full key (lookup + never reversible)
    created_at: str


@dataclass
class Account:
    id: str
    email: str
    password_salt: str
    password_hash: str
    plan_id: str = plans.DEFAULT_PLAN
    api_keys: List[ApiKey] = field(default_factory=list)
    ops_used: int = 0
    period_start: float = field(default_factory=time.time)
    created_at: str = ""
    # Stripe linkage (populated by the checkout webhook; None until a paid plan).
    stripe_customer_id: Optional[str] = None
    stripe_subscription_id: Optional[str] = None
    # OAuth linkage: provider id -> stable provider subject id ("sub").
    oauth_ids: Dict[str, str] = field(default_factory=dict)

    # -- plan / entitlement ------------------------------------------------
    @property
    def plan(self) -> plans.Plan:
        return plans.get_plan(self.plan_id)

    def has_entitlement(self, name: str) -> bool:
        return name in self.plan.features

    # -- serialization -----------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "email": self.email,
            "password_salt": self.password_salt,
            "password_hash": self.password_hash,
            "plan_id": self.plan_id,
            "api_keys": [k.__dict__ for k in self.api_keys],
            "ops_used": self.ops_used,
            "period_start": self.period_start,
            "created_at": self.created_at,
            "stripe_customer_id": self.stripe_customer_id,
            "stripe_subscription_id": self.stripe_subscription_id,
            "oauth_ids": self.oauth_ids,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Account":
        acc = cls(
            id=d["id"], email=d["email"],
            password_salt=d["password_salt"], password_hash=d["password_hash"],
            plan_id=d.get("plan_id", plans.DEFAULT_PLAN),
            ops_used=d.get("ops_used", 0),
            period_start=d.get("period_start", time.time()),
            created_at=d.get("created_at", ""),
            stripe_customer_id=d.get("stripe_customer_id"),
            stripe_subscription_id=d.get("stripe_subscription_id"),
            oauth_ids=dict(d.get("oauth_ids") or {}),
        )
        acc.api_keys = [ApiKey(**k) for k in d.get("api_keys", [])]
        return acc

    def public_view(self) -> dict:
        """Safe representation for API responses (no secrets)."""
        self._roll_period_if_needed()
        p = self.plan
        return {
            "id": self.id,
            "email": self.email,
            "plan": {"id": p.id, "name": p.name, "features": sorted(p.features)},
            "usage": {
                "ops_used": self.ops_used,
                "monthly_op_quota": p.monthly_op_quota,
                "period_start": self.period_start,
            },
            "api_keys": [
                {"id": k.id, "name": k.name, "prefix": k.prefix, "created_at": k.created_at}
                for k in self.api_keys
            ],
            "created_at": self.created_at,
        }

    # -- usage metering ----------------------------------------------------
    def _roll_period_if_needed(self) -> None:
        if time.time() - self.period_start >= _PERIOD_SECONDS:
            self.period_start = time.time()
            self.ops_used = 0

    def record_op(self, n: int = 1) -> None:
        """Meter a mutating operation; raises AuthError(402) over quota."""
        self._roll_period_if_needed()
        if self.ops_used + n > self.plan.monthly_op_quota:
            raise AuthError(
                f"monthly operation quota ({self.plan.monthly_op_quota}) exhausted for the "
                f"{self.plan.name} plan; upgrade to continue",
                status=402,
            )
        self.ops_used += n
        _persist(self)


# ---------------------------------------------------------------------------
# Persistence / index
# ---------------------------------------------------------------------------

def _persist(acc: Account) -> None:
    try:
        persistence.save_account(acc.id, acc.to_dict())
    except Exception:
        pass  # persistence must never break a request


def _index(acc: Account) -> None:
    _ACCOUNTS[acc.id] = acc
    _EMAIL_INDEX[acc.email.lower()] = acc.id
    for k in acc.api_keys:
        _APIKEY_INDEX[k.fingerprint] = acc.id
    if acc.stripe_customer_id:
        _STRIPE_INDEX[acc.stripe_customer_id] = acc.id
    for provider, subject in acc.oauth_ids.items():
        _OAUTH_INDEX[f"{provider}:{subject}"] = acc.id


def restore_accounts() -> None:
    """Rebuild in-memory indexes from disk on startup."""
    for acc_id in persistence.list_accounts():
        data = persistence.load_account(acc_id)
        if not data:
            continue
        try:
            _index(Account.from_dict(data))
        except Exception:
            continue


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_account(email: str, password: str) -> Account:
    email = (email or "").strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        raise AuthError("a valid email is required", status=400)
    if len(password or "") < 8:
        raise AuthError("password must be at least 8 characters", status=400)
    if email in _EMAIL_INDEX:
        raise AuthError("an account with that email already exists", status=409)

    salt = secrets.token_hex(16)
    acc = Account(
        id=str(uuid.uuid4()),
        email=email,
        password_salt=salt,
        password_hash=_hash_secret(password, salt),
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    _index(acc)
    _persist(acc)
    return acc


def create_oauth_account(email: str) -> Account:
    """Create an account for a verified OAuth identity — no usable password.

    A random throwaway password is hashed and discarded, so password login is
    impossible until the user explicitly sets one; only the linked provider
    (or a password reset flow) can sign this account in.
    """
    return create_account(email, secrets.token_urlsafe(32))


def account_by_oauth(provider: str, subject: str) -> Optional[Account]:
    return _ACCOUNTS.get(_OAUTH_INDEX.get(f"{provider}:{subject}", ""))


def link_oauth(acc: Account, provider: str, subject: str) -> None:
    """Bind a provider identity to an account (idempotent for the same pair).

    Refuses to re-bind a subject that already belongs to a different account,
    and refuses to overwrite an account's existing binding for the provider —
    either would let one identity hijack another.
    """
    existing_owner = _OAUTH_INDEX.get(f"{provider}:{subject}")
    if existing_owner is not None and existing_owner != acc.id:
        raise AuthError("this sign-in identity is already linked to another account", status=409)
    current = acc.oauth_ids.get(provider)
    if current is not None and current != subject:
        raise AuthError(
            f"this account is already linked to a different {provider} identity", status=409
        )
    acc.oauth_ids[provider] = subject
    _OAUTH_INDEX[f"{provider}:{subject}"] = acc.id
    _persist(acc)


# A fixed dummy hash so authentication spends the same PBKDF2 time whether or not
# the email exists — closing the timing side-channel that would otherwise let an
# attacker enumerate registered accounts.
_DUMMY_SALT = "0" * 32
_DUMMY_HASH = _hash_secret("invalid", _DUMMY_SALT)


def authenticate_password(email: str, password: str) -> Account:
    acc = _ACCOUNTS.get(_EMAIL_INDEX.get((email or "").strip().lower(), ""))
    if acc is None:
        _verify_secret(password or "", _DUMMY_SALT, _DUMMY_HASH)  # constant-time dummy work
        raise AuthError("invalid email or password", status=401)
    if not _verify_secret(password, acc.password_salt, acc.password_hash):
        raise AuthError("invalid email or password", status=401)
    return acc


def get_account(account_id: str) -> Optional[Account]:
    return _ACCOUNTS.get(account_id)


def account_by_email(email: str) -> Optional[Account]:
    return _ACCOUNTS.get(_EMAIL_INDEX.get((email or "").strip().lower(), ""))


def authenticate_api_key(key: str) -> Account:
    acc_id = _APIKEY_INDEX.get(_apikey_fingerprint(key or ""))
    acc = _ACCOUNTS.get(acc_id or "")
    if acc is None:
        raise AuthError("invalid API key", status=401)
    return acc


def issue_api_key(acc: Account, name: str = "default") -> str:
    """Create and return a new plaintext API key (shown once, then only hashed)."""
    if not acc.has_entitlement(plans.ENT_API_KEYS):
        raise AuthError("API keys require the Pro plan or above", status=402)
    raw = "dps_live_" + secrets.token_urlsafe(24)
    rec = ApiKey(
        id=str(uuid.uuid4()),
        name=name or "default",
        prefix=raw[:16],
        fingerprint=_apikey_fingerprint(raw),
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    acc.api_keys.append(rec)
    _APIKEY_INDEX[rec.fingerprint] = acc.id
    _persist(acc)
    return raw


def revoke_api_key(acc: Account, key_id: str) -> bool:
    before = len(acc.api_keys)
    kept = [k for k in acc.api_keys if k.id != key_id]
    for k in acc.api_keys:
        if k.id == key_id:
            _APIKEY_INDEX.pop(k.fingerprint, None)
    acc.api_keys = kept
    _persist(acc)
    return len(kept) != before


def set_plan(acc: Account, plan_id: str) -> Account:
    if plan_id not in plans.PLANS:
        raise AuthError(f"unknown plan '{plan_id}'", status=400)
    acc.plan_id = plan_id
    _persist(acc)
    return acc


def account_by_stripe_customer(customer_id: str) -> Optional[Account]:
    return _ACCOUNTS.get(_STRIPE_INDEX.get(customer_id or "", ""))


def link_stripe(acc: Account, customer_id: Optional[str], subscription_id: Optional[str]) -> None:
    """Record the Stripe customer/subscription identifiers for an account."""
    if customer_id:
        acc.stripe_customer_id = customer_id
        _STRIPE_INDEX[customer_id] = acc.id
    if subscription_id is not None:
        acc.stripe_subscription_id = subscription_id
    _persist(acc)


# ---------------------------------------------------------------------------
# JWT session tokens
# ---------------------------------------------------------------------------

def _jwt_secret() -> str:
    # A stable per-deployment secret. Set FORGE_JWT_SECRET in production; the
    # dev fallback keeps tokens valid across a single process lifetime.
    return os.environ.get("FORGE_JWT_SECRET") or _DEV_SECRET


_DEV_SECRET = secrets.token_hex(32)


def issue_token(acc: Account, ttl_seconds: int = 7 * 24 * 3600) -> str:
    import jwt

    now = int(time.time())
    payload = {"sub": acc.id, "email": acc.email, "iat": now, "exp": now + ttl_seconds}
    return jwt.encode(payload, _jwt_secret(), algorithm="HS256")


def account_from_token(token: str) -> Account:
    import jwt

    try:
        payload = jwt.decode(token, _jwt_secret(), algorithms=["HS256"])
    except Exception as exc:
        raise AuthError(f"invalid or expired token: {exc}", status=401)
    acc = _ACCOUNTS.get(payload.get("sub", ""))
    if acc is None:
        raise AuthError("token refers to unknown account", status=401)
    return acc

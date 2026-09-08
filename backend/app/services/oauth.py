"""OAuth 2.0 sign-in (Google / GitHub) — Authorization Code flow with PKCE.

Follows the same opt-in pattern as `services/billing`: with no provider
credentials configured, `/api/auth/oauth/providers` reports none and password
login remains the only path. Setting a provider's client id/secret enables it
with no code change.

Security posture:
  * Authorization Code + PKCE (S256): the authorization code alone is useless
    without the per-login verifier, blunting code interception.
  * Server-side ``state``: random, single-use, 10-minute TTL — CSRF protection
    on the callback.
  * The token exchange happens server-to-server (httpx). Provider access
    tokens never reach the browser and are never stored.
  * Only verified provider emails are accepted, so registering someone else's
    email unverified at a provider cannot claim their local account.
  * The browser receives a single-use, 60-second handoff code and redeems it
    via POST for the session JWT — the JWT itself never appears in a URL,
    browser history, or access logs.
  * All browser redirects are built from PUBLIC_BASE_URL, never from request
    parameters — no open-redirect surface.

Config (env, `backend/.env`):
  OAUTH_GOOGLE_CLIENT_ID / OAUTH_GOOGLE_CLIENT_SECRET
  OAUTH_GITHUB_CLIENT_ID / OAUTH_GITHUB_CLIENT_SECRET
  PUBLIC_BASE_URL   base URL used for the provider callback and the post-login
                    redirect (default http://localhost:5173; the Vite dev proxy
                    forwards /api/* to the backend)

Provider console setup: register the redirect URI
  {PUBLIC_BASE_URL}/api/auth/oauth/{provider}/callback
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlencode

import httpx

from app.services import accounts, persistence

# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

_GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
_GITHUB_USER_URL = "https://api.github.com/user"
_GITHUB_EMAILS_URL = "https://api.github.com/user/emails"

_HTTP_TIMEOUT = 10.0


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    authorize_url: str
    token_url: str
    scope: str


_PROVIDERS: Dict[str, Provider] = {
    "google": Provider(
        id="google",
        name="Google",
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        scope="openid email profile",
    ),
    "github": Provider(
        id="github",
        name="GitHub",
        authorize_url="https://github.com/login/oauth/authorize",
        token_url="https://github.com/login/oauth/access_token",
        scope="read:user user:email",
    ),
}


def _client_id(provider_id: str) -> Optional[str]:
    return os.environ.get(f"OAUTH_{provider_id.upper()}_CLIENT_ID") or None


def _client_secret(provider_id: str) -> Optional[str]:
    return os.environ.get(f"OAUTH_{provider_id.upper()}_CLIENT_SECRET") or None


def _is_configured(provider_id: str) -> bool:
    return bool(_client_id(provider_id) and _client_secret(provider_id))


def _base_url() -> str:
    return os.environ.get("PUBLIC_BASE_URL", "http://localhost:5173").rstrip("/")


def _redirect_uri(provider_id: str) -> str:
    return f"{_base_url()}/api/auth/oauth/{provider_id}/callback"


def enabled_providers() -> List[dict]:
    """Public list of sign-in providers that have credentials configured."""
    return [
        {"id": p.id, "name": p.name}
        for p in _PROVIDERS.values()
        if _is_configured(p.id)
    ]


def _get_provider(provider_id: str) -> Provider:
    provider = _PROVIDERS.get(provider_id)
    if provider is None:
        raise accounts.AuthError(f"unknown OAuth provider '{provider_id}'", status=404)
    if not _is_configured(provider.id):
        raise accounts.AuthError(f"{provider.name} sign-in is not configured", status=503)
    return provider


# ---------------------------------------------------------------------------
# Short-lived server-side state (login attempts + token handoff)
# ---------------------------------------------------------------------------

_STATE_TTL_SECONDS = 600.0    # provider round-trip may include account chooser
_HANDOFF_TTL_SECONDS = 60.0   # SPA redeems immediately after the redirect
_MAX_PENDING = 5000           # bound memory under a flood of /start hits


@dataclass
class _PendingLogin:
    provider_id: str
    code_verifier: str
    expires_at: float


@dataclass
class _Handoff:
    token: str
    account_id: str
    expires_at: float


# state -> in-flight login; handoff code -> issued session token. Single-use:
# an entry is read and deleted in one transaction on first redemption.
#
# **In the datastore, not in this process.** The flow spans two requests — the
# browser leaves for the provider and comes back — and with a second API worker
# the callback lands on whichever one the load balancer picks. The PKCE verifier
# was minted on a different one, so sign-in fails roughly half the time with
# "invalid or expired sign-in attempt", which reads as a provider problem and is
# not. Sixty seconds of state in a row looks like overkill right up until that.
#
# Nothing about the security properties changed: still server-side, still
# single-use, still never sent to the browser, and the delete is still what
# makes it single-use — `persistence.take_ephemeral` does the read and the
# delete atomically so two concurrent redemptions cannot both win.
_STATE_PREFIX = "oauth-state:"
_HANDOFF_PREFIX = "oauth-handoff:"


def _purge_expired() -> None:
    persistence.purge_ephemeral(time.time())


# ---------------------------------------------------------------------------
# Flow: begin -> callback (complete) -> exchange (redeem)
# ---------------------------------------------------------------------------


def begin_login(provider_id: str) -> str:
    """Start a login: mint state + PKCE pair, return the provider redirect URL."""
    provider = _get_provider(provider_id)
    _purge_expired()
    if persistence.count_ephemeral() >= _MAX_PENDING:
        raise accounts.AuthError("too many login attempts in flight — try again shortly", status=429)

    state = secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    persistence.save_ephemeral(_STATE_PREFIX + state, {
        "provider_id": provider.id,
        "code_verifier": code_verifier,
        "expires_at": time.time() + _STATE_TTL_SECONDS,
    })

    params = {
        "client_id": _client_id(provider.id),
        "redirect_uri": _redirect_uri(provider.id),
        "response_type": "code",
        "scope": provider.scope,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{provider.authorize_url}?{urlencode(params)}"


def complete_login(provider_id: str, state: str, code: str) -> str:
    """Callback half: verify state, exchange the code, sign the account in.

    Returns a single-use handoff code the SPA redeems for the session JWT.
    """
    provider = _get_provider(provider_id)
    _purge_expired()

    # Single-use, regardless of outcome: taken before the provider round trip,
    # so a replayed callback cannot re-run it.
    row = persistence.take_ephemeral(_STATE_PREFIX + (state or ""))
    pending = _PendingLogin(**row) if row else None
    if (pending is None or pending.provider_id != provider.id
            or pending.expires_at < time.time()):
        raise accounts.AuthError("invalid or expired sign-in attempt — please retry", status=400)

    subject, email = _fetch_identity(provider, code, pending.code_verifier)
    acc = _account_for_identity(provider.id, subject, email)

    handoff_code = secrets.token_urlsafe(32)
    persistence.save_ephemeral(_HANDOFF_PREFIX + handoff_code, {
        "token": accounts.issue_token(acc),
        "account_id": acc.id,
        "expires_at": time.time() + _HANDOFF_TTL_SECONDS,
    })
    return handoff_code


def redeem_handoff(code: str) -> Tuple[str, accounts.Account]:
    """Trade a one-time handoff code for the session JWT (single use)."""
    _purge_expired()
    row = persistence.take_ephemeral(_HANDOFF_PREFIX + (code or ""))
    handoff = _Handoff(**row) if row else None
    if handoff is None or handoff.expires_at < time.time():
        raise accounts.AuthError("invalid or expired sign-in code — please retry", status=401)
    acc = accounts.get_account(handoff.account_id)
    if acc is None:
        raise accounts.AuthError("account no longer exists", status=401)
    return handoff.token, acc


def frontend_redirect(handoff: Optional[str] = None, error: Optional[str] = None) -> str:
    """Where the callback sends the browser — always our own SPA, never a
    caller-supplied URL. Success carries the one-time code; failures a message."""
    if handoff:
        query = urlencode({"oauth_code": handoff})
    else:
        query = urlencode({"oauth_error": error or "sign-in failed"})
    return f"{_base_url()}/?{query}#signin"


# ---------------------------------------------------------------------------
# Provider identity fetch (server-to-server)
# ---------------------------------------------------------------------------


def _fetch_identity(provider: Provider, code: str, code_verifier: str) -> Tuple[str, str]:
    """Exchange the authorization code and return (subject_id, verified_email).

    Raises AuthError(403) when the provider cannot attest a verified email —
    unverified emails must never link to or create a local account.
    """
    data = {
        "client_id": _client_id(provider.id),
        "client_secret": _client_secret(provider.id),
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": _redirect_uri(provider.id),
        "code_verifier": code_verifier,
    }
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            token_resp = client.post(
                provider.token_url, data=data, headers={"Accept": "application/json"}
            )
            token_resp.raise_for_status()
            access_token = token_resp.json().get("access_token")
            if not access_token:
                raise accounts.AuthError("provider did not return an access token", status=502)
            bearer = {"Authorization": f"Bearer {access_token}"}

            if provider.id == "google":
                info = client.get(_GOOGLE_USERINFO_URL, headers=bearer)
                info.raise_for_status()
                return _google_identity(info.json())

            user = client.get(_GITHUB_USER_URL, headers=bearer)
            user.raise_for_status()
            emails = client.get(_GITHUB_EMAILS_URL, headers=bearer)
            emails.raise_for_status()
            return _github_identity(user.json(), emails.json())
    except httpx.HTTPError as exc:
        raise accounts.AuthError(f"could not reach {provider.name}: {exc}", status=502)


def _google_identity(info: dict) -> Tuple[str, str]:
    email = (info.get("email") or "").strip().lower()
    if not info.get("sub") or not email:
        raise accounts.AuthError("Google profile is missing an id or email", status=502)
    if not info.get("email_verified"):
        raise accounts.AuthError("Google account email is not verified", status=403)
    return str(info["sub"]), email


def _github_identity(user: dict, emails: list) -> Tuple[str, str]:
    if not user.get("id"):
        raise accounts.AuthError("GitHub profile is missing an id", status=502)
    verified = [e for e in emails if isinstance(e, dict) and e.get("verified") and e.get("email")]
    primary = next((e for e in verified if e.get("primary")), None) or (verified[0] if verified else None)
    if primary is None:
        raise accounts.AuthError("GitHub account has no verified email", status=403)
    return str(user["id"]), str(primary["email"]).strip().lower()


# ---------------------------------------------------------------------------
# Account resolution
# ---------------------------------------------------------------------------


def _account_for_identity(provider_id: str, subject: str, email: str) -> accounts.Account:
    """Find or create the local account for a verified provider identity.

    Match order: stable provider subject id first, then verified email (which
    links OAuth onto an existing password account), else a fresh account.
    """
    acc = accounts.account_by_oauth(provider_id, subject)
    if acc is not None:
        return acc
    acc = accounts.account_by_email(email)
    if acc is None:
        acc = accounts.create_oauth_account(email)
    accounts.link_oauth(acc, provider_id, subject)
    return acc

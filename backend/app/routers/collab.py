"""WebSocket endpoint for real-time document collaboration (presence + updates).

Browsers can't set headers on WebSocket connects, so the session JWT (or API
key) rides a ``token`` query parameter. Access control mirrors the HTTP rules:
no access → close 4404 (existence not leaked), bad credentials → close 4401.
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import Dict, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect

from app import security
from app.security import current_account
from app.services import accounts, document_service, realtime

_LOGGER = logging.getLogger(__name__)

router = APIRouter(tags=["collab"])

# Single-use, short-lived connect tickets so the browser never puts a real JWT
# or API key in the WebSocket URL (query strings land in access logs/proxies).
# ticket -> (account_id, doc_id, expires_at). In-process, like the hub itself.
_TICKETS: Dict[str, Tuple[str, str, float]] = {}
_TICKET_TTL = 60.0


def _prune_tickets() -> None:
    now = time.time()
    for t in [t for t, (_, _, exp) in _TICKETS.items() if exp < now]:
        _TICKETS.pop(t, None)


@router.post("/api/documents/{doc_id}/ws-ticket")
def create_ws_ticket(doc_id: str, acc: accounts.Account = Depends(current_account)):
    """Issue a single-use ticket for connecting to this document's WebSocket."""
    doc = document_service.get_document(doc_id)
    if doc is None or not document_service.can_access(doc, acc.id):
        raise HTTPException(status_code=404, detail="Document not found")
    _prune_tickets()
    ticket = "wst_" + secrets.token_urlsafe(24)
    _TICKETS[ticket] = (acc.id, doc_id, time.time() + _TICKET_TTL)
    return {"ticket": ticket, "expires_in": int(_TICKET_TTL)}


def _resolve_account(token: str, doc_id: str):
    if token.startswith("wst_"):
        entry = _TICKETS.pop(token, None)  # single use
        if entry is None:
            return None
        account_id, ticket_doc, expires_at = entry
        if ticket_doc != doc_id or expires_at < time.time():
            return None
        return accounts.get_account(account_id)
    if token:
        try:
            if token.startswith("fk_"):
                return accounts.authenticate_api_key(token)
            return accounts.account_from_token(token)
        except accounts.AuthError:
            return None
    if security.AUTH_REQUIRED:
        return None
    return security._dev_account()


@router.websocket("/api/documents/{doc_id}/ws")
async def document_ws(websocket: WebSocket, doc_id: str, token: str = Query("")):
    acc = _resolve_account(token, doc_id)
    if acc is None:
        await websocket.close(code=4401)
        return
    doc = document_service.get_document(doc_id)
    if doc is None or not document_service.can_access(doc, acc.id):
        await websocket.close(code=4404)
        return

    await websocket.accept()
    cid = await realtime.join(doc_id, websocket, {"id": acc.id, "email": acc.email})
    try:
        while True:
            # Client messages are keepalives for now; presence/updates are server-pushed.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001 — never let one socket kill the app
        _LOGGER.debug("document ws error: %s", exc)
    finally:
        await realtime.leave(doc_id, cid)

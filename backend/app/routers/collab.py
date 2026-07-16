"""WebSocket endpoint for real-time document collaboration (presence + updates).

Browsers can't set headers on WebSocket connects, so the session JWT (or API
key) rides a ``token`` query parameter. Access control mirrors the HTTP rules:
no access → close 4404 (existence not leaked), bad credentials → close 4401.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from app import security
from app.services import accounts, document_service, realtime

_LOGGER = logging.getLogger(__name__)

router = APIRouter(tags=["collab"])


def _resolve_account(token: str):
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
    acc = _resolve_account(token)
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

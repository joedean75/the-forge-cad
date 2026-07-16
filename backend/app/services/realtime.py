"""Real-time collaboration hub (roadmap feature 05, phase 1).

In-process WebSocket rooms per document: **presence** (who's here) and
**document_updated** broadcasts so co-editors refresh live. Single-instance by
design — the hub lives in process memory, matching the deployment model
documented in DEPLOY.md; multi-instance needs an external pub/sub (Redis) swap.

Mutations are announced from ``document_service._persist`` (the single choke
point every save flows through). That code runs in worker threads, so
``notify_document_updated`` is sync and schedules the async broadcast onto the
server's event loop captured at startup — and no-ops cleanly when there is no
loop (unit tests, scripts) or nobody is in the room.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from typing import Any, Dict, Optional

from fastapi import WebSocket

_LOGGER = logging.getLogger(__name__)

_loop: Optional[asyncio.AbstractEventLoop] = None
_conn_ids = itertools.count(1)

# doc_id -> {conn_id: {"ws": WebSocket, "user": {id, email}}}
_ROOMS: Dict[str, Dict[int, Dict[str, Any]]] = {}


def set_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Capture the server event loop (called once at startup)."""
    global _loop
    _loop = loop


def _presence_payload(doc_id: str) -> dict:
    users = []
    seen = set()
    for conn in _ROOMS.get(doc_id, {}).values():
        u = conn["user"]
        if u["id"] not in seen:
            seen.add(u["id"])
            users.append(u)
    return {"type": "presence", "doc_id": doc_id, "users": users}


async def _broadcast(doc_id: str, payload: dict, skip: Optional[int] = None) -> None:
    room = _ROOMS.get(doc_id)
    if not room:
        return
    dead = []
    for cid, conn in list(room.items()):
        if cid == skip:
            continue
        try:
            await conn["ws"].send_json(payload)
        except Exception:
            dead.append(cid)
    for cid in dead:
        room.pop(cid, None)


async def join(doc_id: str, ws: WebSocket, user: dict) -> int:
    cid = next(_conn_ids)
    _ROOMS.setdefault(doc_id, {})[cid] = {"ws": ws, "user": user}
    await _broadcast(doc_id, _presence_payload(doc_id))
    return cid


async def leave(doc_id: str, cid: int) -> None:
    room = _ROOMS.get(doc_id)
    if not room:
        return
    room.pop(cid, None)
    if room:
        await _broadcast(doc_id, _presence_payload(doc_id))
    else:
        _ROOMS.pop(doc_id, None)


def room_size(doc_id: str) -> int:
    return len(_ROOMS.get(doc_id, {}))


def notify_document_updated(doc_id: str, updated_at: str) -> None:
    """Announce a saved change to everyone in the room. Sync + thread-safe."""
    if _loop is None or not _ROOMS.get(doc_id):
        return
    payload = {"type": "document_updated", "doc_id": doc_id, "updated_at": updated_at}
    try:
        asyncio.run_coroutine_threadsafe(_broadcast(doc_id, payload), _loop)
    except RuntimeError:  # loop closed (shutdown) — nothing to announce
        pass

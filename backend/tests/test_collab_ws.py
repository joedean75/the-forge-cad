"""Tests for real-time collaboration (roadmap feature 05): presence + live updates."""

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import pytest

from main import app


def _drain_until(ws, wanted_type: str, tries: int = 10) -> dict:
    for _ in range(tries):
        msg = ws.receive_json()
        if msg["type"] == wanted_type:
            return msg
    raise AssertionError(f"no '{wanted_type}' event received")


def test_presence_and_live_update_broadcast():
    # Context-managed client so lifespan runs and the hub captures the loop.
    with TestClient(app) as client:
        r = client.post("/api/documents", json={"prompt": "box 0.05x0.05x0.05 m", "resolution": 48})
        assert r.status_code == 200, r.text
        doc_id = r.json()["id"]

        with client.websocket_connect(f"/api/documents/{doc_id}/ws") as ws1:
            hello = ws1.receive_json()
            assert hello["type"] == "presence"
            assert len(hello["users"]) == 1

            with client.websocket_connect(f"/api/documents/{doc_id}/ws") as ws2:
                ws2.receive_json()  # ws2's own presence snapshot

                # An HTTP mutation must reach both live sockets.
                op = client.post(
                    f"/api/documents/{doc_id}/op",
                    json={"op": "add_primitive",
                          "args": {"shape": "sphere", "operation": "union",
                                   "params": {"radius": 0.02, "center": {"x": 0, "y": 0, "z": 0.04}}}},
                )
                assert op.status_code == 200, op.text
                evt1 = _drain_until(ws1, "document_updated")
                evt2 = _drain_until(ws2, "document_updated")
                assert evt1["doc_id"] == doc_id and evt2["doc_id"] == doc_id
                assert evt1["updated_at"]


def test_ws_unknown_document_closes_4404():
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/api/documents/nope/ws") as ws:
                ws.receive_json()
        assert exc.value.code == 4404


def test_ws_ticket_is_single_use():
    with TestClient(app) as client:
        r = client.post("/api/documents", json={"resolution": 48})
        doc_id = r.json()["id"]
        t = client.post(f"/api/documents/{doc_id}/ws-ticket")
        assert t.status_code == 200, t.text
        ticket = t.json()["ticket"]

        with client.websocket_connect(f"/api/documents/{doc_id}/ws?token={ticket}") as ws:
            assert ws.receive_json()["type"] == "presence"

        # Consumed on first connect — replaying the same ticket is rejected.
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(f"/api/documents/{doc_id}/ws?token={ticket}") as ws:
                ws.receive_json()
        assert exc.value.code == 4401


def test_ws_bad_token_closes_4401():
    with TestClient(app) as client:
        r = client.post("/api/documents", json={"resolution": 48})
        doc_id = r.json()["id"]
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(f"/api/documents/{doc_id}/ws?token=garbage") as ws:
                ws.receive_json()
        assert exc.value.code == 4401

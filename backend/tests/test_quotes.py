"""Tests for instant quotes + orders (roadmap feature 03: "Get it made")."""

from fastapi.testclient import TestClient

from main import app
from app.services import quotes

client = TestClient(app)


def _part(prompt="box 0.1x0.06x0.02 m") -> str:
    r = client.post("/api/documents", json={"prompt": prompt, "resolution": 64})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_quotes_cover_viable_processes():
    doc_id = _part()
    r = client.post(f"/api/documents/{doc_id}/quotes", json={"quantity": 1})
    assert r.status_code == 200, r.text
    body = r.json()
    processes = {q["process"] for q in body["quotes"]}
    # A 100 mm part fits every process family.
    assert {"cnc-3axis", "fdm", "sla", "lpbf-metal"} <= processes
    assert body["provider"] == "local-estimator"
    for q in body["quotes"]:
        assert q["total"] > 0 and q["lead_time_days"] > 0
    # Sorted cheapest-first.
    totals = [q["total"] for q in body["quotes"]]
    assert totals == sorted(totals)


def test_quantity_scales_price():
    doc_id = _part()
    one = client.post(f"/api/documents/{doc_id}/quotes", json={"quantity": 1}).json()
    ten = client.post(f"/api/documents/{doc_id}/quotes", json={"quantity": 10}).json()
    q1 = next(q for q in one["quotes"] if q["process"] == "fdm")
    q10 = next(q for q in ten["quotes"] if q["process"] == "fdm")
    assert q10["total"] > q1["total"] * 5  # scales with qty (setup amortized)


def test_place_and_list_order():
    doc_id = _part()
    r = client.post(
        f"/api/documents/{doc_id}/orders",
        json={"process": "cnc-3axis", "quantity": 2, "notes": "anodize black"},
    )
    assert r.status_code == 200, r.text
    order = r.json()
    assert order["status"] == "submitted"
    assert order["quote"]["process"] == "cnc-3axis"
    assert order["notes"] == "anodize black"

    listed = client.get(f"/api/documents/{doc_id}/orders").json()["orders"]
    assert any(o["id"] == order["id"] for o in listed)


def test_order_unknown_process_is_422():
    doc_id = _part()
    r = client.post(f"/api/documents/{doc_id}/orders", json={"process": "waterjet-9000"})
    assert r.status_code == 422
    assert "not quotable" in r.json()["detail"]


def test_provider_status_local_by_default():
    status = quotes.provider_status()
    assert status["live"] is False
    assert status["mode"] == "local-estimator"

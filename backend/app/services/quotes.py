"""Instant manufacturing quotes + orders — "Get it made" (roadmap feature 03).

Follows the same opt-in provider pattern as ``billing.py``: with no credentials
configured, a **local estimator** prices every viable process from real geometry
(volume, bbox, machining-removal heuristics) so dev needs nothing. Set
``QUOTE_PROVIDER_URL`` (+ optional ``QUOTE_PROVIDER_KEY``) in ``backend/.env`` to
relay quoting to a live supplier API (OpenAI-style JSON POST; response merged
over the local estimate, graceful fallback on failure).

Orders are persisted request-for-quote records (datastore kind ``order``). In
local mode they're immediately "submitted" for tracking; a live provider can
attach its own confirmation/checkout URL.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import List, Optional

import httpx

from app.models.schemas import Quote
from app.services import document_service, persistence

_LOGGER = logging.getLogger(__name__)

# Material pricing (USD/kg) and density (kg/m³) by process family.
_MATERIALS = {
    "cnc-al6061": {"label": "Aluminum 6061", "density": 2700.0, "usd_kg": 12.0},
    "fdm-pla": {"label": "PLA", "density": 1240.0, "usd_kg": 25.0},
    "sla-resin": {"label": "Standard resin", "density": 1150.0, "usd_kg": 60.0},
    "lpbf-alsi10mg": {"label": "AlSi10Mg", "density": 2670.0, "usd_kg": 90.0},
}


def _provider_url() -> Optional[str]:
    return os.environ.get("QUOTE_PROVIDER_URL") or None


def is_live() -> bool:
    return _provider_url() is not None


def provider_status() -> dict:
    return {
        "live": is_live(),
        "mode": "provider" if is_live() else "local-estimator",
        "detail": "External supplier quoting enabled."
        if is_live()
        else "Local cost model — set QUOTE_PROVIDER_URL to relay to a supplier API.",
    }


def _round(v: float) -> float:
    return round(float(v), 2)


def _local_quotes(volume_m3: float, bbox_mm: tuple, quantity: int) -> List[Quote]:
    """Price every viable process from geometry. All models are deliberately
    simple but dimensionally honest: material mass + machine time + setup,
    with per-process viability limits from the bounding box."""
    lx, ly, lz = bbox_mm
    max_dim = max(lx, ly, lz)
    quotes: List[Quote] = []
    qty = max(1, int(quantity))

    def add(process, mat_key, machine_rate_hr, hours_per_part, setup, lead_days):
        mat = _MATERIALS[mat_key]
        mass_kg = volume_m3 * mat["density"]
        material = mass_kg * mat["usd_kg"] * qty
        machine = machine_rate_hr * hours_per_part * qty
        total = material + machine + setup
        quotes.append(
            Quote(
                process=process,
                material_cost=_round(material),
                machine_cost=_round(machine),
                setup_cost=_round(setup),
                total=_round(total),
                lead_time_days=lead_days,
            )
        )

    vol_cm3 = volume_m3 * 1e6
    if max_dim <= 500:
        # CNC: time scales with removed material (stock bbox minus part).
        stock_cm3 = (lx * ly * lz) / 1000.0
        removal = max(stock_cm3 - vol_cm3, vol_cm3 * 0.2)
        add("cnc-3axis", "cnc-al6061", 75.0, 0.25 + removal / 400.0, 40.0, 5 + (2 if qty > 10 else 0))
    if max_dim <= 300:
        add("fdm", "fdm-pla", 4.0, 0.3 + vol_cm3 / 25.0, 5.0, 2)
    if max_dim <= 180:
        add("sla", "sla-resin", 8.0, 0.4 + vol_cm3 / 40.0, 10.0, 3)
    if max_dim <= 250:
        add("lpbf-metal", "lpbf-alsi10mg", 120.0, 0.5 + vol_cm3 / 15.0, 150.0, 8)

    quotes.sort(key=lambda q: q.total)
    return quotes


def _provider_quotes(doc, volume_m3, bbox_mm, quantity) -> Optional[List[Quote]]:
    """Relay to the configured supplier API; None on any failure (fallback local)."""
    url = _provider_url()
    if not url:
        return None
    headers = {"Content-Type": "application/json"}
    key = os.environ.get("QUOTE_PROVIDER_KEY")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    payload = {
        "part": {"name": doc.name, "volume_m3": volume_m3, "bbox_mm": list(bbox_mm)},
        "quantity": quantity,
    }
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(url.rstrip("/") + "/quotes", headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
        return [Quote(**q) for q in data.get("quotes", [])] or None
    except Exception as exc:
        _LOGGER.warning("quote provider failed, falling back to local estimator: %s", exc)
        return None


def quote_document(doc_id: str, quantity: int = 1, resolution: int = 96) -> dict:
    """Instant quotes for a document across every viable process."""
    doc = document_service.get_document(doc_id)
    if doc is None:
        raise KeyError(doc_id)
    if not doc.derived.bbox or doc.derived.volume <= 0:
        document_service.evaluate_document(doc, resolution=resolution)
    b = doc.derived.bbox
    if b is None or doc.derived.volume <= 0:
        raise ValueError("The part has no solid geometry to quote.")
    bbox_mm = (
        (b.max.x - b.min.x) * 1000.0,
        (b.max.y - b.min.y) * 1000.0,
        (b.max.z - b.min.z) * 1000.0,
    )
    volume = float(doc.derived.volume)
    quotes = _provider_quotes(doc, volume, bbox_mm, quantity) or _local_quotes(volume, bbox_mm, quantity)
    return {
        "document_id": doc.id,
        "quantity": max(1, int(quantity)),
        "volume_cm3": _round(volume * 1e6),
        "bbox_mm": [_round(v) for v in bbox_mm],
        "provider": provider_status()["mode"],
        "quotes": [q.model_dump() for q in quotes],
    }


def place_order(doc_id: str, process: str, quantity: int, owner_id: Optional[str], notes: str = "") -> dict:
    """Create a persisted order (RFQ) for the selected process quote."""
    quoted = quote_document(doc_id, quantity)
    match = next((q for q in quoted["quotes"] if q["process"] == process), None)
    if match is None:
        raise ValueError(
            f"Process '{process}' is not quotable for this part. Available: "
            + ", ".join(q["process"] for q in quoted["quotes"])
        )
    order = {
        "id": str(uuid.uuid4()),
        "document_id": doc_id,
        "owner_id": owner_id,
        "process": process,
        "quantity": quoted["quantity"],
        "quote": match,
        "notes": notes[:2000],
        "status": "submitted",
        "provider": quoted["provider"],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    persistence.save_order(order["id"], order)
    return order


def list_orders(doc_id: str) -> List[dict]:
    out = persistence.list_orders_for_document(doc_id)
    out.sort(key=lambda o: o.get("created_at", ""), reverse=True)
    return out

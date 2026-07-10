"""Subscription plans and their entitlements.

A single source of truth for what each tier is allowed to do. Entitlements are
checked by the auth layer (`app/security.py`) to gate premium capability, and the
monthly operation quota meters usage for billing. Keeping this declarative makes
it the seam a real billing provider (Stripe price → plan id) maps onto.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List


@dataclass(frozen=True)
class Plan:
    id: str
    name: str
    price_usd_month: float
    max_documents: int              # -1 = unlimited
    resolution_cap: int             # max meshing/solve resolution
    monthly_op_quota: int           # mutating operations per billing period
    seats: int
    features: frozenset             # entitlement flags (see ENTITLEMENTS)


# Entitlement flag names (used by security.require_entitlement).
ENT_STEP_EXPORT = "step_export"
ENT_CAM = "cam"
ENT_OPTIMIZE = "generative_optimize"
ENT_MACHINE = "machine_control"
ENT_API_KEYS = "api_keys"           # programmatic / agent access

ENTITLEMENTS = (ENT_STEP_EXPORT, ENT_CAM, ENT_OPTIMIZE, ENT_MACHINE, ENT_API_KEYS)


PLANS: Dict[str, Plan] = {
    "free": Plan(
        id="free", name="Free", price_usd_month=0.0,
        max_documents=3, resolution_cap=64, monthly_op_quota=200, seats=1,
        features=frozenset(),
    ),
    "pro": Plan(
        id="pro", name="Pro", price_usd_month=49.0,
        max_documents=200, resolution_cap=192, monthly_op_quota=20_000, seats=1,
        features=frozenset({ENT_STEP_EXPORT, ENT_CAM, ENT_OPTIMIZE, ENT_API_KEYS, ENT_MACHINE}),
    ),
    "enterprise": Plan(
        id="enterprise", name="Enterprise", price_usd_month=499.0,
        max_documents=-1, resolution_cap=256, monthly_op_quota=1_000_000, seats=25,
        features=frozenset(ENTITLEMENTS),
    ),
}

DEFAULT_PLAN = "free"


def get_plan(plan_id: str) -> Plan:
    return PLANS.get(plan_id, PLANS[DEFAULT_PLAN])


def plan_summaries() -> List[dict]:
    """Public catalog for the pricing UI."""
    return [
        {
            "id": p.id,
            "name": p.name,
            "price_usd_month": p.price_usd_month,
            "max_documents": p.max_documents,
            "resolution_cap": p.resolution_cap,
            "monthly_op_quota": p.monthly_op_quota,
            "seats": p.seats,
            "features": sorted(p.features),
        }
        for p in PLANS.values()
    ]

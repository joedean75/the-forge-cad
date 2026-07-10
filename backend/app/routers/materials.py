from fastapi import APIRouter
from typing import List

from app.models.schemas import Material

router = APIRouter(prefix="/api/materials", tags=["materials"])

_MATERIALS = [
    Material(
        id="al-7075",
        name="Aluminum 7075",
        density=2810,
        yield_strength=503,
        youngs_modulus=71.7,
        poissons_ratio=0.33,
        color="#9ca3af",
    ),
    Material(
        id="al-6061",
        name="Aluminum 6061",
        density=2700,
        yield_strength=276,
        youngs_modulus=68.9,
        poissons_ratio=0.33,
        color="#b0b8c4",
    ),
    Material(
        id="ti-6al-4v",
        name="Titanium Ti-6Al-4V",
        density=4430,
        yield_strength=880,
        youngs_modulus=113.8,
        poissons_ratio=0.31,
        color="#6b7280",
    ),
    Material(
        id="steel-4140",
        name="Steel 4140",
        density=7850,
        yield_strength=655,
        youngs_modulus=190,
        poissons_ratio=0.29,
        color="#4b5563",
    ),
]


@router.get("", response_model=List[Material])
def list_materials():
    return _MATERIALS

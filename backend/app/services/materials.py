"""Material catalog (roadmap production-feature 06).

A single source of engineering material properties — density, stiffness, yield,
and cost — assignable to a document via ``material_id``. Everything downstream
that used a hard-coded aluminium density (mass, mass properties, simulation,
DFM, quotes, BOM) reads from here instead, so a part's numbers reflect its real
spec once a material is bound.

Credential-free and offline: the catalog is a built-in table. A document with no
material falls back to :data:`DEFAULT_MATERIAL_ID`.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel

DEFAULT_MATERIAL_ID = "al-6061"


class Material(BaseModel):
    id: str
    name: str
    category: str  # metal | polymer | resin | composite
    density: float          # kg/m^3
    youngs_modulus: float   # Pa
    poisson_ratio: float
    yield_strength: float   # Pa
    cost_per_kg: float      # USD/kg
    # Default manufacturing process this material is typically ordered through,
    # linking a material choice to the quote/DFM process family.
    process: str            # cnc | fdm | sla | lpbf

    @property
    def density_g_cm3(self) -> float:
        return self.density / 1000.0


_CATALOG: Dict[str, Material] = {
    m.id: m for m in [
        Material(id="al-6061", name="Aluminum 6061-T6", category="metal", density=2700.0,
                 youngs_modulus=68.9e9, poisson_ratio=0.33, yield_strength=276e6, cost_per_kg=12.0, process="cnc"),
        Material(id="al-7075", name="Aluminum 7075-T6", category="metal", density=2810.0,
                 youngs_modulus=71.7e9, poisson_ratio=0.33, yield_strength=503e6, cost_per_kg=18.0, process="cnc"),
        Material(id="steel-1018", name="Steel 1018 (mild)", category="metal", density=7870.0,
                 youngs_modulus=205e9, poisson_ratio=0.29, yield_strength=370e6, cost_per_kg=3.5, process="cnc"),
        Material(id="steel-304", name="Stainless 304", category="metal", density=8000.0,
                 youngs_modulus=193e9, poisson_ratio=0.29, yield_strength=215e6, cost_per_kg=6.5, process="cnc"),
        Material(id="ti-6al4v", name="Titanium Ti-6Al-4V", category="metal", density=4430.0,
                 youngs_modulus=113.8e9, poisson_ratio=0.34, yield_strength=880e6, cost_per_kg=95.0, process="lpbf"),
        Material(id="alsi10mg", name="AlSi10Mg (LPBF)", category="metal", density=2670.0,
                 youngs_modulus=70e9, poisson_ratio=0.33, yield_strength=230e6, cost_per_kg=90.0, process="lpbf"),
        Material(id="brass-360", name="Brass 360", category="metal", density=8500.0,
                 youngs_modulus=97e9, poisson_ratio=0.31, yield_strength=125e6, cost_per_kg=9.0, process="cnc"),
        Material(id="pla", name="PLA", category="polymer", density=1240.0,
                 youngs_modulus=3.5e9, poisson_ratio=0.36, yield_strength=50e6, cost_per_kg=25.0, process="fdm"),
        Material(id="abs", name="ABS", category="polymer", density=1040.0,
                 youngs_modulus=2.3e9, poisson_ratio=0.35, yield_strength=40e6, cost_per_kg=28.0, process="fdm"),
        Material(id="petg", name="PETG", category="polymer", density=1270.0,
                 youngs_modulus=2.1e9, poisson_ratio=0.40, yield_strength=50e6, cost_per_kg=30.0, process="fdm"),
        Material(id="nylon-pa12", name="Nylon PA12", category="polymer", density=1010.0,
                 youngs_modulus=1.7e9, poisson_ratio=0.39, yield_strength=48e6, cost_per_kg=55.0, process="fdm"),
        Material(id="resin-std", name="Standard Resin (SLA)", category="resin", density=1150.0,
                 youngs_modulus=2.8e9, poisson_ratio=0.35, yield_strength=55e6, cost_per_kg=60.0, process="sla"),
    ]
}


def list_materials() -> List[Material]:
    return list(_CATALOG.values())


def get_material(material_id: Optional[str]) -> Material:
    """Resolve a material id to its record, falling back to the default."""
    if material_id and material_id in _CATALOG:
        return _CATALOG[material_id]
    return _CATALOG[DEFAULT_MATERIAL_ID]


def density_of(material_id: Optional[str]) -> float:
    return get_material(material_id).density

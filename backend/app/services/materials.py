"""Material catalog — the single source of engineering material properties.

Density, stiffness, yield and cost, assignable to a document via ``material_id``.
Everything downstream that would otherwise hard-code a density (mass, mass
properties, simulation, DFM, quotes, BOM) reads from here, so a part's numbers
reflect its real spec once a material is bound.

The catalog spans two audiences and marks which is which via ``Material.tier``:

* ``maker``        — what a hobbyist actually has on the shelf: FDM filaments,
                     SLA resins, plywood/MDF, acrylic, mild steel and 6061.
* ``professional`` — specified engineering stock: structural and tool steels,
                     stainless grades, aerospace aluminium, titanium, engineering
                     thermoplastics and composites.

``category`` groups them for browsing (see :data:`CATEGORIES` for display order
and labels). Every property is SI — density kg/m^3, moduli and strengths Pa —
and :func:`to_spec_material` converts to the MPa/GPa view the solver schema uses.

Material ids are a persisted document field (``DesignDocument.material_id``), so
existing ids are never renamed; new grades are added alongside them.

Credential-free and offline: this is a built-in table. A document with no
material falls back to :data:`DEFAULT_MATERIAL_ID`.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel

DEFAULT_MATERIAL_ID = "al-6061"

# Display order + labels for the grouped material picker. Keeping this beside the
# catalog means a new category reaches the UI by adding one entry here.
CATEGORIES: List[Dict[str, str]] = [
    {"id": "aluminum", "label": "Aluminum", "blurb": "Light, machinable, corrosion-resistant"},
    {"id": "steel", "label": "Steel", "blurb": "Structural, tool and alloy steels"},
    {"id": "stainless", "label": "Stainless", "blurb": "Corrosion-resistant steels"},
    {"id": "titanium", "label": "Titanium", "blurb": "High strength-to-weight, biocompatible"},
    {"id": "copper-alloy", "label": "Copper & Brass", "blurb": "Conductive, bearing and decorative alloys"},
    {"id": "filament", "label": "3D Printing · Filament", "blurb": "FDM/FFF spool materials"},
    {"id": "resin", "label": "3D Printing · Resin", "blurb": "SLA/DLP photopolymers"},
    {"id": "powder", "label": "3D Printing · Metal Powder", "blurb": "LPBF / DMLS alloys"},
    {"id": "plastic-stock", "label": "Engineering Plastic", "blurb": "Machinable plastic stock"},
    {"id": "elastomer", "label": "Elastomer", "blurb": "Flexible and rubber-like"},
    {"id": "composite", "label": "Composite", "blurb": "Fibre-reinforced laminates"},
    {"id": "wood", "label": "Wood & Sheet Goods", "blurb": "Ply, MDF and hardwood"},
]

_CATEGORY_COLORS: Dict[str, str] = {
    "aluminum": "#b0b8c4",
    "steel": "#4b5563",
    "stainless": "#9aa4b2",
    "titanium": "#6b7280",
    "copper-alloy": "#b08d57",
    "filament": "#5eae7b",
    "resin": "#8b7bd8",
    "powder": "#8a9099",
    "plastic-stock": "#6fa8c7",
    "elastomer": "#54596b",
    "composite": "#3f4550",
    "wood": "#c8a06a",
}

TIERS = ("maker", "professional")


class Material(BaseModel):
    id: str
    name: str
    category: str           # one of CATEGORIES[*].id
    tier: str               # maker | professional
    density: float          # kg/m^3
    youngs_modulus: float   # Pa
    poisson_ratio: float
    yield_strength: float   # Pa
    cost_per_kg: float      # USD/kg
    # Default manufacturing process this material is typically ordered through,
    # linking a material choice to the quote/DFM process family.
    process: str            # cnc | fdm | sla | lpbf | laser | waterjet
    # Short practical note: what someone picks this for.
    notes: str = ""

    @property
    def density_g_cm3(self) -> float:
        return self.density / 1000.0

    @property
    def color(self) -> str:
        return _CATEGORY_COLORS.get(self.category, "#8a9099")


def _m(**kw) -> Material:
    return Material(**kw)


_CATALOG: Dict[str, Material] = {
    m.id: m for m in [
        # ---------------------------------------------------------------- aluminum
        _m(id="al-6061", name="Aluminum 6061-T6", category="aluminum", tier="maker",
           density=2700.0, youngs_modulus=68.9e9, poisson_ratio=0.33, yield_strength=276e6,
           cost_per_kg=12.0, process="cnc",
           notes="General-purpose alloy: weldable, machinable, cheap."),
        _m(id="al-5052", name="Aluminum 5052-H32", category="aluminum", tier="maker",
           density=2680.0, youngs_modulus=70.3e9, poisson_ratio=0.33, yield_strength=193e6,
           cost_per_kg=11.0, process="laser",
           notes="The sheet-metal aluminium: excellent formability, marine grade."),
        _m(id="al-7075", name="Aluminum 7075-T6", category="aluminum", tier="professional",
           density=2810.0, youngs_modulus=71.7e9, poisson_ratio=0.33, yield_strength=503e6,
           cost_per_kg=18.0, process="cnc",
           notes="Aerospace grade — near-steel strength, poor weldability."),
        _m(id="al-2024", name="Aluminum 2024-T3", category="aluminum", tier="professional",
           density=2780.0, youngs_modulus=73.1e9, poisson_ratio=0.33, yield_strength=345e6,
           cost_per_kg=16.0, process="cnc",
           notes="High fatigue resistance for airframe structure; corrodes readily."),

        # ------------------------------------------------------------------- steel
        _m(id="steel-1018", name="Steel 1018 (mild)", category="steel", tier="maker",
           density=7870.0, youngs_modulus=205e9, poisson_ratio=0.29, yield_strength=370e6,
           cost_per_kg=3.5, process="cnc",
           notes="Cheap, weldable low-carbon bar stock — the shop default."),
        _m(id="steel-a36", name="Steel A36 (structural)", category="steel", tier="maker",
           density=7850.0, youngs_modulus=200e9, poisson_ratio=0.26, yield_strength=250e6,
           cost_per_kg=2.8, process="laser",
           notes="Structural plate and angle; standard laser/plasma cut stock."),
        _m(id="steel-4140", name="Steel 4140 (chromoly)", category="steel", tier="professional",
           density=7850.0, youngs_modulus=190e9, poisson_ratio=0.29, yield_strength=655e6,
           cost_per_kg=5.5, process="cnc",
           notes="Heat-treatable alloy steel for shafts, tooling and roll cages."),
        _m(id="steel-4340", name="Steel 4340", category="steel", tier="professional",
           density=7850.0, youngs_modulus=205e9, poisson_ratio=0.29, yield_strength=862e6,
           cost_per_kg=7.5, process="cnc",
           notes="Deep-hardening, high-toughness alloy for gear and shaft duty."),
        _m(id="steel-o1", name="Tool Steel O1", category="steel", tier="professional",
           density=7850.0, youngs_modulus=205e9, poisson_ratio=0.30, yield_strength=1000e6,
           cost_per_kg=14.0, process="cnc",
           notes="Oil-hardening tool steel for punches, dies and cutting edges."),
        _m(id="steel-a2", name="Tool Steel A2", category="steel", tier="professional",
           density=7860.0, youngs_modulus=203e9, poisson_ratio=0.30, yield_strength=1170e6,
           cost_per_kg=17.0, process="cnc",
           notes="Air-hardening and dimensionally stable — precision tooling."),

        # --------------------------------------------------------------- stainless
        # id kept as `steel-304` (predates the stainless category) so documents
        # already bound to it keep resolving.
        _m(id="steel-304", name="Stainless 304", category="stainless", tier="maker",
           density=8000.0, youngs_modulus=193e9, poisson_ratio=0.29, yield_strength=215e6,
           cost_per_kg=6.5, process="cnc",
           notes="General-purpose stainless: food-safe, formable, weldable."),
        _m(id="ss-316l", name="Stainless 316L", category="stainless", tier="professional",
           density=8000.0, youngs_modulus=193e9, poisson_ratio=0.30, yield_strength=290e6,
           cost_per_kg=9.5, process="cnc",
           notes="Molybdenum-bearing: marine and chemical corrosion resistance."),
        _m(id="ss-17-4ph", name="Stainless 17-4 PH", category="stainless", tier="professional",
           density=7800.0, youngs_modulus=197e9, poisson_ratio=0.27, yield_strength=1100e6,
           cost_per_kg=15.0, process="cnc",
           notes="Precipitation-hardening — high strength plus corrosion resistance."),

        # ---------------------------------------------------------------- titanium
        _m(id="ti-6al4v", name="Titanium Ti-6Al-4V (Gr 5)", category="titanium", tier="professional",
           density=4430.0, youngs_modulus=113.8e9, poisson_ratio=0.34, yield_strength=880e6,
           cost_per_kg=95.0, process="cnc",
           notes="The workhorse titanium: aerospace, medical, motorsport."),
        _m(id="ti-gr2", name="Titanium Grade 2 (CP)", category="titanium", tier="professional",
           density=4510.0, youngs_modulus=105e9, poisson_ratio=0.34, yield_strength=275e6,
           cost_per_kg=60.0, process="cnc",
           notes="Commercially pure — formable, weldable, very corrosion resistant."),

        # ------------------------------------------------------------ copper alloy
        _m(id="brass-360", name="Brass 360 (free-cutting)", category="copper-alloy", tier="maker",
           density=8500.0, youngs_modulus=97e9, poisson_ratio=0.31, yield_strength=125e6,
           cost_per_kg=9.0, process="cnc",
           notes="Best machinability of any metal; fittings and decorative parts."),
        _m(id="bronze-932", name="Bearing Bronze 932", category="copper-alloy", tier="professional",
           density=8930.0, youngs_modulus=100e9, poisson_ratio=0.34, yield_strength=125e6,
           cost_per_kg=13.0, process="cnc",
           notes="Self-lubricating bushings and sleeve bearings."),
        _m(id="copper-c110", name="Copper C110 (ETP)", category="copper-alloy", tier="professional",
           density=8940.0, youngs_modulus=117e9, poisson_ratio=0.34, yield_strength=70e6,
           cost_per_kg=11.0, process="cnc",
           notes="Electrical and thermal conductor: busbars, heat spreaders."),

        # ---------------------------------------------------------------- filament
        _m(id="pla", name="PLA", category="filament", tier="maker",
           density=1240.0, youngs_modulus=3.5e9, poisson_ratio=0.36, yield_strength=50e6,
           cost_per_kg=25.0, process="fdm",
           notes="Easiest to print, stiff but brittle; softens near 60 °C."),
        _m(id="petg", name="PETG", category="filament", tier="maker",
           density=1270.0, youngs_modulus=2.1e9, poisson_ratio=0.40, yield_strength=50e6,
           cost_per_kg=30.0, process="fdm",
           notes="Tougher than PLA and water-resistant — good default for parts."),
        _m(id="abs", name="ABS", category="filament", tier="maker",
           density=1040.0, youngs_modulus=2.3e9, poisson_ratio=0.35, yield_strength=40e6,
           cost_per_kg=28.0, process="fdm",
           notes="Heat and impact resistant; warps without an enclosure."),
        _m(id="asa", name="ASA", category="filament", tier="maker",
           density=1070.0, youngs_modulus=2.0e9, poisson_ratio=0.35, yield_strength=44e6,
           cost_per_kg=34.0, process="fdm",
           notes="ABS-like with UV stability — the outdoor choice."),
        _m(id="nylon-pa12", name="Nylon PA12", category="filament", tier="professional",
           density=1010.0, youngs_modulus=1.7e9, poisson_ratio=0.39, yield_strength=48e6,
           cost_per_kg=55.0, process="fdm",
           notes="Tough, low-friction, fatigue resistant; absorbs moisture."),
        _m(id="pa6-cf", name="Nylon PA6-CF (carbon filled)", category="filament", tier="professional",
           density=1200.0, youngs_modulus=8.0e9, poisson_ratio=0.38, yield_strength=95e6,
           cost_per_kg=95.0, process="fdm",
           notes="Stiff structural filament for jigs and end-use brackets; abrasive."),
        _m(id="pc", name="Polycarbonate", category="filament", tier="professional",
           density=1200.0, youngs_modulus=2.3e9, poisson_ratio=0.37, yield_strength=62e6,
           cost_per_kg=60.0, process="fdm",
           notes="High impact strength and ~110 °C service temperature."),

        # ------------------------------------------------------------------- resin
        _m(id="resin-std", name="Standard Resin (SLA)", category="resin", tier="maker",
           density=1150.0, youngs_modulus=2.8e9, poisson_ratio=0.35, yield_strength=55e6,
           cost_per_kg=60.0, process="sla",
           notes="Fine detail for display models; brittle and UV-sensitive."),
        _m(id="resin-tough", name="Tough Resin (SLA)", category="resin", tier="professional",
           density=1180.0, youngs_modulus=2.0e9, poisson_ratio=0.35, yield_strength=45e6,
           cost_per_kg=95.0, process="sla",
           notes="ABS-like toughness for snap fits and functional prototypes."),
        _m(id="resin-castable", name="Castable Wax Resin", category="resin", tier="professional",
           density=1100.0, youngs_modulus=1.6e9, poisson_ratio=0.35, yield_strength=30e6,
           cost_per_kg=140.0, process="sla",
           notes="Burns out clean for investment casting."),

        # ------------------------------------------------------------------ powder
        _m(id="alsi10mg", name="AlSi10Mg (LPBF)", category="powder", tier="professional",
           density=2670.0, youngs_modulus=70e9, poisson_ratio=0.33, yield_strength=230e6,
           cost_per_kg=90.0, process="lpbf",
           notes="Printable cast-aluminium analogue for topology-optimised parts."),
        _m(id="ti64-lpbf", name="Ti-6Al-4V (LPBF)", category="powder", tier="professional",
           density=4430.0, youngs_modulus=110e9, poisson_ratio=0.34, yield_strength=1000e6,
           cost_per_kg=350.0, process="lpbf",
           notes="Printed titanium for lightweight aerospace and medical parts."),
        _m(id="ss316l-lpbf", name="Stainless 316L (LPBF)", category="powder", tier="professional",
           density=7990.0, youngs_modulus=190e9, poisson_ratio=0.30, yield_strength=500e6,
           cost_per_kg=120.0, process="lpbf",
           notes="Printed stainless for manifolds and corrosion-resistant tooling."),

        # ----------------------------------------------------------- plastic stock
        _m(id="delrin", name="Acetal / Delrin (POM)", category="plastic-stock", tier="professional",
           density=1410.0, youngs_modulus=3.1e9, poisson_ratio=0.35, yield_strength=70e6,
           cost_per_kg=18.0, process="cnc",
           notes="Stiff, low-friction, dimensionally stable — gears and bushings."),
        _m(id="hdpe", name="HDPE", category="plastic-stock", tier="maker",
           density=960.0, youngs_modulus=1.1e9, poisson_ratio=0.42, yield_strength=26e6,
           cost_per_kg=6.0, process="cnc",
           notes="Cheap, chemical-resistant, self-lubricating; hard to bond."),
        _m(id="acrylic", name="Acrylic (PMMA)", category="plastic-stock", tier="maker",
           density=1180.0, youngs_modulus=3.2e9, poisson_ratio=0.37, yield_strength=70e6,
           cost_per_kg=9.0, process="laser",
           notes="Laser-cuts to a polished edge; brittle and solvent-sensitive."),
        _m(id="peek", name="PEEK", category="plastic-stock", tier="professional",
           density=1320.0, youngs_modulus=3.6e9, poisson_ratio=0.38, yield_strength=97e6,
           cost_per_kg=290.0, process="cnc",
           notes="High-temp, chemical and radiation resistant engineering polymer."),
        _m(id="uhmw", name="UHMW-PE", category="plastic-stock", tier="professional",
           density=940.0, youngs_modulus=0.7e9, poisson_ratio=0.42, yield_strength=21e6,
           cost_per_kg=12.0, process="cnc",
           notes="Extreme abrasion resistance and low friction — wear strips."),

        # --------------------------------------------------------------- elastomer
        _m(id="tpu-95a", name="TPU 95A (flexible)", category="elastomer", tier="maker",
           density=1210.0, youngs_modulus=0.026e9, poisson_ratio=0.48, yield_strength=9e6,
           cost_per_kg=45.0, process="fdm",
           notes="Flexible filament for gaskets, bumpers and grips."),
        _m(id="silicone-70a", name="Silicone 70A", category="elastomer", tier="professional",
           density=1200.0, youngs_modulus=0.005e9, poisson_ratio=0.49, yield_strength=7e6,
           cost_per_kg=40.0, process="cnc",
           notes="Seals and gaskets over a wide temperature range."),

        # --------------------------------------------------------------- composite
        _m(id="cfrp", name="Carbon Fibre Laminate", category="composite", tier="professional",
           density=1600.0, youngs_modulus=70e9, poisson_ratio=0.30, yield_strength=600e6,
           cost_per_kg=120.0, process="waterjet",
           notes="Very high specific stiffness; quasi-isotropic layup assumed."),
        _m(id="gfrp", name="Fibreglass Laminate (G10)", category="composite", tier="professional",
           density=1850.0, youngs_modulus=18e9, poisson_ratio=0.13, yield_strength=310e6,
           cost_per_kg=25.0, process="waterjet",
           notes="Electrically insulating structural sheet; abrasive to machine."),

        # -------------------------------------------------------------------- wood
        _m(id="plywood-birch", name="Birch Plywood", category="wood", tier="maker",
           density=680.0, youngs_modulus=10.0e9, poisson_ratio=0.30, yield_strength=40e6,
           cost_per_kg=4.0, process="laser",
           notes="The maker standard for laser-cut enclosures and jigs."),
        _m(id="mdf", name="MDF", category="wood", tier="maker",
           density=750.0, youngs_modulus=3.6e9, poisson_ratio=0.30, yield_strength=18e6,
           cost_per_kg=1.5, process="laser",
           notes="Cheap, flat and uniform; swells with moisture."),
        _m(id="hardwood-oak", name="Oak (hardwood)", category="wood", tier="maker",
           density=750.0, youngs_modulus=12.0e9, poisson_ratio=0.35, yield_strength=47e6,
           cost_per_kg=8.0, process="cnc",
           notes="Furniture-grade stock; properties quoted along the grain."),
    ]
}


def list_materials() -> List[Material]:
    return list(_CATALOG.values())


def list_categories() -> List[Dict[str, object]]:
    """Category descriptors in display order, each with its material count.

    Empty categories are omitted so the picker never renders a dead group.
    """
    counts: Dict[str, int] = {}
    for m in _CATALOG.values():
        counts[m.category] = counts.get(m.category, 0) + 1
    return [{**c, "count": counts[c["id"]]} for c in CATEGORIES if counts.get(c["id"])]


def get_material(material_id: Optional[str]) -> Material:
    """Resolve a material id to its record, falling back to the default."""
    if material_id and material_id in _CATALOG:
        return _CATALOG[material_id]
    return _CATALOG[DEFAULT_MATERIAL_ID]


def density_of(material_id: Optional[str]) -> float:
    return get_material(material_id).density


def to_spec_material(m: Material):
    """Adapt a catalog entry to the solver schema's ``Material``.

    That schema predates this catalog and is documented in MPa/GPa, while the
    catalog is SI throughout, so stiffness and strength are scaled here. Keeping
    the conversion in one place means the FEA/DFM maths reads the units it
    expects and the catalog stays consistently SI.
    """
    from app.models.schemas import Material as SpecMaterial

    return SpecMaterial(
        id=m.id,
        name=m.name,
        density=m.density,
        yield_strength=m.yield_strength / 1e6,    # Pa -> MPa
        youngs_modulus=m.youngs_modulus / 1e9,    # Pa -> GPa
        poissons_ratio=m.poisson_ratio,
        color=m.color,
    )


def spec_materials() -> List:
    """The whole catalog in solver-schema form (MPa/GPa)."""
    return [to_spec_material(m) for m in _CATALOG.values()]


def spec_material_for(material_id: Optional[str]):
    """Solver-schema view of one material id, with the same default fallback."""
    return to_spec_material(get_material(material_id))

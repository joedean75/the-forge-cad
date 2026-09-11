"""What a part weighs, and how we know — one answer, in one place.

Every mass figure in this system was a *geometric* one: volume from the mesh,
times the density of the assigned material. That is exactly right for a bracket
somebody modelled and exactly wrong for everything they bought.

The failure is not small. A NEMA 17's envelope is a solid steel block where the
real motor is laminations, windings and air: 669 g modelled against ~350 g real,
so a robot arm with four of them comes out 1.3 kg heavy. A LiPo modelled as a
box of aluminium is nonsense in both directions. And these are precisely the
components an RC or drone build hangs its CG on — the mass budget is wrong in
the place it matters most, and nothing on the report said so.

`parts_catalog.Entry.typical_mass_g` has carried the published figure for a
while and *nothing read it*. Its own comment admitted as much: "The document
keeps reporting its geometric mass, because that is what its geometry is." True,
and not what a BOM total or a centre of gravity is asking.

So: four sources, ranked, and **the source travels with the number**.

- **`override`** — someone put the part on a scale, or read its datasheet, and
  said so. Nothing beats a measurement of the actual object, so it wins
  outright.
- **`catalog`** — the published typical mass for the purchased part this
  document *is* (`catalog_ref`). Beats geometry because the geometry is a
  deliberate simplification: `fidelity` already says `envelope`.
- **`geometric`** — volume times material density. Right for anything modelled,
  and the default it has always been.
- **`unknown`** — no geometry, or no material, or a body that will not evaluate.
  A real answer, reported as zero *with a reason*, rather than a confident 0.0
  that a total silently absorbs.

**Every mass report states its source.** That is the whole point of TOOL-02 and
it is not decoration: a CG that is 4 mm off because one component's mass came
from an envelope is indistinguishable from a correct one unless the report says
which components were guessed at.
"""

from __future__ import annotations

from typing import Any, Dict, NamedTuple, Optional

#: Ranked most to least trustworthy. The order is the resolution order in
#: `effective_mass`, so it is written once here rather than implied by an
#: if-chain somebody later reorders.
OVERRIDE = "override"
CATALOG = "catalog"
GEOMETRIC = "geometric"
UNKNOWN = "unknown"

SOURCES = (OVERRIDE, CATALOG, GEOMETRIC, UNKNOWN)

G = 1000.0


class MassFact(NamedTuple):
    """A mass, where it came from, and what to say about it."""

    kg: float
    source: str
    #: One line a person can read: "published typical mass for NEMA 17", "solid
    #: envelope in 6061-T6". Not a restatement of `source` — the *basis*.
    basis: str
    #: Set when the source is not `geometric` and geometry exists anyway, so a
    #: caller can see the size of the correction it is applying.
    geometric_kg: Optional[float] = None

    @property
    def grams(self) -> float:
        return self.kg * G

    @property
    def ratio_to_geometric(self) -> Optional[float]:
        """How far the trusted figure is from what the geometry implies.

        This is what scales an inertia tensor (see `assembly_mass`), and it is
        also the most useful single number for judging an envelope: a motor at
        0.52 is telling you its solid block is nearly twice the real thing.
        """
        if self.geometric_kg is None or self.geometric_kg <= 0:
            return None
        return self.kg / self.geometric_kg

    def as_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "mass_g": round(self.grams, 3),
            "mass_source": self.source,
            "mass_basis": self.basis,
        }
        if self.geometric_kg is not None:
            out["geometric_mass_g"] = round(self.geometric_kg * G, 3)
            ratio = self.ratio_to_geometric
            if ratio is not None:
                out["mass_ratio_to_geometry"] = round(ratio, 4)
        return out


def geometric_mass(doc) -> Optional[float]:
    """Volume times density, or `None` when there is no body to measure.

    Read off `derived`, which is where every other consumer reads it, rather
    than re-meshing. A document with no solid has no geometric mass — not a
    zero one, which is a different claim.
    """
    derived = getattr(doc, "derived", None)
    if derived is None or derived.error:
        return None
    if not derived.volume:
        return None
    return float(derived.mass or 0.0) or None


def catalog_mass(doc) -> tuple[Optional[float], str]:
    """The published typical mass for the purchased part this document is.

    Covers both halves of the catalog under one `catalog_ref`, the same way
    `standard_parts.document_for_key` does: a generated entry's published
    figure, or a supplier part the user filed with a mass from its datasheet.
    A row that carries no mass returns `None` — a purchased part whose mass
    nobody has stated is `unknown`, not zero.
    """
    ref = getattr(doc, "catalog_ref", None)
    if not ref:
        return None, ""

    from app.services import parts_catalog

    # Through `published_mass_g`, not `Entry.typical_mass_g` directly. A
    # reference carries its *configuration* — `lipo?mass_g=320` is a different
    # part from the default pack — and for a family whose mass is a declared
    # parameter the configured figure is the label on the object, which beats a
    # typical for the class. Reading the entry's default instead reported the
    # 165 g pack's weight for a 320 g one that had said so.
    grams = parts_catalog.published_mass_g(ref)
    if grams:
        key, given = parts_catalog.parse_ref(ref)
        entry = parts_catalog.by_key(key)
        name = (parts_catalog.designation_of(entry, given) if entry is not None
                else str(ref))
        return float(grams) / G, f"published typical mass for {name}"

    # A supplier part the user filed. Looked up second because the generated
    # catalog is the smaller, cheaper lookup.
    try:
        from app.services import supplier_parts

        row = supplier_parts.by_key(ref, owner_id=getattr(doc, "owner_id", None))
    except Exception:  # noqa: BLE001 — a catalog miss is not a mass failure
        row = None
    # Only a *declared* mass. The import path fills a supplier row's `mass_g`
    # from the body it just read, so an undeclared one is the geometric figure
    # by another name — trusting it would label geometry as published data and
    # then report it as a correction to itself.
    if row and row.get("mass_g") and row.get("mass_source") == "declared":
        return float(row["mass_g"]) / G, (
            f"supplier-declared mass for {row.get('designation') or ref}")
    return None, ""


def effective_mass(doc) -> MassFact:
    """The mass to use for this part, and the record of where it came from."""
    from app.services import materials

    geometric = geometric_mass(doc)

    override = getattr(doc, "mass_override_g", None)
    if override is not None and float(override) > 0:
        return MassFact(float(override) / G, OVERRIDE,
                        "measured or datasheet mass recorded on this part",
                        geometric)

    published, basis = catalog_mass(doc)
    if published is not None and published > 0:
        return MassFact(published, CATALOG, basis, geometric)

    if geometric is not None and geometric > 0:
        material = materials.get_material(getattr(doc, "material_id", None))
        return MassFact(geometric, GEOMETRIC,
                        f"solid geometry in {material.name}", None)

    # Say which of the two reasons it is: "assign a material" and "this document
    # has no body" are different next actions.
    derived = getattr(doc, "derived", None)
    if derived is not None and derived.error:
        reason = "the body does not currently evaluate"
    elif derived is None or not derived.volume:
        reason = "this document has no solid geometry"
    else:
        reason = "no material is assigned and no mass was recorded"
    return MassFact(0.0, UNKNOWN, reason, None)


def for_document(doc_id: str) -> MassFact:
    """`effective_mass` by id, evaluating the body first if it has not been."""
    from app.services import document_service

    doc = document_service.get_document(doc_id)
    if doc is None:
        return MassFact(0.0, UNKNOWN, f"no document {doc_id!r}", None)
    document_service.ensure_derived(doc)
    return effective_mass(doc)


def summarize(facts) -> Dict[str, Any]:
    """A rollup of where a report's masses came from.

    A total with a source breakdown beside it is a total somebody can decide
    whether to trust. Without it, one envelope-derived motor in a list of forty
    printed parts is invisible.
    """
    counts: Dict[str, int] = {}
    mass_by_source: Dict[str, float] = {}
    for fact in facts:
        counts[fact.source] = counts.get(fact.source, 0) + 1
        mass_by_source[fact.source] = mass_by_source.get(fact.source, 0.0) + fact.grams
    out: Dict[str, Any] = {
        "by_source": {k: counts[k] for k in SOURCES if k in counts},
        "mass_g_by_source": {k: round(mass_by_source[k], 3)
                             for k in SOURCES if k in mass_by_source},
    }
    if counts.get(UNKNOWN):
        out["warning"] = (
            f"{counts[UNKNOWN]} part(s) contribute no mass because none is "
            f"known — the total is a lower bound, not an estimate")
    return out

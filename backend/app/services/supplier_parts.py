"""Purchased parts that came from a supplier, alongside the ones we generate.

`parts_catalog` builds 210 parts from the published dimensional standards, and
that is the right floor: it always works, it needs no account anywhere, and an
ISO 4762 M4×16 is an ISO 4762 M4×16 whoever makes it. It is also the ceiling.
It cannot give you *this* Igus bearing, *that* Festo cylinder, or the connector
whose footprint decides whether the enclosure lid closes.

The obvious answer is to pull those from a CAD portal. **We looked, and the
portals do not permit it** — not "it would be hard", but the terms say no:

* **3Dfindit / CADENAS.** `robots.txt` disallows this crawler by name and the
  site answers a non-browser client with 403. There is no documented HTTP API
  that returns geometry — the published interfaces are CAD-host plugins and a
  C++ SDK — and downloads are gated behind a login with a 50-a-day fair-use
  cap. The models belong to the manufacturers who publish them.
* **TraceParts.** There *is* a real REST gateway (`cadRequest` → a temporary
  `cadFileUrl`), it takes an API key granted after a review, and its terms are
  explicit: users "may not i) distribute the Data as part of an external
  service, or ii) copy or post the Data on a website, or iii) distribute the
  Data on any storage medium whatsoever, or iv) use the Data with the aim of
  competing with any TraceParts service", and may not "use such … files to
  create catalogs … competing with any TraceParts service". Seeding a library
  from it is the one thing it forbids in four different ways. It also requires
  handing over each downloading user's name, email, company and country.

So the catalog is not seeded from a portal. It grows the only way it lawfully
can: **the person who accepted a portal's terms brings the file.** They fetch
the STEP from 3Dfindit or TraceParts or the manufacturer's own site under their
own account, and this records it as a catalog part of theirs — scoped to their
account, never shared, never re-published. That is a user importing their own
file, which is what the import route has always been for; what is new is that
the result is a catalog row rather than a loose document.

Four things follow, and each is a way a "parts library" goes wrong:

- **The part number is the user's, and it is stated as theirs.** `provenance`
  records where they said it came from. Nothing here validates it against a
  supplier, and nothing here invents one — the same rule the generated catalog
  follows by publishing a search rather than a part number.
- **`geometry` is `exact` or `mesh`, always, on every row.** A supplier STEP
  arrives as a `BrepBodyFeature` with addressable faces; an STL arrives as a
  voxel `ImportFeature` that cannot be filleted, measured exactly or re-exported
  as a solid. Both are legitimate to place and they are not the same thing, and
  a field present on only some rows is one an agent eventually reads wrong.
- **Rows are owner-scoped, and that is a boundary rather than tidiness.** The
  row names a `document_id`, and `assembly_service.add_component` refuses a part
  its owner cannot open — a global catalog of supplier parts would either break
  that check or hand every tenant a handle on one mutable document.
- **The links out are searches.** `SOURCES` deep-links a *query* on each portal,
  which is a hyperlink and not a copy. It is how you get to the file; it is not
  a way to fetch one behind the user's back.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus, urlparse

from app.services import persistence

_LOGGER = logging.getLogger(__name__)

_KIND = "supplier_part"

#: A supplier key is prefixed so it can never collide with a generated one —
#: `insert_standard_part` takes both and must not have to guess.
PREFIX = "supplier:"

#: Where a user says they got the file. Each carries a *search* link, because a
#: search is a hyperlink and a fetch is a copy. `label` is what the row shows.
SOURCES: Dict[str, Dict[str, str]] = {
    "3dfindit": {
        "label": "3Dfindit",
        "search": "https://www.3dfindit.com/en/search?q={q}",
        "note": "CADENAS portal — free account, manufacturer-certified catalogs",
    },
    "traceparts": {
        "label": "TraceParts",
        "search": "https://www.traceparts.com/en/search?SearchAction=Keyword&Keywords={q}",
        "note": "free account; models remain the publisher's property",
    },
    "grabcad": {
        "label": "GrabCAD",
        "search": "https://grabcad.com/library?query={q}",
        "note": "community uploads — check the licence on each model",
    },
    "manufacturer": {
        "label": "Manufacturer",
        "search": "",
        "note": "downloaded from the maker's own site",
    },
    "other": {"label": "Other", "search": "", "note": ""},
}

CATEGORIES = ("fastener", "bearing", "motor", "extrusion", "electronics",
              "pneumatic", "hardware", "other")

_SLUG = re.compile(r"[^a-z0-9]+")


def sources() -> List[dict]:
    """The portals, for a UI offering "go and find one"."""
    return [{"id": key, **value} for key, value in SOURCES.items()]


def search_url(source: str, query: str) -> Optional[str]:
    template = (SOURCES.get(source) or {}).get("search")
    return template.replace("{q}", quote_plus(query)) if template else None


def _safe_link(url: str) -> str:
    """A stored URL that is safe to put in an ``href``, or nothing.

    `source_url` is typed by the importer and comes back out as the row's link.
    `javascript:` and `data:` in an `href` are script execution in whoever
    clicks it — the importer today, and anyone they share the assembly with
    tomorrow. Only http(s) survives; anything else is dropped rather than
    rejected, because a bad link is not a reason to refuse a good part.
    """
    url = (url or "").strip()
    if not url:
        return ""
    scheme = urlparse(url).scheme.lower()
    if scheme in ("http", "https"):
        return url[:2000]
    _LOGGER.info("dropped a supplier link with scheme %r", scheme or "(relative)")
    return ""


def _slug(text: str) -> str:
    return _SLUG.sub("-", (text or "").strip().lower()).strip("-")[:48] or "part"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ------------------------------------------------------------------ storage --

def _row_id(key: str, owner_id: Optional[str]) -> str:
    """Storage id. Owner-qualified, so two accounts may hold the same key."""
    return f"{owner_id or '_'}::{key}"


def for_owner(owner_id: Optional[str]) -> List[dict]:
    """Every supplier part this account has imported, newest first.

    Only this account's: the row names a document, and a document another
    account cannot open is a part they cannot place.
    """
    rows = [r for r in persistence._rows_by_json_field(_KIND, "owner_id", owner_id or "")
            if r.get("owner_id") == (owner_id or "")]
    return sorted(rows, key=lambda r: r.get("created_at", ""), reverse=True)


def by_key(key: str, owner_id: Optional[str]) -> Optional[dict]:
    row = persistence._load(_KIND, _row_id(key, owner_id))
    # Belt and braces: the id already carries the owner, but a row that says it
    # belongs to somebody else must never resolve into an assembly.
    if row is not None and row.get("owner_id") != (owner_id or ""):
        return None
    return row


def forget(key: str, owner_id: Optional[str]) -> bool:
    """Take a part out of the catalog, leaving its document alone.

    Deliberately not a document delete. The part may be sitting in three
    assemblies, and a catalog listing is a shortcut to placing it again — not
    the thing that keeps it alive.
    """
    return persistence._delete(_KIND, _row_id(key, owner_id))


# ------------------------------------------------------------------ import --

def record(
    *,
    document_id: str,
    owner_id: Optional[str],
    designation: str,
    source: str = "other",
    manufacturer: str = "",
    part_number: str = "",
    category: str = "other",
    source_url: str = "",
    notes: str = "",
    geometry: str = "exact",
    size_mm: Optional[Dict[str, float]] = None,
    mass_g: Optional[float] = None,
    mass_source: str = "geometric",
) -> dict:
    """File an already-imported document as a catalog part of this account's.

    `mass_source` says where `mass_g` came from and it is not decoration. The
    import path fills `mass_g` from the *derived* mass of the body it just
    voxelised or read — which is a geometric figure wearing a catalog row's
    clothes. Reading it back as a published mass would report `source: catalog`
    for a number the geometry produced, which is precisely the confusion
    `part_mass` exists to end. Only `declared` is trusted over geometry.
    """
    if source not in SOURCES:
        raise ValueError(f"unknown source {source!r}; one of {', '.join(SOURCES)}")
    if category not in CATEGORIES:
        raise ValueError(f"unknown category {category!r}; one of {', '.join(CATEGORIES)}")
    if geometry not in ("exact", "mesh"):
        raise ValueError("geometry must be 'exact' or 'mesh'")
    source_url = _safe_link(source_url)

    stem = part_number or designation
    key = f"{PREFIX}{_slug(manufacturer)}-{_slug(stem)}" if manufacturer \
        else f"{PREFIX}{_slug(stem)}"
    if by_key(key, owner_id) is not None:
        # A second import of the same part number is a new row, not an
        # overwrite: the first one may already be placed in an assembly and
        # replacing its document under it would move geometry nobody touched.
        key = f"{key}-{uuid.uuid4().hex[:6]}"

    row = {
        "key": key,
        "owner_id": owner_id or "",
        "document_id": document_id,
        "designation": designation,
        "manufacturer": manufacturer,
        "part_number": part_number,
        "category": category,
        "source": source,
        "source_url": source_url,
        "notes": notes,
        "geometry": geometry,
        "size_mm": size_mm or {"x": 0.0, "y": 0.0, "z": 0.0},
        "mass_g": mass_g,
        "mass_source": mass_source if mass_source in ("declared", "geometric")
                       else "geometric",
        "created_at": _now(),
    }
    persistence._save(_KIND, _row_id(key, owner_id), row, owner_id=owner_id)
    _LOGGER.info("filed supplier part %s (%s) for %s", key, source, owner_id)
    return row


def import_file(
    *,
    filename: str,
    data: bytes,
    owner_id: Optional[str],
    designation: str = "",
    source: str = "other",
    manufacturer: str = "",
    part_number: str = "",
    category: str = "other",
    source_url: str = "",
    notes: str = "",
    units: str = "auto",
    resolution: int = 96,
) -> dict:
    """Import a supplier's CAD file and file the result as a catalog part.

    The import itself is `import_service`, unchanged — STEP and IGES come in
    exact through OpenCascade, meshes are voxelized. What is decided here is
    only which of those happened, because that is the difference between a part
    whose faces can be selected and one whose cannot, and the row has to say so
    rather than let somebody find out by trying to fillet it.
    """
    from app.cad_kernel.core.feature_tree import walk_nodes
    from app.services import document_service as docs
    from app.services import import_service

    name = designation.strip() or part_number.strip() or filename.rsplit("/", 1)[-1]
    doc = import_service.import_document(
        filename=filename, data=data, name=name,
        resolution=resolution, units=units, owner_id=owner_id)

    kinds = {n.get("type") for n in walk_nodes(doc.feature_tree)}
    geometry = "exact" if "BrepBodyFeature" in kinds else "mesh"

    bbox = doc.derived.bbox
    size_mm = {"x": 0.0, "y": 0.0, "z": 0.0}
    if bbox:
        size_mm = {a: round((getattr(bbox.max, a) - getattr(bbox.min, a)) * 1000.0, 3)
                   for a in "xyz"}

    row = record(
        document_id=doc.id, owner_id=owner_id, designation=name, source=source,
        manufacturer=manufacturer, part_number=part_number, category=category,
        source_url=source_url, notes=notes, geometry=geometry, size_mm=size_mm,
        mass_g=round(doc.derived.mass * 1000.0, 3) if doc.derived.mass else None)

    # `catalog_ref` is what `standard_parts` reads to find an account's existing
    # document for a key, so a supplier part de-duplicates exactly like a
    # generated one and eight of them are one BOM line.
    doc.catalog_ref = row["key"]
    docs.update_document(doc.id, feature_tree=doc.feature_tree, resolution=resolution)
    return row


# ----------------------------------------------------------------- listing --

def describe(row: dict) -> Dict[str, Any]:
    """One catalog row, in the same shape `parts_catalog.describe` returns.

    Same shape on purpose: the picker and `list_standard_parts` show generated
    and supplier parts in one list, and two differently-shaped rows in one list
    is how a caller ends up inferring which kind it is holding.
    """
    source = SOURCES.get(row.get("source", "other"), SOURCES["other"])
    supplier = " ".join(x for x in (row.get("manufacturer"),
                                    row.get("part_number")) if x)
    return {
        "key": row["key"],
        "category": row.get("category", "other"),
        "designation": row.get("designation", ""),
        # Where the geometry came from stands where a standard's number would.
        "standard": supplier or source["label"],
        "material_id": "",
        # A third value beside as-standard/envelope: this is the supplier's own
        # body, as they published it, not something measured off a table.
        "fidelity": "supplier",
        "geometry": row.get("geometry", "exact"),
        "notes": row.get("notes") or
                 f"imported from {source['label']}"
                 + (f" — {row['source_url']}" if row.get("source_url") else ""),
        "size_mm": row.get("size_mm") or {"x": 0.0, "y": 0.0, "z": 0.0},
        **({"typical_mass_g": row["mass_g"]} if row.get("mass_g") else {}),
        # Re-checked on the way out, not only on the way in: a row written
        # before the scheme check existed is still a row, and this is the field
        # that becomes an `href`.
        "supplier_search": _safe_link(row.get("source_url", ""))
                           or search_url(row.get("source", "other"),
                                         supplier or row.get("designation", ""))
                           or "",
        "source": row.get("source", "other"),
        "manufacturer": row.get("manufacturer", ""),
        "part_number": row.get("part_number", ""),
        "document_id": row.get("document_id"),
        "created_at": row.get("created_at"),
    }

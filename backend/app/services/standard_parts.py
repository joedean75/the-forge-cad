"""Turning a catalog entry into a part document, once per account.

A catalog part becomes an ordinary `DesignDocument` — that is what makes it
measurable, sectionable, exportable and interference-checkable like anything
else. The only question this module answers is *how many* documents.

**One per (catalog key, owner), reused.** Eight M4×16 screws in a frame must be
one document placed eight times, not eight documents. Two reasons, and the
second is the one that bites:

* The BOM groups by `part_id`, so eight documents would print eight lines of
  quantity 1 where a purchasing system wants one line of quantity 8.
* A component is a *reference*. `assembly_service.add_component` refuses a part
  its owner cannot open, so the document has to belong to the account placing
  it — which means the reuse key is (key, owner), not key alone. A shared global
  screw would either break that check or hand every tenant a handle on one
  mutable document.

The document is a normal one in every other way: it appears in the library, it
can be edited (a screw with a wider head is a legitimate thing to want), and
editing it changes every place it is used. That last part is the assembly
contract, not a surprise — and it is why the copy is per account rather than
per assembly.

**A configuration is part of the key, not a second document with the same
name.** Half the RC catalog is parameterised — a LiPo pack, a carbon plate, a
motor whose can is a different diameter — and two packs of different sizes are
two parts. So the reuse key is the *reference*, `parts_catalog.make_ref`, which
is the bare key for the default configuration and the key plus its changed
parameters otherwise. Two consequences, and both are the point: eight packs of
one size are still one document and therefore one BOM line of qty 8, and every
document stored before parameters existed still resolves, because its bare key
is what the default configuration produces.
"""

from __future__ import annotations

import re

import logging
from typing import Optional

from app.services import persistence

_LOGGER = logging.getLogger(__name__)


def _existing(key: str, owner_id: Optional[str]) -> Optional[str]:
    """The document this account already has for this reference, if any.

    `key` here is a `parts_catalog` *reference* — a bare catalog key, or a key
    with the parameters that make it a different part. Matching on the whole
    string is what keeps two differently-sized packs two documents.
    """
    from app.services import document_service as docs

    for doc in docs.DOCUMENTS.values():
        if doc.catalog_ref == key and doc.owner_id == owner_id:
            return doc.id
    for row in persistence._rows_by_json_field("document", "catalog_ref", key):
        if row.get("owner_id") == owner_id:
            return row["id"]
    return None


def listing(owner_id: Optional[str] = None, *, category: Optional[str] = None,
            search: Optional[str] = None, source: Optional[str] = None,
            limit: int = 60, offset: int = 0) -> dict:
    """The whole catalog a caller can place: generated parts plus their own.

    One listing rather than two, because they are the same question — "what can
    I put in this assembly?" — and a picker with two lists makes the user decide
    which tab a bearing is in before they can search for one. The rows share a
    shape (`parts_catalog.describe` / `supplier_parts.describe`) and every row
    carries `fidelity` and `geometry`, so nothing has to be inferred from which
    half it came from.

    Supplier rows come first: an account that has imported six parts wants those
    six near the top, not on page four behind 173 screws.
    """
    from app.services import parts_catalog, supplier_parts, adafruit_parts

    mine = [supplier_parts.describe(r) for r in supplier_parts.for_owner(owner_id)]
    if source:
        mine = [r for r in mine if r["source"] == source]
    generated = [] if source else [parts_catalog.describe(e) for e in parts_catalog.catalog()]

    adafruit = [adafruit_parts.describe(r) for r in adafruit_parts.manifest()["parts"]] if source in (None, "", "adafruit") else []
    rows = mine + generated + adafruit
    if category:
        rows = [r for r in rows if r["category"] == category]
    match = "all"
    if (search or "").strip():
        rows, match = _search(rows, search)

    limit = max(1, min(int(limit or 60), 250))
    counts: dict = {}
    for row in rows:
        counts[row["category"]] = counts.get(row["category"], 0) + 1
    offset = max(0, int(offset))
    page = rows[offset:offset + limit]
    return {"parts": page, "count": len(rows), "offset": offset, "match": match,
            "returned": len(page), "categories": counts,
            "imported": len(mine),
            "sources": [adafruit_parts.SOURCE, *supplier_parts.sources()],
            "source": "generated from the published dimensional standards "
                      "(ISO 4762/4032/7089/2338/273, ISO 15, NEMA frames) and "
                      "from the settled conventions of RC, drone and robotics "
                      "work (the 30.5/20/16 stack patterns, the four-digit "
                      "brushless stator code, the three servo classes), plus "
                      "the supplier files this account imported. What varies "
                      "by manufacturer is a parameter rather than a table row"}


def document_for_key(key: str, owner_id: Optional[str] = None,
                     params: Optional[dict] = None) -> Optional[str]:
    """The document for any catalog key — generated or supplier-imported.

    One entry point because `insert_standard_part` takes one `key` argument and
    must not have to know which half of the catalog it names. A supplier part
    *is* a document already (somebody imported the file), so there is nothing to
    build: the row names it, and it takes no parameters because its geometry is
    a file rather than a builder.
    """
    from app.services import parts_catalog, supplier_parts

    if str(key).startswith("adafruit:"):
        from app.services import adafruit_parts
        if params:
            raise ValueError("Adafruit parts use original manufacturer geometry, without parameters")
        return adafruit_parts.document_for_key(key, owner_id)
    if str(key).startswith(supplier_parts.PREFIX):
        if params:
            raise ValueError("an imported supplier part has no parameters — "
                             "its geometry is the file its maker published")
        row = supplier_parts.by_key(key, owner_id)
        return row["document_id"] if row else None
    entry = parts_catalog.by_key(key)
    return document_for(entry, owner_id, params) if entry else None


def document_for(entry, owner_id: Optional[str] = None,
                 params: Optional[dict] = None) -> str:
    """The id of this account's document for one configuration of an entry.

    Raises `ValueError` for a parameter the entry does not declare or a value
    outside its range — `parts_catalog.resolve_params` decides, and the
    operation layer turns it into a typed `BAD_ARG`.
    """
    from app.services import document_service as docs
    from app.services import parts_catalog

    resolved = parts_catalog.resolve_params(entry, params)
    ref = parts_catalog.make_ref(entry.key, resolved)
    found = _existing(ref, owner_id)
    if found is not None:
        return found

    doc = docs.create_document(
        name=parts_catalog.designation_of(entry, resolved), owner_id=owner_id,
        resolution=96)
    doc.feature_tree = parts_catalog.build_tree(entry, resolved)
    doc.catalog_ref = ref
    doc.material_id = entry.material
    # Provenance is "imported" in the sense that matters here: nobody in this
    # account modelled it. It is not `manual`, and calling it `text_to_cad`
    # would put it in the training corpus as a prompt nobody wrote.
    doc.provenance = "imported"
    docs.update_document(doc.id, feature_tree=doc.feature_tree, resolution=96)
    return doc.id


def category_of(catalog_ref: Optional[str],
                owner_id: Optional[str] = None) -> Optional[str]:
    """The catalog category a document came from, or `None` if it came from none.

    One entry point across both halves for the same reason `document_for_key`
    is: a caller asking "is this a propeller?" must not have to know whether the
    row was generated here or imported by its owner. A supplier row is
    owner-scoped, so the owner has to be passed — the assembly's, which is the
    account whose parts an assembly can hold at all.
    """
    from app.services import parts_catalog, supplier_parts

    ref = (catalog_ref or "").strip()
    if not ref:
        return None
    if ref.startswith("adafruit:"):
        from app.services import adafruit_parts
        row = adafruit_parts.by_key(ref)
        return row["category"] if row else None
    if ref.startswith(supplier_parts.PREFIX):
        row = supplier_parts.by_key(ref, owner_id)
        return row.get("category") if row else None
    entry = parts_catalog.by_key(parts_catalog.parse_ref(ref)[0])
    return entry.category if entry else None


def published_mass_kg(catalog_ref: Optional[str]) -> Optional[float]:
    """The mass the catalog *publishes* for a document, where its own is wrong.

    A catalog part is a solid envelope — that is what `fidelity: envelope` says
    — and for anything with air, windings or a circuit board inside it, the
    envelope's geometric mass is not the part's mass. A NEMA 17 modelled as a
    solid steel block weighs 672 g against a real 350 g, so every BOM total and
    every mass budget containing a motor was nearly double.

    Three rules hold this honest:

    * **The document goes on reporting its own geometric mass.**
      `mass_properties` measures that part's geometry and must not start lying
      about it. The substitution belongs here, at the roll-up, where the
      question is "what does the real thing weigh" rather than "how big is this
      body".
    * **Whoever substitutes says so.** The roll-ups do not call this directly:
      they go through `part_mass.effective_mass`, which ranks this figure
      against a mass recorded on the part and states `mass_source` on every
      row, because a number whose provenance is invisible is one nobody can
      check.
    * **A supplier-imported part is never substituted.** Its mass came from the
      body its manufacturer published, so there is no second figure to prefer —
      `supplier_parts.describe` reports the geometric mass under
      `typical_mass_g` precisely because for those rows the two are the same.

    Returns kilograms, which is what the roll-ups work in, or `None`
    when the document is not a catalog part or the entry publishes no figure.
    """
    from app.services import parts_catalog, supplier_parts

    ref = (catalog_ref or "").strip()
    if not ref or ref.startswith(supplier_parts.PREFIX):
        return None
    grams = parts_catalog.published_mass_g(ref)
    return None if grams is None else float(grams) / 1000.0


_SEARCH_FIELDS = ("key", "designation", "standard", "category", "notes", "name",
                  "manufacturer", "part_number")
#: Where a match counts for more: the row's own name rather than its notes,
#: which mention other sizes in passing ("3650 is 36 mm across" is in the 540's).
_PRIMARY_FIELDS = ("key", "designation", "name", "part_number")
#: What people call a standard part, which the designation does not say:
#: "ISO 4762 M4x4" is a screw to anyone asking for one.
_COMMON_NAMES = {
    "ISO 4762": "socket head cap screw bolt",
    "ISO 4032": "hex nut",
    "ISO 7089": "flat washer",
    "ISO 2338": "dowel pin",
    "ISO 15": "ball bearing",
    "NEMA": "stepper motor",
}


_STOP = {"a", "an", "and", "the", "for", "of", "to", "with", "in", "on", "my", "some"}


def _words(text: str) -> list:
    return [w for w in re.split(r"[^a-z0-9.]+", text.lower())
            if w.strip(".") and w not in _STOP]


def _search(rows: list, search: str):
    """Rows matching every word of `search`, anywhere in the row's text.

    The whole query used to be one substring, so "540 motor" missed "540 can
    motor (brushed)" and "2212 motor" missed "2212 brushless motor" — and the
    copilot logs show eight searches for a car motor that came back empty. Each
    word now matches on its own, in the key, the designation, the standard, the
    category or the notes. A model name such as "A2212" also matches on its
    number, because that is the part of it the catalog carries.

    When nothing matches every word, the rows matching the most words come back
    and `match` says `partial`, so an empty page is reserved for a query that
    matched nothing at all.
    """
    terms = _words(search)
    if not terms:
        return rows, "all"
    # The exact phrase in a row's own name is the strongest answer there is:
    # "NEMA 17" means the NEMA 17, not every row mentioning a NEMA and a 17.
    phrase = " ".join(search.lower().split())
    named = [r for r in rows
             if phrase in " ".join(str(r.get(f) or "") for f in _PRIMARY_FIELDS).lower()]
    if named:
        return named, "all"
    def found(term, hay):
        digits = re.sub(r"^[a-z]+", "", term) if re.match(r"^[a-z]+\d{3,}", term) else ""
        return term in hay or bool(digits and digits in hay)

    scored = []
    for row in rows:
        standard = str(row.get("standard") or "")
        common = " ".join(v for k, v in _COMMON_NAMES.items() if standard.startswith(k))
        hay = " ".join([*(str(row.get(f) or "") for f in _SEARCH_FIELDS), common]).lower()
        primary = " ".join(str(row.get(f) or "") for f in _PRIMARY_FIELDS).lower()
        hits = sum(found(t, hay) for t in terms)
        if hits:
            scored.append((hits, sum(found(t, primary) for t in terms), row))
    # Stable within a rank, so the catalog's own order (M4x4 before M4x5)
    # survives among equally good matches.
    scored.sort(key=lambda x: (-x[0], -x[1]))
    full = [row for hits, _, row in scored if hits == len(terms)]
    if full:
        return full, "all"
    if not scored:
        return [], "none"
    best = scored[0][0]
    return [row for hits, _, row in scored if hits == best], "partial"

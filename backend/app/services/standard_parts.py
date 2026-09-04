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
            limit: int = 60) -> dict:
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
    from app.services import parts_catalog, supplier_parts

    mine = [supplier_parts.describe(r) for r in supplier_parts.for_owner(owner_id)]
    if source:
        mine = [r for r in mine if r["source"] == source]
    generated = [] if source else [parts_catalog.describe(e) for e in parts_catalog.catalog()]

    rows = mine + generated
    if category:
        rows = [r for r in rows if r["category"] == category]
    needle = (search or "").strip().lower()
    if needle:
        rows = [r for r in rows
                if needle in r["designation"].lower()
                or needle in r["key"].lower()
                or needle in str(r.get("standard", "")).lower()]

    limit = max(1, min(int(limit or 60), 250))
    counts: dict = {}
    for row in rows:
        counts[row["category"]] = counts.get(row["category"], 0) + 1
    return {"parts": rows[:limit], "count": len(rows),
            "returned": min(len(rows), limit), "categories": counts,
            "imported": len(mine),
            "sources": supplier_parts.sources(),
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
    * **Whoever substitutes says so.** Both callers carry `mass_source` on the
      row, and the modelled figure beside it, because a number whose provenance
      is invisible is one nobody can check.
    * **A supplier-imported part is never substituted.** Its mass came from the
      body its manufacturer published, so there is no second figure to prefer —
      `supplier_parts.describe` reports the geometric mass under
      `typical_mass_g` precisely because for those rows the two are the same.

    Returns kilograms, which is what both callers already work in, or `None`
    when the document is not a catalog part or the entry publishes no figure.
    """
    from app.services import parts_catalog, supplier_parts

    ref = (catalog_ref or "").strip()
    if not ref or ref.startswith(supplier_parts.PREFIX):
        return None
    grams = parts_catalog.published_mass_g(ref)
    return None if grams is None else float(grams) / 1000.0

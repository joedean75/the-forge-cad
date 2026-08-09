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
"""

from __future__ import annotations

import logging
from typing import Optional

from app.services import persistence

_LOGGER = logging.getLogger(__name__)


def _existing(key: str, owner_id: Optional[str]) -> Optional[str]:
    """The document this account already has for `key`, if any."""
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
                      "(ISO 4762/4032/7089/2338/273, ISO 15, NEMA frames), plus "
                      "the supplier files this account imported"}


def document_for_key(key: str, owner_id: Optional[str] = None) -> Optional[str]:
    """The document for any catalog key — generated or supplier-imported.

    One entry point because `insert_standard_part` takes one `key` argument and
    must not have to know which half of the catalog it names. A supplier part
    *is* a document already (somebody imported the file), so there is nothing to
    build: the row names it. A generated one is built on first use.
    """
    from app.services import parts_catalog, supplier_parts

    if str(key).startswith(supplier_parts.PREFIX):
        row = supplier_parts.by_key(key, owner_id)
        return row["document_id"] if row else None
    entry = parts_catalog.by_key(key)
    return document_for(entry, owner_id) if entry else None


def document_for(entry, owner_id: Optional[str] = None) -> str:
    """The id of this account's document for a catalog entry, making it if needed."""
    from app.services import document_service as docs

    found = _existing(entry.key, owner_id)
    if found is not None:
        return found

    doc = docs.create_document(name=entry.designation, owner_id=owner_id,
                               resolution=96)
    doc.feature_tree = entry.build()
    doc.catalog_ref = entry.key
    doc.material_id = entry.material
    # Provenance is "imported" in the sense that matters here: nobody in this
    # account modelled it. It is not `manual`, and calling it `text_to_cad`
    # would put it in the training corpus as a prompt nobody wrote.
    doc.provenance = "imported"
    docs.update_document(doc.id, feature_tree=doc.feature_tree, resolution=96)
    return doc.id

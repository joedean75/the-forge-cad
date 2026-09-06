"""Which inputs produced a result, and when a result stops being about the part.

Everything downstream of the feature tree — the mesh, the FEA solve, the DFM
report, the toolpath, the spec score — is a *reading of a body*. The body moves
on every edit. So the only honest way to hold one of those readings is next to a
fingerprint of what produced it, and the only honest way to show one is to say
whether that fingerprint still matches the document.

Before this, staleness was handled twice and neither was complete. The frontend
store cleared its analysis panels on every mutation (`STALE`), which is right for
the tab that made the edit and useless after a reload or for a second viewer.
And `derived.eval_key` fingerprinted the *geometry* only, so a change of material
— which moves mass, stress and cost and nothing else — invalidated nothing at all.

Three things live here, and they belong together because they are the same idea
at three scales:

- **Fingerprints.** `geometry_fingerprint` is the old `_evaluation_key`, moved
  intact. `analysis_fingerprint` extends it per product with the inputs that
  product actually reads — material and load cases for a solve, process and
  build axis for DFM, tool and stock for CAM. The table is explicit
  (`PRODUCTS`), because "which edit invalidates which result" is a design
  decision and a reader should be able to see it in one place rather than infer
  it from five call sites.
- **The caches.** Solids, exact bodies, encoded meshes and thumbnails, moved
  from `document_service` so their keys and their invalidation are decided in
  one file. `document_service` keeps module-level aliases, so every existing
  caller (and every test that reaches into them) still works on the same object.
- **Publication.** `publish` refuses a result whose fingerprint no longer
  matches the document. That is the rule STATE-02 asks for and it cannot be left
  to callers: a job that takes ninety seconds finishes into a document that has
  been edited twice, and the natural code writes its answer anyway.

**Revision and fingerprint are different questions, deliberately.** A revision
counts *writes* — it moves when the part is renamed — and it is what an
optimistic write checks against, because two clients editing one document need
to know that they saw the same thing. A fingerprint hashes *inputs* — renaming
does not touch it — and it is what a cached result checks against. Conflating
them makes either a rename recompute the geometry or a lost update invisible.
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from typing import Any, Callable, Dict, Optional, Tuple


# ---------------------------------------------------------------------------
# Caches
# ---------------------------------------------------------------------------

class LruCache(dict):
    """A dict that forgets its least-recently-used entry past `capacity`.

    Subclassing dict keeps `in`, `[]` and `.pop(k, default)` working for the
    existing call sites (and the tests that reach into them) rather than forcing
    an interface change through the codebase.

    Bounded by *count* here; `CACHE-03` adds the byte budget, which needs a
    sizer per cache because an evaluated solid's memory is a mesh and an OCC
    shape's is not introspectable from Python at all.
    """

    def __init__(self, capacity: int):
        super().__init__()
        self._capacity = capacity
        self._order: "OrderedDict[str, None]" = OrderedDict()
        self.evictions = 0

    def __getitem__(self, key):
        value = super().__getitem__(key)
        self._order.move_to_end(key)
        return value

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self._order[key] = None
        self._order.move_to_end(key)
        while len(self._order) > self._capacity:
            oldest, _ = self._order.popitem(last=False)
            super().pop(oldest, None)
            self.evictions += 1

    def pop(self, key, *default):
        self._order.pop(key, None)
        return super().pop(key, *default)

    def clear(self) -> None:
        self._order.clear()
        super().clear()


SOLID_CAPACITY = 64
BREP_CAPACITY = 64
MESH_CAPACITY = 48
THUMBNAIL_CAPACITY = 256

#: Evaluated solids, keyed by document id.
SOLIDS: Dict[str, Any] = LruCache(SOLID_CAPACITY)

#: Exact bodies (shape, entity tags, reference verdicts), keyed by the hash of
#: the *resolved* tree rather than the document id. A stale exact body is far
#: nastier than a slow one: the geometry silently stops agreeing with the
#: document, and every topology-addressing operation reads it.
BREPS: Dict[str, Any] = LruCache(BREP_CAPACITY)

#: Encoded meshes and rendered thumbnails, keyed by `(doc_id, content hash)`.
#: A *superseded* hash stays reachable while it is remembered, which is the
#: whole reason these are caches rather than a single current value: a client
#: holding the older URL is holding a correct one. Thumbnails are smaller and
#: longer-lived than meshes because a library view asks for a dozen at once and
#: asks again on every visit.
MESHES: Dict[str, bytes] = LruCache(MESH_CAPACITY)
THUMBNAILS: Dict[str, bytes] = LruCache(THUMBNAIL_CAPACITY)


def _keyed(doc_id: str, digest: str) -> str:
    return f"{doc_id}:{digest}"


def cache_mesh(doc_id: str, digest: str, payload: bytes) -> None:
    MESHES[_keyed(doc_id, digest)] = payload


def cached_mesh(doc_id: str, digest: str) -> Optional[bytes]:
    return MESHES.get(_keyed(doc_id, digest))


def cache_thumbnail(doc_id: str, digest: str, payload: bytes) -> None:
    THUMBNAILS[_keyed(doc_id, digest)] = payload


def cached_thumbnail(doc_id: str, digest: str) -> Optional[bytes]:
    return THUMBNAILS.get(_keyed(doc_id, digest))


def forget_document(doc_id: str) -> None:
    """Drop everything cached *by document id* when the document goes away.

    Not the exact-body cache: it is keyed by resolved-tree hash, which is shared
    across documents on purpose (a released revision evaluated as an ephemeral
    document is the same tree), and entry by entry it has no idea whose it is.
    It is bounded and self-evicting, which is what makes that safe.
    """
    SOLIDS.pop(doc_id, None)
    for cache in (MESHES, THUMBNAILS):
        for key in [k for k in list(cache) if k.startswith(f"{doc_id}:")]:
            cache.pop(key, None)


def stats() -> Dict[str, Dict[str, int]]:
    """Entry counts and eviction totals, for the benchmark report."""
    return {
        name: {"entries": len(cache), "capacity": cache._capacity,
               "evictions": cache.evictions}
        for name, cache in (("solids", SOLIDS), ("breps", BREPS),
                            ("meshes", MESHES), ("thumbnails", THUMBNAILS))
    }


# ---------------------------------------------------------------------------
# Fingerprints
# ---------------------------------------------------------------------------

def _digest(parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()


def _dumped(models, drop: Tuple[str, ...] = ()) -> str:
    """The whole model, minus named outputs.

    Listing the fields to *include* is how a later schema addition silently
    stops being part of the key, so this inverts it: everything counts unless it
    is provably derived.
    """
    return json.dumps(
        [{k: v for k, v in m.model_dump().items() if k not in drop}
         for m in (models or [])],
        sort_keys=True, default=str)


def tree_hash(tree: dict) -> str:
    return hashlib.sha256(json.dumps(tree, sort_keys=True, default=str).encode()).hexdigest()


def geometry_fingerprint(doc, resolution: int) -> str:
    """Everything that changes what an evaluation would produce.

    Not just the feature tree: a parameter value, the active configuration and
    the resolution all move the body without touching a single node. Anything
    missing here becomes a stale mesh served under a fresh hash, which is the
    one failure a cache must not have — so this is deliberately generous, and
    excludes only what provably cannot change geometry (the material, the name,
    the spec, the revision counter).
    """
    return _digest([
        tree_hash(doc.feature_tree),
        resolution,
        doc.active_configuration or "",
        # `value` is the *resolved* number the caller is about to write back, not
        # an input — including it would invalidate the key on the very
        # evaluation that produced it.
        _dumped(doc.parameters, drop=("value",)),
        _dumped(doc.configurations),
    ])


def _material(doc) -> str:
    return doc.material_id or ""


def _spec(doc) -> str:
    return json.dumps(doc.spec or {}, sort_keys=True, default=str)


class Product:
    """One derived product, and the document state it is a reading of.

    `extra` names the request-time settings that belong in the fingerprint —
    the element budget of a solve, the process of a DFM run, the tool of a
    toolpath. They are arguments rather than document fields, so they cannot be
    read off the document and have to be passed in; a product whose fingerprint
    ignored them would report a milling analysis as current for an additive one.
    """

    def __init__(self, kind: str, document_inputs: Tuple[Callable[[Any], str], ...],
                 extra: Tuple[str, ...], description: str):
        self.kind = kind
        self.document_inputs = document_inputs
        self.extra = extra
        self.description = description


#: Which document state each derived product reads, and which call arguments.
#:
#: This is the invalidation matrix, written once. Every entry starts from the
#: geometry fingerprint — nothing downstream survives the body moving — and adds
#: what else it reads. `analysis` reads the material because stress is a
#: property of the material as much as of the shape; `dfm` does not, because
#: none of the topology measurements it makes (wall thickness, draft, tool
#: access) depends on what the part is made of.
PRODUCTS: Dict[str, Product] = {
    "analysis": Product(
        "analysis", (_material,), ("elements", "load_cases", "solver"),
        "linear-static FEA: stress, displacement, safety factor"),
    "dfm": Product(
        "dfm", (), ("process", "build_axis", "resolution"),
        "manufacturability measurements for one process"),
    "cam": Product(
        "cam", (), ("machine", "tool", "stock"),
        "a toolpath and the G-code written from it"),
    "spec": Product(
        "spec", (_material, _spec), (),
        "the score against the document's requirements"),
    "drawing": Product(
        "drawing", (), ("spec",),
        "the projected drawing sheet"),
}


def analysis_fingerprint(doc, kind: str, resolution: int,
                         settings: Optional[dict] = None) -> str:
    """The fingerprint a derived product of `kind` should be published under.

    Unknown kinds raise rather than defaulting to the geometry fingerprint. A
    product silently fingerprinted on geometry alone looks current after the
    input it actually depends on changes, which is precisely the failure this
    module exists to make impossible.
    """
    product = PRODUCTS.get(kind)
    if product is None:
        raise KeyError(f"no derived product {kind!r}; known: {', '.join(sorted(PRODUCTS))}")
    settings = settings or {}
    parts = [kind, geometry_fingerprint(doc, resolution)]
    parts.extend(read(doc) for read in product.document_inputs)
    parts.extend(
        json.dumps(settings.get(name), sort_keys=True, default=str)
        for name in product.extra)
    return _digest(parts)


# ---------------------------------------------------------------------------
# Publication
# ---------------------------------------------------------------------------

#: A published result carries no payload larger than this. A drawing sheet or a
#: G-code program is an *artifact* — it has a URL and a content hash — and
#: inlining one into the document is the mistake P1 undid for the mesh.
MAX_RESULT_BYTES = 256 * 1024


def publish(doc, kind: str, result: dict, *, resolution: int,
            settings: Optional[dict] = None,
            fingerprint: Optional[str] = None,
            computed_at: Optional[str] = None) -> bool:
    """Record a result against the document, unless it is already obsolete.

    `fingerprint` is what the caller computed **before** doing the work. When it
    no longer matches what the document fingerprints to now, the inputs moved
    while the work ran and the result is refused — that is STATE-02, and it
    cannot be left to callers: a job that takes ninety seconds finishes into a
    document that has been edited twice, and the natural code writes its answer
    anyway.

    Returns whether it was kept. A refusal is not an error. The result is a
    correct reading of a body that is no longer the current one, and the caller
    may still show it to whoever asked — labelled as being about an older
    revision, which is exactly what `freshness` reports.
    """
    from app.models.schemas import AnalysisProduct

    current = analysis_fingerprint(doc, kind, resolution, settings)
    if fingerprint is not None and fingerprint != current:
        return False
    inline = result if len(json.dumps(result, default=str)) <= MAX_RESULT_BYTES else {}
    doc.analysis[kind] = AnalysisProduct(
        kind=kind,
        fingerprint=current,
        computed_at=computed_at or _now(),
        result=inline,
        truncated=not inline and bool(result),
    )
    return True


def freshness(doc, kind: str, resolution: int,
              settings: Optional[dict] = None) -> str:
    """`current` | `stale` | `missing` for one product on one document.

    `stale` covers both halves of the same question — the body moved, or the
    settings this reading was taken under are not the ones now selected. They
    are the same answer to the caller ("this is not about what you are looking
    at"), and splitting them would invite a UI that treats one of them as fine.
    """
    held = (doc.analysis or {}).get(kind)
    if held is None:
        return "missing"
    return ("current"
            if held.fingerprint == analysis_fingerprint(doc, kind, resolution, settings)
            else "stale")


def held(doc, kind: str):
    """The stored product of that kind, whether or not it is current."""
    return (doc.analysis or {}).get(kind)


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()

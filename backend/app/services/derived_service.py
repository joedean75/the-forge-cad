"""Resolving a derived reference, and the dependency graph around it.

The feature is `cad_kernel/ops/derived.py`; this is the half that knows what a
document is. It is not in `feature_ops` for the reason `services/datum` is not:
`feature_ops` is tree surgery, and making it the place that loads documents
would give every caller holding a bare tree a datastore dependency.

**The graft is ordinary features.** A resolved `DerivedFeature` is replaced by
the source's own nodes, so both kernels, the mesher, DFM, naming and the
decompiler all go on seeing shapes they already know. Nothing here needed a new
evaluator entry, and a derived body is exact on the exact kernel for exactly the
same reasons the source's own body is.

**Ids are prefixed, deterministically.** Two documents mint ids from their own
allocators, so `f3` means a different thing in each and splicing without
renaming would collide. `{derived_id}~{source_id}` is stable across rebuilds —
which trajectory replay and the decompiler's mesh comparison both rest on — and
it can be read backwards, which is what lets a picked triangle name the derived
feature that is actually in this document's tree.
"""

from __future__ import annotations

import copy
from typing import Any, List, Optional, Sequence, Tuple

from app.cad_kernel.core.feature_tree import node_children, walk_nodes
from app.cad_kernel.ops.derived import GRAFT_SEP
from app.services.feature_ops import DERIVED_TYPE, SKETCH_TYPE, has_solid, is_bodyless


class DerivedError(ValueError):
    """A derived reference could not be read. The message says which and why."""


class DerivedCycle(DerivedError):
    """Following the reference returns to a document already being read."""


# ------------------------------------------------------------- the graph --

def references(tree: Optional[dict]) -> List[dict]:
    """Every `DerivedFeature` node in a serialized tree."""
    return [n for n in walk_nodes(tree or {}) if n.get("type") == DERIVED_TYPE]


def dependencies(tree: Optional[dict]) -> List[Tuple[str, Optional[str]]]:
    """The `(document_id, revision)` pairs a tree reads, deduplicated.

    The revision is part of the identity rather than a detail of it: a pinned
    reference reads an immutable snapshot, so it is *not* invalidated when the
    source moves, and folding the two together here is what lets the evaluation
    key say so.
    """
    seen: set = set()
    out: List[Tuple[str, Optional[str]]] = []
    for node in references(tree):
        params = node.get("params") or {}
        pair = (params.get("document_id") or "", params.get("revision") or None)
        if pair[0] and pair not in seen:
            seen.add(pair)
            out.append(pair)
    return out


def source_feature(feature_id: Optional[str]) -> Optional[str]:
    """The derived feature a grafted id belongs to, or None if it is not one.

    A grafted node's id names a feature of *another* document, which this
    document's tree does not contain — so a triangle attributed to one would
    select nothing. Read back to the prefix and it selects the reference that
    brought it in, which is the feature a user can actually edit.
    """
    if not feature_id or GRAFT_SEP not in feature_id:
        return None
    return feature_id.split(GRAFT_SEP, 1)[0]


# --------------------------------------------------------- the resolution --

def resolve(tree: dict, *, owner_id: Optional[str] = None,
            chain: Sequence[str] = ()) -> dict:
    """Replace every derived reference in `tree` with the geometry it names.

    Mutates the tree it is given, which is safe because the only caller is the
    one stage of `feature_ops.resolve_for_evaluation`, working on a copy it made
    itself. `chain` is the documents already being resolved, outermost first —
    the check that makes an infinite recursion impossible rather than unlikely,
    since the up-front check in `add_derived` can always be defeated by editing
    the *other* document afterwards.
    """
    if not references(tree):
        return tree

    def rewrite(node: dict) -> dict:
        if node.get("type") == DERIVED_TYPE:
            return graft(node, owner_id=owner_id, chain=chain)
        kids = node_children(node)
        if kids:
            node.setdefault("params", {})["children"] = [rewrite(k) for k in kids]
        return node

    root = tree.get("root")
    if isinstance(root, dict):
        tree["root"] = rewrite(root)
    return tree


def _label(node_id: str) -> str:
    return ("this reference" if node_id == "this reference"
            else f"derived feature {node_id}")


def graft(node: dict, *, owner_id: Optional[str] = None,
          chain: Sequence[str] = ()) -> dict:
    """The nodes one derived reference stands for."""
    from app.services import document_service

    params = node.get("params") or {}
    # Named by id once it has one. `add_derived` resolves the reference *before*
    # it is written, so there is nothing to roll back when it fails — and at
    # that point the allocator has not assigned an id, so the message says what
    # it is rather than naming a feature that does not exist yet.
    node_id = node.get("id") or "this reference"
    doc_id = params.get("document_id") or ""
    kind = params.get("kind") or "body"
    entities = [e for e in (params.get("entities") or []) if e]

    if not doc_id:
        raise DerivedError(f"{_label(node_id)} names no source document")
    if doc_id in tuple(chain):
        raise DerivedCycle(
            f"{_label(node_id)} would read {doc_id}, which is already "
            f"being read: {' -> '.join(tuple(chain) + (doc_id,))}. A document "
            f"cannot derive from itself, directly or round a loop.")

    source = document_service.get_document(doc_id)
    if source is None:
        raise DerivedError(
            f"{_label(node_id)} names document {doc_id!r}, which no "
            f"longer exists — delete the reference or point it at another part")
    # The same rule `assembly_service.add_component` applies to a part: a
    # reference reads the source's geometry straight back out, so a document
    # this one's owner cannot open is one it cannot derive from. Checked here
    # rather than at the caller because it is a question about two stored
    # documents and needs no caller identity — and because resolution is the
    # moment the geometry would actually be read.
    if owner_id and not document_service.can_access(source, owner_id):
        raise DerivedError(
            f"{_label(node_id)} reads document {doc_id!r}, which this "
            f"document's owner can no longer open")

    revision = params.get("revision")
    if revision:
        rev = next((r for r in source.revisions if r.letter == revision), None)
        if rev is None:
            raise DerivedError(
                f"{_label(node_id)} is pinned to revision {revision!r} "
                f"of {doc_id!r}, which has no such revision")
        source = source.model_copy(update={"feature_tree": rev.feature_tree})

    # A source with no body is not something to splice a stand-in for. The
    # evaluator needs *a* solid when a tree prunes to nothing, so grafting an
    # empty document would put the 1 mm placeholder in this document's viewport
    # and report it as the part — the confident little cube three datum planes
    # replaced.
    if kind == "body" and not has_solid(source.feature_tree):
        raise DerivedError(
            f"{_label(node_id)} reads document {doc_id!r}, which has no "
            f"body yet — build one there first, or derive a sketch instead")

    graft_tree = document_service.graft_tree(source, chain=tuple(chain) + (doc_id,))
    graft_tree = _prefixed(graft_tree, node_id)
    if kind == "sketch":
        return _pick_sketch(graft_tree, node, node_id, entities)
    return _pick_body(graft_tree, node, node_id, entities)


def _prefixed(tree: dict, prefix: str) -> dict:
    """Rename every node in a grafted tree, and every reference to one.

    Two passes rather than one, because a reference can point backwards as
    easily as forwards: the ids are collected first, then every string in every
    node's params that *is* one of them is rewritten. A reference left pointing
    at the source's id would resolve against this document's tree, where that id
    either means nothing or — worse — means somebody else's feature.
    """
    ids = {n["id"] for n in walk_nodes(tree) if n.get("id")}
    renamed = {old: f"{prefix}{GRAFT_SEP}{old}" for old in ids}

    def rewrite(value: Any) -> Any:
        if isinstance(value, str):
            return renamed.get(value, value)
        if isinstance(value, list):
            return [rewrite(v) for v in value]
        if isinstance(value, dict):
            return {k: rewrite(v) for k, v in value.items()}
        return value

    for node in walk_nodes(tree):
        if node.get("id") in renamed:
            node["id"] = renamed[node["id"]]
        params = node.get("params") or {}
        for key, value in list(params.items()):
            if key == "children":
                continue
            params[key] = rewrite(value)
    return tree


def _named(node: dict, source: dict) -> dict:
    """Carry the reference's own name onto the geometry it stands for."""
    name = source.get("name")
    if name:
        node["name"] = name
    return node


def _pick_body(tree: dict, node: dict, node_id: str,
               entities: List[str]) -> dict:
    root = tree.get("root")
    if not isinstance(root, dict):
        raise DerivedError(
            f"{_label(node_id)} reads a document with no body yet")
    if not entities:
        return _named(root, node)

    found = {n["id"]: n for n in walk_nodes(tree) if n.get("id")}
    picked = []
    for entity in entities:
        grafted = f"{node_id}{GRAFT_SEP}{entity}"
        if grafted not in found:
            raise DerivedError(
                f"{_label(node_id)} names feature {entity!r}, which is "
                f"not in the source document (it may have been deleted, "
                f"suppressed, or consumed by a boolean)")
        picked.append(found[grafted])
    if all(is_bodyless(p) for p in picked):
        # Splicing a sketch in as a body means it is pruned before evaluation
        # and the boolean above it collapses — a `kind: "body"` reference that
        # silently contributes nothing. `kind: "sketch"` is how a sketch is
        # brought across, and it is a different graft.
        raise DerivedError(
            f"{_label(node_id)} names only features that make no "
            f"material — use kind='sketch' to bring a sketch across")
    if len(picked) == 1:
        return _named(picked[0], node)
    # Several named features are one body, so they are unioned rather than
    # returned as a list: a graft has to be a single node, because it replaces
    # a single node.
    return {"type": "BooleanFeature", "id": f"{node_id}{GRAFT_SEP}union",
            "name": node.get("name") or "Derived",
            "params": {"operation": "union", "children": picked}}


def _pick_sketch(tree: dict, node: dict, node_id: str,
                 entities: List[str]) -> dict:
    sketches = [n for n in walk_nodes(tree) if n.get("type") == SKETCH_TYPE]
    if entities:
        wanted = f"{node_id}{GRAFT_SEP}{entities[0]}"
        picked = next((s for s in sketches if s.get("id") == wanted), None)
        if picked is None:
            raise DerivedError(
                f"{_label(node_id)} names sketch {entities[0]!r}, which "
                f"is not a sketch in the source document")
    elif len(sketches) == 1:
        picked = sketches[0]
    elif not sketches:
        raise DerivedError(
            f"{_label(node_id)} asks for a sketch and the source "
            f"document has none")
    else:
        # Guessing would be right most of the time, which is the worst possible
        # accuracy for a reference that decides what the part is.
        raise DerivedError(
            f"{_label(node_id)} must name which sketch: the source has "
            f"{len(sketches)} of them")
    # The consumers of this sketch live in *this* document and name the derived
    # feature, not the sketch it stands for — so the graft takes the reference's
    # own id. Every other grafted node keeps its prefixed one.
    picked = copy.deepcopy(picked)
    picked["id"] = node_id
    return _named(picked, node)


# ------------------------------------------------------- what points at me --

def dependents(document_id: str) -> List[dict]:
    """Documents whose stored tree derives from this one.

    Two stages on purpose. The SQL pass is a substring filter over the stored
    JSON — cheap, index-free but done in the database, and crucially it does not
    deserialize a feature tree per row. It over-approximates by construction, so
    every candidate is then confirmed by actually walking its tree; a document
    id is a uuid, so in practice nothing survives the first stage that fails the
    second, but "in practice" is not a filter.
    """
    from app.services import persistence

    out: List[dict] = []
    for data in persistence.documents_mentioning(document_id):
        tree = data.get("feature_tree") or {}
        pairs = [p for p in dependencies(tree) if p[0] == document_id]
        if not pairs:
            continue
        out.append({
            "document_id": data.get("id"),
            "name": data.get("name"),
            "owner_id": data.get("owner_id"),
            "references": [{"revision": revision} for _, revision in pairs],
        })
    return out


def status(doc) -> dict:
    """Every reference this document makes and whether it can still be read.

    The interesting answer is the failing one. A reference that resolves is
    invisible — the body is simply right — but a source that was deleted, had
    the named feature removed, or was un-shared leaves a document that still
    opens and no longer means what it says, and the only place that is visible
    is here.
    """
    from app.services import document_service

    rows: List[dict] = []
    for node in references(doc.feature_tree):
        params = node.get("params") or {}
        row = {
            "feature_id": node.get("id"),
            "name": node.get("name"),
            "document_id": params.get("document_id"),
            "revision": params.get("revision"),
            "kind": params.get("kind") or "body",
            "entities": list(params.get("entities") or []),
            "resolves": True,
            "reason": None,
        }
        source = document_service.get_document(row["document_id"] or "")
        row["source_name"] = source.name if source else None
        try:
            graft(node, owner_id=doc.owner_id, chain=(doc.id,))
        except DerivedError as exc:
            row["resolves"] = False
            row["reason"] = str(exc)
        rows.append(row)
    return {"derives_from": rows, "used_by": dependents(doc.id)}


def path_back(doc_id: str, source_id: str) -> Optional[List[str]]:
    """The loop, if deriving `doc_id` from `source_id` would close one.

    Walked forwards from the *source*: a cycle exists exactly when the source
    already reads this document, at any depth. Returned as the path rather than
    a boolean because "that would be circular" is not something a user can act
    on and "A -> B -> C -> A" is.
    """
    from app.services import document_service

    if source_id == doc_id:
        return [doc_id, doc_id]
    seen = {source_id}
    queue: List[List[str]] = [[source_id]]
    while queue:
        path = queue.pop(0)
        source = document_service.get_document(path[-1])
        if source is None:
            continue
        for next_id, _ in dependencies(source.feature_tree):
            if next_id == doc_id:
                return [doc_id] + path + [doc_id]
            if next_id not in seen:
                seen.add(next_id)
                queue.append(path + [next_id])
    return None

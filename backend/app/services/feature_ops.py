"""Feature-tree manipulation in Python (backend equivalent of util/featureTree.ts).

Pure dict transforms on the serialized tree ({version, root}); used by the
operation API so an agent can mutate geometry with discrete calls.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional

from app.cad_kernel.core.feature_tree import node_children, walk_nodes

_BOOLEAN_TYPES = {"BooleanFeature", "FilletFeature", "ChamferFeature"}

_ID_PREFIX = "f"


class IdAllocator:
    """Hands out feature ids that depend only on the tree, never on the clock.

    Ids used to be ``f{wall_clock_ms:x}{process_counter}``, which meant replaying
    the same operations against the same starting tree produced *different* ids
    every run. Anything that records or replays an op sequence — trajectory
    export, the tree decompiler, a regression fixture — was therefore
    unreproducible. Allocating the lowest unused ``f<n>`` for the tree in hand
    makes a replay byte-identical to the original.

    Legacy ids (timestamp-shaped, or uuid4s from older documents) are kept in the
    used-set as opaque strings, so a freshly allocated id can never collide with
    one already persisted.
    """

    def __init__(self, tree: Optional[Dict[str, Any]] = None):
        self._used: set = set()
        for node in walk_nodes(tree or {}):
            node_id = node.get("id")
            if node_id:
                self._used.add(str(node_id))
        self._next = 1

    def alloc(self) -> str:
        while True:
            candidate = f"{_ID_PREFIX}{self._next}"
            self._next += 1
            if candidate not in self._used:
                self._used.add(candidate)
                return candidate

    def ensure(self, node: Dict[str, Any]) -> str:
        """Give ``node`` an id if it lacks one; return whatever id it now has.

        Deliberately not ``setdefault(…, alloc())`` — that evaluates the
        allocation even when the node already has an id, burning a number and
        shifting every later id in the tree.
        """
        if not node.get("id"):
            node["id"] = self.alloc()
        else:
            self._used.add(str(node["id"]))
        return node["id"]


def next_id(tree: Optional[Dict[str, Any]] = None) -> str:
    """One deterministic id for ``tree``. Use IdAllocator when minting several."""
    return IdAllocator(tree).alloc()


# Tree-shape helpers live in the kernel so every layer walks a tree the same
# way; re-exported here because this module is where the service layer looks.
children_of = node_children


def is_boolean(node: Dict[str, Any]) -> bool:
    return node.get("type") in _BOOLEAN_TYPES


def flatten(tree: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every node, parents before children."""
    return list(walk_nodes(tree))


def find_node(tree: Dict[str, Any], node_id: str) -> Optional[Dict[str, Any]]:
    for n in flatten(tree):
        if n.get("id") == node_id:
            return n
    return None


def set_params(tree: Dict[str, Any], node_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
    t = copy.deepcopy(tree)

    def walk(n: Dict[str, Any]) -> bool:
        if n.get("id") == node_id:
            n.setdefault("params", {}).update(params)
            return True
        return any(walk(c) for c in children_of(n))

    if not (t.get("root") and walk(t["root"])):
        raise KeyError(f"feature {node_id} not found")
    return t


def add_primitive(tree: Dict[str, Any], primitive: Dict[str, Any], operation: str = "union",
                  target_id: str | None = None) -> Dict[str, Any]:
    """Combine a new body with the part — the whole of it, or one feature of it.

    Without ``target_id`` the primitive joins the root, which is the ordinary
    "add a boss" / "cut a hole" edit. With it, the primitive is combined with
    *that feature alone*, in place — the same "act on one feature rather than the
    body" idiom `add_modifier` already has.

    That is not a convenience. ``union(plate, subtract(boss, bore))`` — a boss
    with a hole through it, where the hole must not also cut the plate — cannot
    be expressed by appending to the root at all, and it is a shape whole-tree
    generation produces routinely. Until this existed those documents were
    unrecoverable by `decompiler`, and an agent could not build one.
    """
    t = copy.deepcopy(tree)
    root = t["root"]
    ids = IdAllocator(t)
    ids.ensure(primitive)

    if target_id is not None and target_id != root.get("id"):
        if find_node(t, target_id) is None:
            raise KeyError(f"feature {target_id} not found")
        _combine_with(t["root"], primitive, operation, target_id, ids)
        return t

    # First real feature on a blank doc replaces the placeholder base (a subtract
    # still needs stock, so it falls through and cuts the placeholder instead).
    if root.get("placeholder") and operation != "subtract":
        t["root"] = primitive
        return t
    if root.get("type") == "BooleanFeature" and root.get("params", {}).get("operation") == operation:
        root["params"].setdefault("children", []).append(primitive)
        return t
    t["root"] = {
        "type": "BooleanFeature",
        "id": ids.alloc(),
        "name": operation.capitalize(),
        "params": {"operation": operation, "children": [root, primitive]},
    }
    return t


def _combine_with(node: Dict[str, Any], primitive: Dict[str, Any], operation: str,
                  target_id: str, ids: "IdAllocator") -> bool:
    """Wrap the target in a boolean with ``primitive``, in place. True if found.

    Appending to a target that is *already* a boolean of the same operation
    mirrors what the root path does, and is what makes a third and fourth child
    land in one group rather than in a right-leaning chain of nested pairs — two
    shapes that evaluate the same but decompile and read differently.
    """
    kids = children_of(node)
    for i, child in enumerate(kids):
        if child.get("id") == target_id:
            if (child.get("type") == "BooleanFeature"
                    and (child.get("params") or {}).get("operation") == operation):
                child["params"].setdefault("children", []).append(primitive)
            else:
                kids[i] = {
                    "type": "BooleanFeature",
                    "id": ids.alloc(),
                    "name": operation.capitalize(),
                    "params": {"operation": operation, "children": [child, primitive]},
                }
            node["params"]["children"] = kids
            return True
        if _combine_with(child, primitive, operation, target_id, ids):
            return True
    return False


def parent_of(tree: Dict[str, Any], node_id: str) -> Optional[Dict[str, Any]]:
    """The node whose children contain ``node_id``, or None for the root."""
    for node in walk_nodes(tree):
        if any(c.get("id") == node_id for c in children_of(node)):
            return node
    return None


def add_modifier(tree: Dict[str, Any], modifier: Dict[str, Any],
                 target_id: str | None = None) -> Dict[str, Any]:
    """Wrap a feature in a modifier. Without ``target_id`` the whole body is
    wrapped (root); with it, that specific feature is wrapped **in place** — so a
    Pattern/Transform can act on one selected feature (e.g. replicate one hole)
    rather than the entire part."""
    t = copy.deepcopy(tree)
    IdAllocator(t).ensure(modifier)
    modifier.setdefault("params", {})

    if target_id is None or target_id == t["root"].get("id"):
        modifier["params"]["children"] = [t["root"]]
        t["root"] = modifier
        return t

    def wrap(node: Dict[str, Any]) -> Dict[str, Any]:
        kids = children_of(node)
        for i, c in enumerate(kids):
            if c.get("id") == target_id:
                modifier["params"]["children"] = [c]
                kids[i] = modifier
                node["params"]["children"] = kids
                return node
            wrap(c)
        return node

    if find_node(t, target_id) is None:
        raise KeyError(f"feature {target_id} not found")
    wrap(t["root"])
    return t


def remove_node(tree: Dict[str, Any], node_id: str) -> Dict[str, Any]:
    t = copy.deepcopy(tree)
    root = t["root"]
    if root.get("id") == node_id:
        raise ValueError("cannot delete the root feature")

    def prune(n: Dict[str, Any]) -> Dict[str, Any]:
        kids = children_of(n)
        if not kids:
            return n
        new_kids = [prune(c) for c in kids if c.get("id") != node_id]
        n["params"]["children"] = new_kids
        if is_boolean(n) and len(new_kids) == 1:
            return new_kids[0]
        return n

    t["root"] = prune(root)
    return t


def set_keep(tree: Dict[str, Any], node_id: str, keep: bool) -> Dict[str, Any]:
    """Mark a feature as must-keep for generative optimization.

    Topology optimization is free to delete any material that isn't carrying
    load, which routinely removes things the part needs anyway — mounting bosses,
    bearing seats, the walls around a bolt hole. Flagging the feature pins its
    material solid for the whole solve, so the optimizer works around it instead
    of through it.

    Stored on the node like ``suppressed``, so it survives serialization and
    version history without a schema change.
    """
    t = copy.deepcopy(tree)

    def walk(n: Dict[str, Any]) -> bool:
        if n.get("id") == node_id:
            if keep:
                n["keep"] = True
            else:
                n.pop("keep", None)
            return True
        return any(walk(c) for c in children_of(n))

    if not (t.get("root") and walk(t["root"])):
        raise KeyError(f"feature {node_id} not found")
    return t


def kept_features(tree: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every node flagged must-keep, in tree order."""
    return [n for n in walk_nodes(tree) if n.get("keep")]


def set_suppressed(tree: Dict[str, Any], node_id: str, suppressed: bool) -> Dict[str, Any]:
    t = copy.deepcopy(tree)

    def walk(n: Dict[str, Any]) -> bool:
        if n.get("id") == node_id:
            if suppressed:
                n["suppressed"] = True
            else:
                n.pop("suppressed", None)
            return True
        return any(walk(c) for c in children_of(n))

    if not (t.get("root") and walk(t["root"])):
        raise KeyError(f"feature {node_id} not found")
    return t


def reorder(tree: Dict[str, Any], node_id: str, direction: str) -> Dict[str, Any]:
    """Move a feature up/down within its parent's children list."""
    t = copy.deepcopy(tree)
    delta = -1 if direction == "up" else 1

    def walk(n: Dict[str, Any]) -> bool:
        kids = children_of(n)
        for i, c in enumerate(kids):
            if c.get("id") == node_id:
                j = i + delta
                if 0 <= j < len(kids):
                    kids[i], kids[j] = kids[j], kids[i]
                return True
        return any(walk(c) for c in kids)

    if not (t.get("root") and walk(t["root"])):
        raise KeyError(f"feature {node_id} not found")
    return t


def resolve_suppressed(tree: Dict[str, Any]) -> Dict[str, Any]:
    """Return an evaluation tree with suppressed features pruned.

    Suppressing a modifier passes its child through (keep the geometry, skip the
    op); suppressing a primitive or boolean drops that subtree. Booleans that fall
    to one child collapse; an empty result becomes a tiny placeholder box.
    """
    t = copy.deepcopy(tree)

    def resolve(node: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        kids = children_of(node)
        if node.get("suppressed"):
            if not is_boolean(node) and len(kids) == 1:
                return resolve(kids[0])  # modifier -> pass child through
            return None  # primitive / boolean -> drop
        if kids:
            new_kids = [r for r in (resolve(c) for c in kids) if r is not None]
            node["params"]["children"] = new_kids
            if is_boolean(node):
                if len(new_kids) == 0:
                    return None
                if len(new_kids) == 1:
                    return new_kids[0]
        return node

    root = resolve(t["root"]) if t.get("root") else None
    if root is None:
        # A transient stand-in for "everything is suppressed" — this tree is only
        # ever evaluated, never persisted, so the id is a fixed literal.
        root = {"type": "BoxFeature", "id": "f_empty", "name": "Empty",
                "params": {"size": {"x": 0.001, "y": 0.001, "z": 0.001}, "center": {"x": 0, "y": 0, "z": 0}}}
    return {"version": t.get("version", "brep-v1"), "root": root}


def resolve_datums(tree: Dict[str, Any]) -> Dict[str, Any]:
    """Bake symbolic ``on_face`` references into concrete plane/offset params.

    Any ExtrudeFeature carrying ``params.on_face = {feature_id, face}`` has its
    ``plane`` and ``offset`` recomputed from the referenced feature's *current*
    geometry (see ``services/datum``). This is what lets a sketch-on-face follow
    an upstream size change. Run after ``resolve_suppressed`` so a reference to a
    suppressed feature is skipped gracefully rather than pinning stale geometry.

    A reference that cannot be resolved (missing/suppressed feature, empty solid)
    is left as-is — the node keeps its last concrete plane/offset as a fallback.
    """
    from app.services import datum

    t = copy.deepcopy(tree)

    def _subtree_ids(node: Dict[str, Any]) -> set:
        ids = {node.get("id")}
        for c in children_of(node):
            ids |= _subtree_ids(c)
        return ids

    def walk(node: Dict[str, Any]) -> None:
        params = node.get("params", {})
        ref = params.get("on_face")
        if node.get("type") == "ExtrudeFeature" and isinstance(ref, dict):
            fid = ref.get("feature_id")
            face = ref.get("face")
            if fid and fid != node.get("id"):
                # Skip re-resolution when the referenced feature *contains* this
                # extrude (an ancestor / whole-body reference): resolving it would
                # fold the sketch's own geometry into the datum and drift the plane
                # upward every regen. The plane baked at creation (before the
                # sketch was in the tree) is the correct, stable fallback.
                target = find_node(t, fid)
                is_ancestor = target is not None and node.get("id") in _subtree_ids(target)
                if not is_ancestor:
                    try:
                        plane, offset, origin_uv = datum.resolve_face_frame(t, fid, face)
                        params["plane"] = plane
                        params["offset"] = offset
                        params["plane_origin"] = list(origin_uv)  # zero the sketch on the face
                    except (KeyError, ValueError):
                        pass  # keep the baked-in fallback plane/offset
        for c in children_of(node):
            walk(c)

    if t.get("root"):
        walk(t["root"])
    return t


def ensure_ids(tree: Dict[str, Any]) -> Dict[str, Any]:
    t = copy.deepcopy(tree)
    ids = IdAllocator(t)

    def walk(n: Dict[str, Any]) -> None:
        ids.ensure(n)
        for c in children_of(n):
            walk(c)

    if t.get("root"):
        walk(t["root"])
    return t

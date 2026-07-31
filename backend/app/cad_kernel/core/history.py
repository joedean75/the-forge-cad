"""Generative history: following topology through a rebuild instead of re-measuring it.

`core/naming` names an entity by *what it is* — kind, size, and position normalised
to the body's bounding box. That is what makes a reference survive a resize, and it
is the only thing that can work for a body with no construction history at all (a
STEP file). What it cannot survive is an edit that **re-topologises**: cut a slot
across a filleted face and the face becomes two faces, each with a new size and a
new normalised position. A descriptor either picks one of them arbitrarily or gives
up. Neither is right — the correct answer is "this reference now names two faces".

OpenCascade already knows. Every `BRepAlgoAPI_*` and `BRepBuilderAPI_MakeShape`
builder can say, for a sub-shape of its input, what that sub-shape became:
`Modified` (it is now these), `Generated` (it produced these), `IsDeleted` (it is
gone). This module threads those answers through the whole evaluation so that a
reference resolves by *following the map* rather than by re-measuring.

The scheme
----------

Every face and edge carries a **tag** — an identity, not a measurement. A tag is
minted once, by the feature that created the entity, as
``{feature_id}:{kind}:{index}``: the index is the entity's position in
`TopExp::MapShapes`' deterministic exploration order of *that feature's own
output*. That is a much stronger assumption than global exploration order, and a
true one: `BRepPrimAPI_MakeBox` visits its six faces in the same order whatever
the dimensions, so stretching a plate does not renumber anything.

From there tags propagate. When a builder consumes a shape, each input sub-shape's
tag is handed to whatever it became — one successor for a face that merely moved,
*several* for one that was split, none for one that was consumed. Entities with no
antecedent are new, and are tagged by the feature that made them.

Three deliberate limits, each of which is the honest answer rather than a
shortcut:

* **Absent history is normal.** `BRepPrimAPI_MakeBox` has no history because it
  has no input, and `BRepFilletAPI_MakeFillet` does not report what happened to
  the edges it did not touch. A stage that says nothing about a sub-shape is read
  as "it came through unchanged", and if it did not, the tag simply fails to
  appear on the result — which falls back to descriptor matching.
* **Only a real history reports deletion.** `BRepFilletAPI_MakeFillet.IsDeleted`
  returns ``True`` for box edges that are plainly still there, so trusting it
  would report live geometry as destroyed. Only builders that fill a genuine
  history (the `BRepAlgoAPI_*` booleans) are believed on that question, which is
  why `advance` takes `trust_deleted` rather than assuming it.
* **A successor has to be the same kind.** A fillet `Generated`s a *face* from an
  edge. That face is a new entity — the fillet surface — not the edge under a new
  name, so cross-kind successors are dropped and the face is tagged as new.

Tags are content-free and deterministic: they depend only on feature ids (which
`feature_ops.IdAllocator` already makes reproducible) and on topology order. The
same document therefore produces the same tags on every machine and every run,
which is the property replay and trajectory export need.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

# Faces and edges are the only entities anything selects. Vertices would double
# the traversal cost for a kind no operation in the catalog takes.
KINDS = ("face", "edge")


def _enum(kind: str):
    from OCP.TopAbs import TopAbs_ShapeEnum

    return (TopAbs_ShapeEnum.TopAbs_FACE if kind == "face"
            else TopAbs_ShapeEnum.TopAbs_EDGE)


def _explore(shape, kind: str) -> List[Any]:
    """Sub-shapes of one kind, de-duplicated, in OCC's deterministic order.

    The same traversal `naming._explore` uses, and for the same reason: an
    explorer visits a shared face once per parent, so a map is needed to collapse
    the repeats. Kept separate because this module must not depend on `naming`
    (naming depends on it).
    """
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    mapping = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, _enum(kind), mapping)
    return [mapping.FindKey(i) for i in range(1, mapping.Extent() + 1)]


class ShapeKeys:
    """Stable integer keys for sub-shapes, shared across one whole evaluation.

    `TopoDS_Shape` has no usable Python identity — two handles to the same face
    are different objects — so a dict cannot be keyed on the shape itself.
    `TopTools_IndexedMapOfShape` hashes by `IsSame`, which ignores orientation, so
    a face and its reverse land on one key. That is the identity this module
    wants: a sub-shape passed unchanged from a child body into its parent keeps
    its key, which is what makes "no history reported means it survived" work.
    """

    def __init__(self):
        from OCP.TopTools import TopTools_IndexedMapOfShape

        self._map = TopTools_IndexedMapOfShape()

    def key(self, shape) -> int:
        # `Add` returns the existing index when the shape is already mapped, so
        # this is one crossing into OCC rather than a FindIndex/Add pair.
        return self._map.Add(shape)


class EntityMap:
    """Which tag each sub-shape of one built shape carries.

    Held per build *stage*, not per document: the map for a fillet's child body
    and the map for the filleted result are different objects, because the same
    tag points at different geometry in each.
    """

    def __init__(self, keys: ShapeKeys):
        self._keys = keys
        self._tags: Dict[int, str] = {}
        # Tags a builder explicitly said it destroyed. Only ever written from a
        # builder whose IsDeleted is trustworthy — see the module docstring.
        self.removed: Set[str] = set()

    # ------------------------------------------------------------ writing --

    def assign(self, sub, tag: str) -> None:
        self._tags.setdefault(self._keys.key(sub), tag)

    def tag_of(self, sub) -> Optional[str]:
        return self._tags.get(self._keys.key(sub))

    def name_new(self, shape, owner: str) -> None:
        """Tag whatever this stage created and nothing carried a tag into.

        Numbered among the *untagged* entities rather than by absolute position,
        so adding a feature that carries its own tags through does not renumber
        the ones this feature made.
        """
        for kind in KINDS:
            fresh = 0
            for sub in _explore(shape, kind):
                if self.tag_of(sub) is None:
                    self.assign(sub, f"{owner}:{kind}:{fresh}")
                    fresh += 1

    def suffixed(self, suffix: str) -> "EntityMap":
        """A copy with every tag suffixed — one instance of a pattern.

        Copy *i* of a patterned feature has genuinely different edges from copy
        0, and a reference has to be able to name one of them.
        """
        out = EntityMap(self._keys)
        out._tags = {k: f"{t}{suffix}" for k, t in self._tags.items()}
        out.removed = {f"{t}{suffix}" for t in self.removed}
        return out

    # ------------------------------------------------------------ reading --

    def entities(self, shape, kind: str) -> Dict[str, List[Any]]:
        """``tag -> sub-shapes of ``shape`` carrying it``, in exploration order.

        Resolved against the shape rather than read out of the table, so a tag
        assigned to something that did not end up in the result — which absent
        history makes possible — simply does not appear, instead of resolving to
        geometry that is not on the body.
        """
        found: Dict[str, List[Any]] = {}
        for sub in _explore(shape, kind):
            tag = self.tag_of(sub)
            if tag is not None:
                found.setdefault(tag, []).append(sub)
        return found


def seed(shape, owner: str, keys: ShapeKeys) -> EntityMap:
    """First tags for a shape built from nothing — a primitive, or an import."""
    entities = EntityMap(keys)
    entities.name_new(shape, owner)
    return entities


def _drain(answer) -> List[Any]:
    """A `TopTools_ListOfShape` as a Python list, without paying to iterate an empty one.

    This is the hot loop of the whole module — two queries per sub-shape per stage
    — and `list(occ_list)` costs about 0.26 ms *whether or not the list has
    anything in it*, because pybind builds an iterator object either way. Almost
    every sub-shape of almost every build is untouched and answers empty, so
    checking `Size` first (a thousandth of that) is the difference between
    history costing 350% of an exact build and costing a few percent. `First` is
    the same trick for the overwhelmingly common size-one answer.
    """
    size = answer.Size()
    if size == 0:
        return []
    return [answer.First()] if size == 1 else list(answer)


def _successors(builder, sub) -> List[Any]:
    """What ``sub`` became, restricted to entities of its own kind.

    `Modified` first: for a boolean it is the list a split face or edge turned
    into, which is the case this whole module exists for. `Generated` is the
    fallback for builders that report a replacement that way, and it is where the
    same-kind filter earns its place — a fillet generates a *face* from an edge,
    and that face is a new entity rather than the edge renamed.
    """
    kind = sub.ShapeType()
    for query in (builder.Modified, builder.Generated):
        try:
            candidates = _drain(query(sub))
        except Exception:  # noqa: BLE001 — not every builder answers every query
            continue
        out = [s for s in candidates if s.ShapeType() == kind]
        if out:
            return out
    return []


def advance(sources: Sequence[Tuple[Any, Optional[EntityMap]]], builder, result,
            owner: str, keys: ShapeKeys, *, trust_deleted: bool = False) -> EntityMap:
    """The tag map for ``result``, following ``builder``'s history from its inputs.

    ``sources`` are the (shape, map) pairs that went in; ``owner`` names whatever
    this stage creates from scratch. ``trust_deleted`` says whether the builder's
    ``IsDeleted`` means anything — see the module docstring for why that is not a
    given.
    """
    out = EntityMap(keys)
    for shape, tags in sources:
        if tags is None:
            continue
        out.removed |= tags.removed
        for kind in KINDS:
            for sub in _explore(shape, kind):
                tag = tags.tag_of(sub)
                if tag is None:
                    continue
                successors = _successors(builder, sub)
                if successors:
                    for successor in successors:
                        out.assign(successor, tag)
                elif trust_deleted and _is_deleted(builder, sub):
                    out.removed.add(tag)
                else:
                    # Nothing said otherwise, so it came through untouched. If it
                    # did not, `entities` will not find it on the result and the
                    # reference falls back to descriptor matching.
                    out.assign(sub, tag)
    out.name_new(result, owner)
    return out


def _is_deleted(builder, sub) -> bool:
    try:
        return bool(builder.IsDeleted(sub))
    except Exception:  # noqa: BLE001
        return False



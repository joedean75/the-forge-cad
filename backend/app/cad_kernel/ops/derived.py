"""Derived geometry — one document reading another's, live.

Everything else here is bottom-up: a document is one tree with one body set, and
an assembly references *finished* documents. There has been no way to say "the
lid's outline is the box's outline" and have it stay true, so the outline gets
typed twice and the second copy is wrong from the first edit onwards.

A `DerivedFeature` names `{document_id, revision?, entities}` and pulls that
geometry in **at evaluation time**. It is not a copy: nothing of the source is
stored here beyond its id, so the source's next edit is the child's next
evaluation. That is the whole feature, and it is also the whole risk — the three
things it can get wrong are cycles, staleness and access, and each is handled
where it can actually be seen:

- **Cycles** are refused twice. `add_derived` walks the graph before it writes,
  because a cycle a user can create is a document they then cannot open; and
  resolution carries the chain of documents it is already inside, because the
  first check can be defeated by editing the *other* document afterwards. The
  second one is what makes an infinite recursion impossible rather than
  unlikely.
- **Staleness** is `document_service._evaluation_key`, which hashes what is *in*
  the document — and a derived document's geometry is not. The key folds in each
  source's own key, recursively, so a parameter changed three documents upstream
  invalidates this one.
- **Access** is checked at resolution against the *deriving document's owner*,
  the same rule `assembly_service.add_component` applies to a part: a reference
  reads the source's geometry straight back out, so a document you can no longer
  open is one you can no longer derive from. It needs no caller identity — it is
  a question about two stored documents — which is why it can live at resolution
  rather than being threaded through every handler.

**Two kinds, because a shared outline is the case that matters and a body is the
case that is obvious.** `kind: "body"` splices the source's solid (or the named
subtrees of it) in as ordinary features — so both kernels, the mesher, DFM and
naming all see a tree of the shapes they already know, and nothing needed a new
evaluator entry. `kind: "sketch"` brings across a `SketchFeature`, which is what
an enclosure's top and bottom sharing one profile actually needs: each extrudes
the same outline to its own thickness.

**The grafted ids are prefixed, and deterministically.** Two documents mint ids
from their own allocators, so `f3` means something different in each; splicing
without renaming would collide. `{derived_id}~{source_id}` is stable across
rebuilds — which is what trajectory replay and the decompiler's mesh comparison
rest on — and readable, which matters the first time somebody sees one in a
feature list.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.cad_kernel.core.feature_tree import Feature, register_feature
from app.cad_kernel.core.topology import Solid

#: How a derived reference reads its source.
KINDS = ("body", "sketch")

#: Separates the feature that pulled the geometry from the id it had at home.
#: A character that cannot appear in an allocator-minted id (`f<n>`), so
#: `{prefix}~{id}` can be split back apart to say where a node came from.
GRAFT_SEP = "~"


class DerivedNotResolved(ValueError):
    """A DerivedFeature reached an evaluator, so it was never resolved."""


@register_feature
@dataclass
class DerivedFeature(Feature):
    """Geometry read from another document at evaluation time.

    Holds a reference and nothing else. `services/derived_service.resolve`
    replaces it with the source's own features before anything evaluates, for
    the same reason `SketchFeature` is substituted out: every stage downstream
    is written against ordinary features, and the one that is not would be a
    second place this system decides what a body is.
    """

    document_id: str = ""
    #: A released revision letter, or None for the source as it stands now.
    #: Pinning is the opt-out from the whole point of the feature, so it is
    #: never the default — but a child that must not move when its parent does
    #: is a real requirement and the alternative is a copy nobody maintains.
    revision: Optional[str] = None
    #: Which of the source's features to pull. Empty means its whole body.
    entities: List[str] = field(default_factory=list)
    kind: str = "body"

    def evaluate(self) -> Solid:
        raise DerivedNotResolved(
            f"derived feature {self.id} was not resolved — it names geometry in "
            f"document {self.document_id!r} and has no body of its own until "
            f"that document is read")

    def _serialize_params(self) -> Dict[str, Any]:
        return {"document_id": self.document_id, "revision": self.revision,
                "entities": list(self.entities), "kind": self.kind}

    @classmethod
    def _from_dict(cls, data: Dict[str, Any]) -> "DerivedFeature":
        p = data.get("params") or {}
        return cls(
            id=data.get("id"),
            name=data.get("name", "Derived"),
            document_id=p.get("document_id") or "",
            revision=p.get("revision"),
            entities=list(p.get("entities") or []),
            kind=p.get("kind") or "body",
        )

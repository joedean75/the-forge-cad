"""Parametric feature tree for the Forge CAD kernel.

A feature tree is a declarative recipe that produces a `Solid`.  Features are
serializable so designs can be stored, transferred, and regenerated.

**Adding a feature type:** define it in `cad_kernel/ops/`, decorate it with
`@register_feature`, and teach the SDF evaluator how to build it (one entry in
`evaluator/sdf._SDF_BUILDERS`). Nothing here needs editing — the registry used
to be a hand-maintained import list plus a parallel dict, so a new feature that
forgot either half failed at runtime, far from the cause.
"""

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Literal, Optional, Type

from app.cad_kernel.core.topology import Solid
from app.cad_kernel.core.types import Vector3

# Feature type name -> class, populated by @register_feature at import time.
_FEATURE_TYPES: Dict[str, Type["Feature"]] = {}
_ops_loaded = False


def register_feature(cls: Type["Feature"]) -> Type["Feature"]:
    """Register a Feature subclass so `Feature.from_dict` can rebuild it."""
    _FEATURE_TYPES[cls.__name__] = cls
    return cls


def _load_ops() -> None:
    """Import every ops module once so its features self-register.

    Deferred rather than imported at module scope because the ops modules import
    back into core; doing it on first deserialization keeps that cycle broken.
    """
    global _ops_loaded
    if _ops_loaded:
        return
    _ops_loaded = True
    from app.cad_kernel import ops

    for module in pkgutil.iter_modules(ops.__path__):
        importlib.import_module(f"{ops.__name__}.{module.name}")


def feature_types() -> Dict[str, Type["Feature"]]:
    """Every registered feature type, keyed by the name used in serialized trees."""
    _load_ops()
    return dict(_FEATURE_TYPES)


# ---------------------------------------------------------------------------
# Serialized-tree helpers
#
# These operate on the *dict* form of a tree, which is what the database, the
# API and the operation layer all pass around. Everything that walks a tree
# should come through here: children live under ``params.children``, and code
# that reimplemented that lookup has reached for a non-existent top-level
# ``children`` key more than once.
# ---------------------------------------------------------------------------

def node_children(node: Any) -> List[Dict[str, Any]]:
    """Child nodes of a serialized feature node."""
    if not isinstance(node, dict):
        return []
    kids = (node.get("params") or {}).get("children") or []
    return [k for k in kids if isinstance(k, dict)]


def root_of(tree_or_node: Any) -> Optional[Dict[str, Any]]:
    """Accept either a whole tree (`{"root": …}`) or a bare node."""
    if not isinstance(tree_or_node, dict):
        return None
    if "type" in tree_or_node:
        return tree_or_node
    root = tree_or_node.get("root")
    return root if isinstance(root, dict) else None


def walk_nodes(tree_or_node: Any) -> Iterator[Dict[str, Any]]:
    """Depth-first iteration over a serialized tree, parents before children."""
    root = root_of(tree_or_node)
    if root is None:
        return
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node_children(node)))


def find_node_by_type(tree_or_node: Any, type_name: str, node_id: Optional[str] = None):
    """First node of `type_name`, optionally pinned to a specific id."""
    for node in walk_nodes(tree_or_node):
        if node.get("type") == type_name and (node_id is None or node.get("id") == node_id):
            return node
    return None


@dataclass
class Feature:
    """Base class for all parametric features."""

    id: Optional[str] = None
    name: str = "Feature"
    params: Dict[str, Any] = field(default_factory=dict)

    def evaluate(self) -> Solid:
        raise NotImplementedError

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.__class__.__name__,
            "id": self.id,
            "name": self.name,
            "params": self._serialize_params(),
        }

    def _serialize_params(self) -> Dict[str, Any]:
        return self.params

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "Feature":
        """Rebuild a feature from its serialized form.

        Types come from the decorator-populated registry, so a new feature is
        available here the moment its module defines it — there is no list to
        keep in step.
        """
        _load_ops()
        cls = _FEATURE_TYPES.get(data["type"])
        if cls is None:
            known = ", ".join(sorted(_FEATURE_TYPES))
            raise ValueError(f"Unknown feature type: {data['type']} (known: {known})")
        return cls._from_dict(data)


@dataclass
class FeatureTree:
    """A rooted tree of features."""

    root: Feature
    version: str = "brep-v1"

    def evaluate(self) -> Solid:
        return self.root.evaluate()

    def to_dict(self) -> Dict[str, Any]:
        return {"version": self.version, "root": self.root.to_dict()}

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "FeatureTree":
        return FeatureTree(root=Feature.from_dict(data["root"]), version=data.get("version", "brep-v1"))

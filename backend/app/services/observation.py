"""The state an agent sees, and what one operation changed about it.

Two things were missing from the operation API for agent use. There was no
compact, stable description of the document — the copilot had a private
`_doc_summary` and everyone else got the whole `DesignDocument`, mesh data-URI
and all. And nothing reported what an operation actually *did*: the caller had to
diff a base64 STL against the previous one to discover whether its edit had any
effect.

`summarize` is the observation; `snapshot` + `delta` are the effect. Both are
plain JSON so they can go into a transcript, an HTTP response, or a training
example unchanged.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

MM = 1000.0

# Structural or bulky keys: an agent editing a feature wants its dimensions, not
# its children array or a 400-point traced polygon.
_SKIP_PARAM_KEYS = {"children", "profile", "sketch"}


def trim_params(params: Optional[dict]) -> dict:
    """Feature params minus heavy/structural keys — enough to edit from."""
    return {k: v for k, v in (params or {}).items() if k not in _SKIP_PARAM_KEYS}


def _bbox_mm(bbox) -> Optional[Dict[str, float]]:
    if bbox is None:
        return None
    return {
        "x": round((bbox.max.x - bbox.min.x) * MM, 3),
        "y": round((bbox.max.y - bbox.min.y) * MM, 3),
        "z": round((bbox.max.z - bbox.min.z) * MM, 3),
    }


def features(doc) -> List[dict]:
    """Every feature as {id, type, name, params}, parents before children.

    Params are included because an agent that cannot see a box's current
    ``size: {x, y, z}`` will invent parameter names when asked to change it.
    """
    out: List[dict] = []

    def walk(node: dict) -> None:
        out.append({
            "id": node.get("id"),
            "type": node.get("type"),
            "name": node.get("name"),
            "params": trim_params(node.get("params", {})),
        })
        for child in (node.get("params", {}) or {}).get("children", []) or []:
            walk(child)

    root = (doc.feature_tree or {}).get("root")
    if root:
        walk(root)
    return out


def summarize(doc) -> dict:
    """Compact, LLM-facing snapshot of the current body."""
    d = doc.derived
    return {
        "name": doc.name,
        "features": features(doc),
        "bbox_size_mm": _bbox_mm(d.bbox),
        "mass_kg": round(d.mass, 4) if d.mass else 0.0,
        "material_id": doc.material_id,
        "error": d.error,
    }


def snapshot(doc) -> dict:
    """The measurable state that a delta is computed against.

    Deliberately cheap: no meshing, no export. Everything here is already on the
    document, so taking a snapshot before and after an operation costs nothing.
    """
    if doc is None:
        return {}
    d = doc.derived
    return {
        "volume": d.volume,
        "mass": d.mass,
        "bbox_mm": _bbox_mm(d.bbox),
        "feature_ids": [f["id"] for f in features(doc)],
        "error": d.error,
    }


def _round(value: Optional[float], places: int = 9) -> Optional[float]:
    return None if value is None else round(value, places)


def delta(before: dict, after: dict) -> dict:
    """What changed between two snapshots.

    Reports feature ids added and removed rather than a count, so a caller can
    tell *which* feature an operation created without a follow-up
    ``list_features`` round-trip — the id is what every later call references.
    """
    if not before or not after:
        return {}

    before_ids = list(before.get("feature_ids") or [])
    after_ids = list(after.get("feature_ids") or [])
    added = [i for i in after_ids if i not in set(before_ids)]
    removed = [i for i in before_ids if i not in set(after_ids)]

    vol_before, vol_after = before.get("volume") or 0.0, after.get("volume") or 0.0
    mass_before, mass_after = before.get("mass") or 0.0, after.get("mass") or 0.0

    return {
        "volume": {"before": _round(vol_before), "after": _round(vol_after),
                   "change": _round(vol_after - vol_before)},
        "mass": {"before": _round(mass_before, 6), "after": _round(mass_after, 6),
                 "change": _round(mass_after - mass_before, 6)},
        "bbox_mm": {"before": before.get("bbox_mm"), "after": after.get("bbox_mm")},
        "features": {
            "added": added,
            "removed": removed,
            "count_before": len(before_ids),
            "count_after": len(after_ids),
        },
        # Strictly about geometry — deliberately *not* including whether features
        # were added or removed. An operation that adds a feature and moves
        # nothing measurable is the signature of an edit that silently did not
        # apply, and folding the tree change in here would mask exactly that.
        # (It caught a whole-body `add_modifier(fillet)` doing nothing at all.)
        "changed_geometry": bool(
            _round(vol_after - vol_before) not in (0.0, None)
            or before.get("bbox_mm") != after.get("bbox_mm")
        ),
    }


def result_is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, (dict, list, str)) and len(value) == 0)

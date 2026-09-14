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

# Params holding stored topology references. What is *stored* is the whole
# descriptor — an id alone is a hash of the entity's current measurements and
# breaks the moment an upstream dimension moves it — but what an agent needs to
# read back is the id, and ten fields apiece across a dozen edges buries the
# dimensions it came for.
_REFERENCE_KEYS = {"edges", "faces"}


def _summarize_value(key: str, value: Any) -> Any:
    if key in _REFERENCE_KEYS and isinstance(value, list):
        ids = [r.get("id") for r in value if isinstance(r, dict) and r.get("id")]
        return ids or value
    if key == "on_face" and isinstance(value, dict):
        return {k: v for k, v in value.items() if k != "face_ref"}
    return value


def trim_params(params: Optional[dict]) -> dict:
    """Feature params minus heavy/structural keys — enough to edit from."""
    return {k: _summarize_value(k, v) for k, v in (params or {}).items()
            if k not in _SKIP_PARAM_KEYS}


def _bbox_mm(bbox) -> Optional[Dict[str, float]]:
    if bbox is None:
        return None
    return {
        "x": round((bbox.max.x - bbox.min.x) * MM, 3),
        "y": round((bbox.max.y - bbox.min.y) * MM, 3),
        "z": round((bbox.max.z - bbox.min.z) * MM, 3),
    }


def bounds_mm(bbox) -> Optional[dict]:
    """Full bounds; size alone loses where an off-centre part actually lies."""
    if bbox is None:
        return None
    return {side: {axis: round(getattr(getattr(bbox, side), axis) * MM, 3)
                   for axis in ("x", "y", "z")} for side in ("min", "max")}


def features(doc) -> List[dict]:
    """Every feature as {id, type, name, params}, parents before children.

    Params are included because an agent that cannot see a box's current
    ``size: {x, y, z}`` will invent parameter names when asked to change it.
    """
    out: List[dict] = []

    def walk(node: dict, parent_id=None) -> None:
        out.append({
            "id": node.get("id"),
            "type": node.get("type"),
            "name": node.get("name"),
            "params": trim_params(node.get("params", {})),
            "parent_id": parent_id,
            "keep": bool(node.get("keep")),
            "suppressed": bool(node.get("suppressed")),
        })
        for child in (node.get("params", {}) or {}).get("children", []) or []:
            walk(child, node.get("id"))

    root = (doc.feature_tree or {}).get("root")
    if root:
        walk(root)
    return out


def summarize(doc) -> dict:
    """Compact, LLM-facing snapshot of the current body."""
    d = doc.derived
    spec = getattr(doc, "spec", None) or {}
    out = {
        "kind": "document",
        "id": doc.id,
        "name": doc.name,
        "features": features(doc),
        "bbox_size_mm": _bbox_mm(d.bbox),
        "bbox_mm": bounds_mm(d.bbox),
        "mass_kg": round(d.mass, 4) if d.mass else 0.0,
        "material_id": doc.material_id,
        "error": d.error,
        "parameter_units": "Feature geometry uses metres unless a parameter declares another unit; *_mm is millimetres, *_deg is degrees.",
    }
    # A sheet's mass and volume are zero because it has neither, not because
    # nothing is assigned — so it says what it is and reports the number it does
    # have. Stated rather than inferred from the zero, for the same reason
    # `summarize_assembly` states its `kind`: an agent that reads a missing
    # figure as an unset one will try to fix it by assigning a material.
    if str(getattr(d, "body_kind", "solid")) == "sheet":
        out["body_kind"] = "sheet"
        out["area_mm2"] = round(float(getattr(d, "area", 0.0)) * 1e6, 3)
    # Reading constraints is cheap; evaluating their score is a separate tool.
    if spec:
        out["spec"] = spec
    for key in ("source_prompt", "manufacturing", "general_tolerance",
                "active_configuration", "mechanism"):
        value = getattr(doc, key, None)
        if value:
            out[key] = value
    if getattr(doc, "parameters", None):
        out["parameters"] = [p.model_dump() for p in doc.parameters]
    return out


def summarize_assembly(asm) -> dict:
    """The same job as `summarize`, for an assembly rather than a part.

    An assembly is not a `DesignDocument` and cannot be squeezed into one: it has
    no feature tree, and the thing an agent needs to reason about is *which part
    sits where and what holds it there*. Positions are in millimetres for the
    same reason every other observation is — see `services/units`.
    """
    from itertools import product
    import numpy as np
    from app.cad_kernel.evaluator.sdf import _euler_matrix
    from app.services import assembly_service

    frames = assembly_service.connector_frames(asm)
    components = []
    world_corners = []
    for c in getattr(asm, "components", []) or []:
        t = c.transform
        rotation = _euler_matrix(t.rotate_deg.x, t.rotate_deg.y, t.rotate_deg.z)
        translate = np.array([t.translate.x, t.translate.y, t.translate.z])
        item = {
            "id": c.id,
            "part_id": c.part_id,
            "name": c.name,
            "position_mm": {"x": round(t.translate.x * MM, 3),
                            "y": round(t.translate.y * MM, 3),
                            "z": round(t.translate.z * MM, 3)},
            "size_mm": _bbox_mm(c.bbox),
            "local_bbox_mm": bounds_mm(c.bbox),
            "rotation_deg": t.rotate_deg.model_dump(),
            "scale": t.scale,
            "positioned_by_mates": [m.id for m in asm.mates if m.moved == c.id],
            # A component whose part was deleted is still in the tree and is not
            # geometry; an agent that cannot see that will try to mate to it.
            "missing": bool(c.missing),
        }
        if c.bbox is not None and not c.missing:
            corners = np.array(list(product(*[(getattr(c.bbox.min, a), getattr(c.bbox.max, a))
                                              for a in ("x", "y", "z")])))
            corners = (corners * t.scale) @ rotation.T + translate
            world_corners.extend(corners)
            item["world_bbox_mm"] = {
                "min": dict(zip(("x", "y", "z"), np.round(corners.min(axis=0) * MM, 3).tolist())),
                "max": dict(zip(("x", "y", "z"), np.round(corners.max(axis=0) * MM, 3).tolist())),
            }
        item["connectors"] = [{
            "id": cid,
            "local_origin_mm": np.round(origin * MM, 3).tolist(),
            "local_axes": np.round(axes, 6).tolist(),
            "world_origin_mm": np.round((rotation @ (origin * t.scale) + translate) * MM, 3).tolist(),
            "world_axes": np.round(rotation @ axes, 6).tolist(),
        } for cid, (origin, axes) in (frames.get(c.part_id) or {}).items()]
        components.append(item)
    return {
        # Which kind of thing this observation describes. An agent driving both
        # subjects gets two differently-shaped states, and one that has to infer
        # which from the presence of a key will eventually infer wrong.
        "kind": "assembly",
        "id": asm.id,
        "name": asm.name,
        "components": components,
        # `angle_deg` is in here because it is a pose an agent can change and
        # then has to be able to read back — a state an operation moves and the
        # observation does not show is one nothing can be trained against.
        "mates": [m.model_dump() for m in (getattr(asm, "mates", []) or [])],
        "bbox_size_mm": _bbox_mm(getattr(asm, "bbox", None)),
        "world_bbox_mm": ({
            "min": dict(zip(("x", "y", "z"), np.round(np.min(world_corners, axis=0) * MM, 3).tolist())),
            "max": dict(zip(("x", "y", "z"), np.round(np.max(world_corners, axis=0) * MM, 3).tolist())),
        } if world_corners else None),
        "coordinate_notes": "Positions/world_bbox_mm are assembly-space mm. local_bbox_mm and connector local origins are part-space mm; axes are column vectors. World bounds enclose transformed part bounding boxes, not exact surfaces. bbox_size_mm is the legacy camera-fit envelope and ignores rotation.",
    }


def snapshot_assembly(asm) -> dict:
    if asm is None:
        return {}
    return {
        "component_ids": [c.id for c in (getattr(asm, "components", []) or [])],
        "mate_ids": [m.id for m in (getattr(asm, "mates", []) or [])],
        "bbox_mm": _bbox_mm(getattr(asm, "bbox", None)),
        "positions": {c.id: (round(c.transform.translate.x * MM, 3),
                             round(c.transform.translate.y * MM, 3),
                             round(c.transform.translate.z * MM, 3))
                      for c in (getattr(asm, "components", []) or [])},
    }


def assembly_delta(before: dict, after: dict) -> dict:
    """What one assembly operation changed.

    Deliberately the same shape of answer as `delta`: what was added, what was
    removed, and — the part that matters for a mate — **whether anything
    actually moved**. Adding a mate that positions nothing differently is the
    assembly-side version of an edit that reports success and does nothing.
    """
    if not before or not after:
        return {}
    before_c, after_c = before.get("component_ids") or [], after.get("component_ids") or []
    before_m, after_m = before.get("mate_ids") or [], after.get("mate_ids") or []
    moved = [cid for cid, pos in (after.get("positions") or {}).items()
             if cid in (before.get("positions") or {})
             and (before["positions"][cid] != pos)]
    return {
        "components": {"added": [c for c in after_c if c not in set(before_c)],
                       "removed": [c for c in before_c if c not in set(after_c)],
                       "count_before": len(before_c), "count_after": len(after_c)},
        "mates": {"added": [m for m in after_m if m not in set(before_m)],
                  "removed": [m for m in before_m if m not in set(after_m)]},
        "moved": moved,
        "bbox_mm": {"before": before.get("bbox_mm"), "after": after.get("bbox_mm")},
        "changed_placement": bool(moved) or before_c != after_c,
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

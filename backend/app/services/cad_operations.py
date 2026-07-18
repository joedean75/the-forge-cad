"""AI-native operation API: discrete, typed CAD operations over a document.

Both the UI and an agent (via REST or the MCP server) drive geometry through
this catalog rather than emitting whole trees. Each operation has a name, kind
(query/mutate), a description, and a JSON-schema-ish params block so the surface
is self-describing for LLM function-calling.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Dict, List

from app.services import document_service, feature_ops


# ── node builders ──

def _make_primitive(shape: str, params: Dict[str, Any] | None) -> Dict[str, Any]:
    p = params or {}
    center = p.get("center", {"x": 0, "y": 0, "z": 0})
    axis = p.get("axis", {"x": 0, "y": 0, "z": 1})
    if shape == "box":
        return {"type": "BoxFeature", "name": "Box",
                "params": {"size": p.get("size", {"x": 0.05, "y": 0.05, "z": 0.05}), "center": center}}
    if shape == "sphere":
        return {"type": "SphereFeature", "name": "Sphere",
                "params": {"radius": p.get("radius", 0.03), "center": center}}
    if shape == "cylinder":
        return {"type": "CylinderFeature", "name": "Cylinder",
                "params": {"radius": p.get("radius", 0.02), "height": p.get("height", 0.06),
                           "center": center, "axis": axis}}
    if shape == "cone":
        return {"type": "ConeFeature", "name": "Cone",
                "params": {"base_radius": p.get("base_radius", 0.03), "height": p.get("height", 0.06),
                           "center": center, "axis": axis}}
    raise ValueError(f"unknown shape: {shape}")


def _make_modifier(kind: str, params: Dict[str, Any] | None) -> Dict[str, Any]:
    p = params or {}
    axis = p.get("axis", {"x": 0, "y": 0, "z": 1})
    center = p.get("center", {"x": 0, "y": 0, "z": 0})
    table = {
        "fillet": ("FilletFeature", {"radius": p.get("radius", 0.01), "operation": p.get("operation", "union")}),
        "chamfer": ("ChamferFeature", {"radius": p.get("radius", 0.01), "operation": p.get("operation", "union")}),
        "shell": ("ShellFeature", {"thickness": p.get("thickness", 0.004)}),
        "hole": ("HoleFeature", {"radius": p.get("radius", 0.005), "depth": p.get("depth", 0.1),
                                 "center": center, "axis": axis}),
        "pattern": ("PatternFeature", {"kind": p.get("kind", "linear"), "count": p.get("count", 3),
                                       "spacing": p.get("spacing", 0.05), "axis": p.get("axis", {"x": 1, "y": 0, "z": 0}),
                                       "center": center}),
        "transform": ("TransformFeature", {"translate": p.get("translate", {"x": 0, "y": 0, "z": 0}),
                                           "rotate": p.get("rotate", {"x": 0, "y": 0, "z": 0}),
                                           "scale": p.get("scale", 1.0)}),
    }
    if kind not in table:
        raise ValueError(f"unknown modifier: {kind}")
    typ, mp = table[kind]
    return {"type": typ, "name": kind.capitalize(), "params": mp}


def _get(doc_id: str):
    doc = document_service.get_document(doc_id)
    if doc is None:
        raise KeyError(doc_id)
    return doc


# ── handlers ──

def _list_features(doc_id, args):
    doc = _get(doc_id)
    return {"features": [{"id": n.get("id"), "type": n["type"], "name": n.get("name")}
                         for n in feature_ops.flatten(doc.feature_tree)]}


def _get_feature(doc_id, args):
    doc = _get(doc_id)
    node = feature_ops.find_node(doc.feature_tree, args["feature_id"])
    if node is None:
        raise KeyError(args["feature_id"])
    return {"feature": node}


def _bounding_box(doc_id, args):
    doc = _get(doc_id)
    b = doc.derived.bbox
    if not b:
        return {"bbox": None}
    return {
        "size_mm": {"x": round((b.max.x - b.min.x) * 1000, 3),
                    "y": round((b.max.y - b.min.y) * 1000, 3),
                    "z": round((b.max.z - b.min.z) * 1000, 3)},
        "mass_kg": doc.derived.mass,
    }


def _measure(doc_id, args):
    a, b = args["a"], args["b"]
    d = math.dist([a["x"], a["y"], a["z"]], [b["x"], b["y"], b["z"]])
    return {"distance_mm": round(d * 1000, 4)}


def _add_primitive(doc_id, args):
    doc = _get(doc_id)
    prim = _make_primitive(args["shape"], args.get("params"))
    tree = feature_ops.add_primitive(doc.feature_tree, prim, args.get("operation", "union"))
    document_service.update_document(doc_id, feature_tree=tree)
    return {"message": f"added {args['shape']} ({args.get('operation', 'union')})", "feature_id": prim["id"]}


def _add_extrude(doc_id, args):
    doc = _get(doc_id)
    node = {
        "type": "ExtrudeFeature", "name": "Extrude",
        "params": {
            "profile": args.get("profile", {"type": "rect", "width": 0.05, "height": 0.03, "cx": 0.0, "cy": 0.0}),
            "plane": args.get("plane", "xy"),
            "offset": args.get("offset", 0.0),
            "depth": args.get("depth", 0.02),
        },
    }
    tree = feature_ops.add_primitive(doc.feature_tree, node, args.get("operation", "union"))
    document_service.update_document(doc_id, feature_tree=tree)
    return {"message": "added extrude", "feature_id": node["id"]}


def _resolve_on_face(tree, args, params):
    """If args carry an ``on_face`` ref, store it and bake an initial plane/offset.

    A symbolic sketch-on-face reference ({feature_id, face}) is resolved against
    the referenced feature's current geometry so the sketch follows it through
    later edits (see services/datum + feature_ops.resolve_datums).
    """
    from app.services import datum

    ref = args.get("on_face")
    if not isinstance(ref, dict) or not ref.get("feature_id"):
        return
    params["on_face"] = {"feature_id": ref["feature_id"], "face": ref.get("face", "top")}
    try:
        plane, offset = datum.resolve_face(tree, ref["feature_id"], ref.get("face", "top"))
        params["plane"] = plane
        params["offset"] = offset
    except (KeyError, ValueError):
        pass  # fall back to explicit plane/offset already in params


def _add_sketch(doc_id, args):
    from app.services import sketch_solver

    doc = _get(doc_id)
    spec = args["sketch"]
    solved = sketch_solver.solve_sketch(spec)
    polygon = sketch_solver.sketch_to_polygon(spec, solved)
    params = {
        "profile": {"type": "polygon", "points": polygon},
        "sketch": spec,  # keep the editable source so the sketch can be reopened
        "plane": args.get("plane", "xy"),
        "offset": args.get("offset", 0.0),
        "depth": args.get("depth", 0.02),
    }
    _resolve_on_face(doc.feature_tree, args, params)
    node = {"type": "ExtrudeFeature", "name": "Sketch", "params": params}
    tree = feature_ops.add_primitive(doc.feature_tree, node, args.get("operation", "union"))
    document_service.update_document(doc_id, feature_tree=tree)
    return {"message": "added sketch extrude", "feature_id": node["id"],
            "converged": solved["converged"], "residual": solved["residual"]}


def _update_sketch(doc_id, args):
    from app.services import sketch_solver

    doc = _get(doc_id)
    fid = args["feature_id"]
    node = feature_ops.find_node(doc.feature_tree, fid)
    if node is None or node.get("type") != "ExtrudeFeature":
        raise KeyError(fid)
    spec = args["sketch"]
    solved = sketch_solver.solve_sketch(spec)
    polygon = sketch_solver.sketch_to_polygon(spec, solved)
    params = {"profile": {"type": "polygon", "points": polygon}, "sketch": spec}
    for key in ("plane", "offset", "depth"):
        if key in args:
            params[key] = args[key]
    _resolve_on_face(doc.feature_tree, args, params)
    tree = feature_ops.set_params(doc.feature_tree, fid, params)
    document_service.update_document(doc_id, feature_tree=tree)
    return {"message": "updated sketch", "converged": solved["converged"], "residual": solved["residual"]}


def _add_modifier(doc_id, args):
    doc = _get(doc_id)
    mod = _make_modifier(args["modifier"], args.get("params"))
    tree = feature_ops.add_modifier(doc.feature_tree, mod)
    document_service.update_document(doc_id, feature_tree=tree)
    return {"message": f"added {args['modifier']}", "feature_id": mod["id"]}


def _set_feature_params(doc_id, args):
    doc = _get(doc_id)
    tree = feature_ops.set_params(doc.feature_tree, args["feature_id"], args["params"])
    document_service.update_document(doc_id, feature_tree=tree)
    return {"message": f"updated feature {args['feature_id']}"}


def _delete_feature(doc_id, args):
    doc = _get(doc_id)
    tree = feature_ops.remove_node(doc.feature_tree, args["feature_id"])
    document_service.update_document(doc_id, feature_tree=tree)
    return {"message": f"deleted feature {args['feature_id']}"}


def _add_dimension(doc_id, args):
    from app.models.schemas import AddDimensionRequest

    req = AddDimensionRequest(a=args["a"], b=args["b"], label=args.get("label"), source=args.get("source", "user"))
    dim = document_service.add_dimension(doc_id, req)
    if dim is None:
        raise KeyError(doc_id)
    return {"dimension": dim.model_dump()}


def _set_feature_suppressed(doc_id, args):
    doc = _get(doc_id)
    tree = feature_ops.set_suppressed(doc.feature_tree, args["feature_id"], bool(args.get("suppressed", True)))
    document_service.update_document(doc_id, feature_tree=tree)
    state = "suppressed" if args.get("suppressed", True) else "unsuppressed"
    return {"message": f"{state} feature {args['feature_id']}"}


def _reorder_feature(doc_id, args):
    doc = _get(doc_id)
    tree = feature_ops.reorder(doc.feature_tree, args["feature_id"], args.get("direction", "up"))
    document_service.update_document(doc_id, feature_tree=tree)
    return {"message": f"moved {args['feature_id']} {args.get('direction', 'up')}"}


def _ai_edit(doc_id, args):
    document_service.ai_edit_document(doc_id, args["instruction"])
    return {"message": "applied AI edit"}


def _slice_part(doc_id, args):
    """FDM-slice the body; stats come back, the G-code lands on the document."""
    import base64

    from app.models.schemas import CamResult
    from app.services import slicer

    settings = slicer.SliceSettings(**(args.get("settings") or {}))
    out = slicer.slice_document(doc_id, settings)
    doc = _get(doc_id)
    doc.derived.cam = CamResult(
        gcode_url="data:text/plain;base64," + base64.b64encode(out["gcode"].encode("utf-8")).decode("ascii"),
        operations=[f"fdm {settings.layer_height}mm/{settings.wall_count} walls/{int(settings.infill * 100)}% infill"],
        estimated_minutes=out["stats"]["estimated_minutes"],
    )
    stats = out["stats"]
    return {"message": f"sliced into {stats['layers']} layers "
                       f"(~{stats['estimated_minutes']} min, {stats['filament_g']} g filament)",
            "stats": stats}


def _print_part(doc_id, args):
    """Upload the sliced G-code to a Moonraker/Klipper printer (slice first if needed)."""
    import base64

    from app.services.machine import moonraker_client as mr
    from app.services.machine.url_guard import UrlNotAllowed, validate_url

    try:
        url = validate_url(args["printer_url"])
    except UrlNotAllowed as exc:
        raise ValueError(str(exc))

    doc = _get(doc_id)
    if doc.derived.cam is None or not doc.derived.cam.gcode_url:
        _slice_part(doc_id, args)  # defaults (or agent-passed settings)
        doc = _get(doc_id)
    _, b64 = doc.derived.cam.gcode_url.split(",", 1)
    gcode = base64.b64decode(b64).decode("utf-8")
    filename = f"forge_{(doc.name or 'part')[:32].replace(' ', '_')}.gcode"
    start = bool(args.get("start", False))
    try:
        mr.upload_gcode(url, filename, gcode, start=start)
    except mr.MoonrakerError:
        raise ValueError("printer request failed — is the Moonraker URL reachable?")
    return {"message": f"{'started printing' if start else 'uploaded'} {filename}",
            "filename": filename, "started": start}


@dataclass
class Operation:
    name: str
    kind: str  # "query" | "mutate"
    description: str
    params: Dict[str, Any]
    handler: Callable[[str, Dict[str, Any]], Dict[str, Any]]


_VEC = {"type": "object", "properties": {"x": {"type": "number"}, "y": {"type": "number"}, "z": {"type": "number"}}}

OPERATIONS: Dict[str, Operation] = {
    op.name: op for op in [
        Operation("list_features", "query", "List all features in the document (id, type, name).", {}, _list_features),
        Operation("get_feature", "query", "Get one feature's full definition.",
                  {"feature_id": {"type": "string"}}, _get_feature),
        Operation("bounding_box", "query", "Overall size (mm) and mass (kg) of the current body.", {}, _bounding_box),
        Operation("measure", "query", "Distance in mm between two points (metres).",
                  {"a": _VEC, "b": _VEC}, _measure),
        Operation("add_primitive", "mutate",
                  "Add a box/sphere/cylinder/cone, unioned or subtracted. Lengths in metres.",
                  {"shape": {"enum": ["box", "sphere", "cylinder", "cone"]},
                   "operation": {"enum": ["union", "subtract", "intersect"], "default": "union"},
                   "params": {"type": "object"}}, _add_primitive),
        Operation("add_modifier", "mutate",
                  "Apply a modifier to the whole body: fillet/chamfer/shell/hole/pattern/transform.",
                  {"modifier": {"enum": ["fillet", "chamfer", "shell", "hole", "pattern", "transform"]},
                   "params": {"type": "object"}}, _add_modifier),
        Operation("add_extrude", "mutate",
                  "Sketch a 2D profile on a plane and extrude it into a solid (unioned or subtracted).",
                  {"profile": {"type": "object", "description": "{type:rect|circle|polygon, ...} in metres"},
                   "plane": {"enum": ["xy", "xz", "yz"], "default": "xy"},
                   "offset": {"type": "number"}, "depth": {"type": "number"},
                   "operation": {"enum": ["union", "subtract", "intersect"], "default": "union"}}, _add_extrude),
        Operation("add_sketch", "mutate",
                  "Solve a constrained 2D sketch (points/lines/arcs/circles + constraints) and extrude the profile. "
                  "Pass on_face to place the sketch on another feature's face so it follows that feature through edits.",
                  {"sketch": {"type": "object", "description": "{points, lines, arcs, circles, constraints}"},
                   "plane": {"enum": ["xy", "xz", "yz"], "default": "xy"},
                   "depth": {"type": "number"}, "offset": {"type": "number"},
                   "on_face": {"type": "object",
                               "description": "{feature_id, face} face in top|bottom|left|right|front|back — overrides plane/offset, resolved live at regen"},
                   "operation": {"enum": ["union", "subtract", "intersect"], "default": "union"}}, _add_sketch),
        Operation("update_sketch", "mutate",
                  "Re-solve an existing sketch extrude with an edited sketch spec (and optional plane/offset/depth/on_face).",
                  {"feature_id": {"type": "string"}, "sketch": {"type": "object"},
                   "plane": {"enum": ["xy", "xz", "yz"]}, "depth": {"type": "number"}, "offset": {"type": "number"},
                   "on_face": {"type": "object", "description": "{feature_id, face} — see add_sketch"}},
                  _update_sketch),
        Operation("set_feature_params", "mutate", "Change a feature's parameters (merged). Lengths in metres.",
                  {"feature_id": {"type": "string"}, "params": {"type": "object"}}, _set_feature_params),
        Operation("delete_feature", "mutate", "Remove a feature by id.",
                  {"feature_id": {"type": "string"}}, _delete_feature),
        Operation("set_feature_suppressed", "mutate",
                  "Suppress or unsuppress a feature (skip it during regeneration without deleting).",
                  {"feature_id": {"type": "string"}, "suppressed": {"type": "boolean"}}, _set_feature_suppressed),
        Operation("reorder_feature", "mutate", "Move a feature up/down within its parent group.",
                  {"feature_id": {"type": "string"}, "direction": {"enum": ["up", "down"]}}, _reorder_feature),
        Operation("add_dimension", "mutate", "Add a point-to-point dimension (metres).",
                  {"a": _VEC, "b": _VEC, "label": {"type": "string"}}, _add_dimension),
        Operation("ai_edit", "mutate", "Apply a natural-language edit to the whole part.",
                  {"instruction": {"type": "string"}}, _ai_edit),
        Operation("slice_part", "query",
                  "Slice the body for FDM 3D printing (SDF slicer). Returns layer/time/filament "
                  "stats; the G-code is stored on the document for download or printing.",
                  {"settings": {"type": "object",
                                "description": "optional overrides: layer_height (mm), wall_count, "
                                               "infill (0..1), temp_nozzle, temp_bed, speed_print …"}},
                  _slice_part),
        Operation("print_part", "mutate",
                  "Upload the sliced G-code to a Moonraker/Klipper printer and optionally start it. "
                  "Slices with defaults first if the part hasn't been sliced.",
                  {"printer_url": {"type": "string", "description": "Moonraker base URL, e.g. http://printer.local:7125"},
                   "start": {"type": "boolean", "default": False},
                   "settings": {"type": "object", "description": "slice settings if slicing is needed"}},
                  _print_part),
    ]
}


def catalog() -> List[Dict[str, Any]]:
    return [{"name": o.name, "kind": o.kind, "description": o.description, "params": o.params}
            for o in OPERATIONS.values()]


def dispatch(op_name: str, doc_id: str, args: Dict[str, Any] | None) -> Dict[str, Any]:
    op = OPERATIONS.get(op_name)
    if op is None:
        raise ValueError(f"unknown operation: {op_name}")
    return op.handler(doc_id, args or {})

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



@dataclass
class Operation:
    name: str
    kind: str  # "query" | "mutate"
    description: str
    params: Dict[str, Any]
    handler: Callable[[str, Dict[str, Any]], Dict[str, Any]]


_VEC = {"type": "object", "properties": {"x": {"type": "number"}, "y": {"type": "number"}, "z": {"type": "number"}}}

# Populated by @operation as each handler is defined, so a new operation is one
# decorated function — the registry used to be a 180-line list sitting far from
# the handlers it named, which meant editing two distant places and kept the
# description away from the code it describes.
OPERATIONS: Dict[str, Operation] = {}


def operation(name: str, kind: str, description: str, params: Dict[str, Any]):
    """Register a handler as a callable operation.

    `kind` is "query" (read-only) or "mutate". `params` is the JSON-schema-ish
    shape published to agents in the catalog.
    """

    def decorate(fn):
        if name in OPERATIONS:
            raise ValueError(f"duplicate operation {name!r}")
        OPERATIONS[name] = Operation(name, kind, description, params, fn)
        return fn

    return decorate

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

@operation("list_features", "query", "List all features in the document (id, type, name).",
           {})
def _list_features(doc_id, args):
    doc = _get(doc_id)
    return {"features": [{"id": n.get("id"), "type": n["type"], "name": n.get("name")}
                         for n in feature_ops.flatten(doc.feature_tree)]}


@operation("get_feature", "query", "Get one feature's full definition.",
           {"feature_id": {"type": "string"}})
def _get_feature(doc_id, args):
    doc = _get(doc_id)
    node = feature_ops.find_node(doc.feature_tree, args["feature_id"])
    if node is None:
        raise KeyError(args["feature_id"])
    return {"feature": node}


@operation("bounding_box", "query", "Overall size (mm) and mass (kg) of the current body.",
           {})
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


@operation("measure", "query", "Distance in mm between two points (metres).",
           {"a": _VEC, "b": _VEC})
def _measure(doc_id, args):
    a, b = args["a"], args["b"]
    d = math.dist([a["x"], a["y"], a["z"]], [b["x"], b["y"], b["z"]])
    return {"distance_mm": round(d * 1000, 4)}


@operation("add_primitive", "mutate", "Add a box/sphere/cylinder/cone, unioned or subtracted. Lengths in metres.",
           {"shape": {"enum": ["box", "sphere", "cylinder", "cone"]},
           "operation": {"enum": ["union", "subtract", "intersect"], "default": "union"},
           "params": {"type": "object"}})
def _add_primitive(doc_id, args):
    doc = _get(doc_id)
    prim = _make_primitive(args["shape"], args.get("params"))
    tree = feature_ops.add_primitive(doc.feature_tree, prim, args.get("operation", "union"))
    document_service.update_document(doc_id, feature_tree=tree)
    return {"message": f"added {args['shape']} ({args.get('operation', 'union')})", "feature_id": prim["id"]}


@operation("add_extrude", "mutate", "Sketch a 2D profile on a plane and extrude it into a solid (unioned or subtracted).",
           {"profile": {"type": "object", "description": "{type:rect|circle|polygon, ...} in metres"},
           "plane": {"enum": ["xy", "xz", "yz"], "default": "xy"},
           "offset": {"type": "number"}, "depth": {"type": "number"},
           "operation": {"enum": ["union", "subtract", "intersect"], "default": "union"}})
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


@operation("add_revolve", "mutate", "Revolve a 2D profile (authored in radius/height, metres) about an axis to make a "
           "turned part — shaft, wheel, pulley, bottle. angle<360 = partial revolve.",
           {"profile": {"type": "object", "description": "{type:rect|circle|polygon} in (radius,height)"},
           "axis": {"enum": ["x", "y", "z"], "default": "z"}, "center": _VEC,
           "angle": {"type": "number", "default": 360},
           "operation": {"enum": ["union", "subtract", "intersect"], "default": "union"}})
def _add_revolve(doc_id, args):
    doc = _get(doc_id)
    node = {"type": "RevolveFeature", "name": "Revolve", "params": {
        "profile": args.get("profile", {"type": "rect", "width": 0.01, "height": 0.03, "cx": 0.02, "cy": 0.0}),
        "axis": args.get("axis", "z"), "center": args.get("center", {"x": 0, "y": 0, "z": 0}),
        "angle": args.get("angle", 360.0)}}
    tree = feature_ops.add_primitive(doc.feature_tree, node, args.get("operation", "union"))
    document_service.update_document(doc_id, feature_tree=tree)
    return {"message": "added revolve", "feature_id": node["id"]}


@operation("add_sweep", "mutate", "Sweep a round profile of 'radius' along a polyline 'path' (metres) — pipe, tube, wire, rail.",
           {"path": {"type": "array", "description": "[[x,y,z], …] in metres"},
           "radius": {"type": "number"},
           "operation": {"enum": ["union", "subtract", "intersect"], "default": "union"}})
def _add_sweep(doc_id, args):
    doc = _get(doc_id)
    node = {"type": "SweepFeature", "name": "Sweep", "params": {
        "path": args.get("path", [[0, 0, 0], [0, 0, 0.05]]), "radius": args.get("radius", 0.005)}}
    tree = feature_ops.add_primitive(doc.feature_tree, node, args.get("operation", "union"))
    document_service.update_document(doc_id, feature_tree=tree)
    return {"message": "added sweep", "feature_id": node["id"]}


@operation("add_loft", "mutate", "Loft between two profiles at two heights along a plane normal — adapters, tapers, funnels.",
           {"profile_a": {"type": "object"}, "profile_b": {"type": "object"},
           "plane": {"enum": ["xy", "xz", "yz"], "default": "xy"},
           "offset_a": {"type": "number"}, "offset_b": {"type": "number"},
           "operation": {"enum": ["union", "subtract", "intersect"], "default": "union"}})
def _add_loft(doc_id, args):
    doc = _get(doc_id)
    node = {"type": "LoftFeature", "name": "Loft", "params": {
        "profile_a": args.get("profile_a", {"type": "rect", "width": 0.04, "height": 0.04, "cx": 0, "cy": 0}),
        "profile_b": args.get("profile_b", {"type": "circle", "radius": 0.015, "cx": 0, "cy": 0}),
        "plane": args.get("plane", "xy"), "offset_a": args.get("offset_a", 0.0), "offset_b": args.get("offset_b", 0.05)}}
    tree = feature_ops.add_primitive(doc.feature_tree, node, args.get("operation", "union"))
    document_service.update_document(doc_id, feature_tree=tree)
    return {"message": "added loft", "feature_id": node["id"]}


@operation("add_sheet_metal", "mutate", "Create a sheet-metal base flange: a flat plate of constant thickness that edge "
           "flanges can be bent from. Lengths in metres. Optionally pass 'flanges' up front, "
           "each {edge: x+|x-|y+|y-, length, angle, radius}.",
           {"thickness": {"type": "number", "default": 0.0015},
           "length": {"type": "number", "default": 0.10},
           "width": {"type": "number", "default": 0.06},
           "bend_radius": {"type": "number", "default": 0.002,
           "description": "default inside bend radius"},
           "k_factor": {"type": "number", "default": 0.44,
           "description": "neutral-axis position, 0.33 hard / 0.44 soft"},
           "flanges": {"type": "array", "description": "[{edge, length, angle, radius}, …]"},
           "operation": {"enum": ["union", "subtract", "intersect"], "default": "union"}})
def _add_sheet_metal(doc_id, args):
    doc = _get(doc_id)
    node = {"type": "SheetMetalFeature", "name": "Sheet metal", "params": {
        "thickness": args.get("thickness", 0.0015),
        "length": args.get("length", 0.10),
        "width": args.get("width", 0.06),
        "bend_radius": args.get("bend_radius", 0.002),
        "k_factor": args.get("k_factor", 0.44),
        "flanges": args.get("flanges", [])}}
    tree = feature_ops.add_primitive(doc.feature_tree, node, args.get("operation", "union"))
    document_service.update_document(doc_id, feature_tree=tree)
    return {"message": "added sheet metal base flange", "feature_id": node["id"]}


@operation("add_flange", "mutate", "Bend a new edge flange off a sheet-metal body. Positive angle bends up, negative down.",
           {"edge": {"enum": ["x+", "x-", "y+", "y-"], "default": "x+"},
           "length": {"type": "number", "default": 0.02, "description": "flat leg length, metres"},
           "angle": {"type": "number", "default": 90.0, "description": "bend angle, degrees"},
           "radius": {"type": "number", "description": "inside radius (default: the part's)"},
           "feature_id": {"type": "string", "description": "sheet-metal feature (default: the first)"}})
def _add_flange(doc_id, args):
    """Attach an edge flange to an existing sheet-metal feature."""
    import copy

    doc = _get(doc_id)
    # Work on a copy so a rejected flange can't leave the stored tree half-edited.
    tree = copy.deepcopy(doc.feature_tree)
    target = args.get("feature_id")

    from app.cad_kernel.ops.sheet_metal import find_sheet_metal

    node = find_sheet_metal(tree, target)
    if node is None:
        raise ValueError("no sheet-metal feature found; create one with add_sheet_metal first")

    flange = {
        "edge": args.get("edge", "x+"),
        "length": args.get("length", 0.02),
        "angle": args.get("angle", 90.0),
    }
    if args.get("radius") is not None:
        flange["radius"] = args["radius"]
    node["params"].setdefault("flanges", []).append(flange)

    document_service.update_document(doc_id, feature_tree=tree)
    return {"message": f"added {flange['edge']} flange", "feature_id": node.get("id")}


def _sheet_metal_feature(doc, feature_id=None):
    """The document's sheet-metal feature, or a clear error if it has none."""
    from app.cad_kernel.ops.sheet_metal import SheetMetalFeature, find_sheet_metal

    node = find_sheet_metal(doc.feature_tree, feature_id)
    if node is None:
        raise ValueError("this document has no sheet-metal feature")
    return SheetMetalFeature._from_dict(node)


@operation("flat_pattern", "query", "Unfold the sheet-metal body: developed blank size, bend allowances and bend-line "
           "positions — what you need to cut and set up the brake.",
           {"feature_id": {"type": "string", "description": "sheet-metal feature (default: the first)"}})
def _flat_pattern(doc_id, args):
    """Developed blank, bend table and cut outline for the sheet-metal body."""
    doc = _get(doc_id)
    feature = _sheet_metal_feature(doc, args.get("feature_id"))

    pattern = feature.flat_pattern()
    # Report in mm as well — a brake setup sheet is never in metres.
    pattern["blank_size_mm"] = {k: round(v * 1000.0, 3) for k, v in pattern["blank_size"].items()}
    for bend in pattern["bends"]:
        bend["bend_allowance_mm"] = round(bend["bend_allowance"] * 1000.0, 3)
        bend["distance_from_edge_mm"] = round(bend["distance_from_edge"] * 1000.0, 3)
    # Cut geometry, so the blank is directly manufacturable rather than a number
    # someone has to redraw.
    pattern.update(feature.flat_pattern_outline())
    return pattern


@operation("add_mirror", "mutate", "Mirror the body (or a target feature) across a plane and union — symmetry in one op.",
           {"plane": {"enum": ["xy", "xz", "yz"], "default": "yz"}, "offset": {"type": "number"},
           "target": {"type": "string", "description": "feature id to mirror (default: whole body)"}})
def _add_mirror(doc_id, args):
    doc = _get(doc_id)
    mod = {"type": "MirrorFeature", "name": "Mirror",
           "params": {"plane": args.get("plane", "yz"), "offset": args.get("offset", 0.0)}}
    tree = feature_ops.add_modifier(doc.feature_tree, mod, target_id=args.get("target"))
    document_service.update_document(doc_id, feature_tree=tree)
    return {"message": "added mirror", "feature_id": mod["id"]}


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
        plane, offset, origin_uv = datum.resolve_face_frame(tree, ref["feature_id"], ref.get("face", "top"))
        params["plane"] = plane
        params["offset"] = offset
        params["plane_origin"] = list(origin_uv)  # zero the sketch on the face
    except (KeyError, ValueError):
        pass  # fall back to explicit plane/offset already in params


@operation("add_sketch", "mutate", "Solve a constrained 2D sketch (points/lines/arcs/circles + constraints) and extrude the profile. "
           "Pass on_face to place the sketch on another feature's face so it follows that feature through edits.",
           {"sketch": {"type": "object", "description": "{points, lines, arcs, circles, constraints}"},
           "plane": {"enum": ["xy", "xz", "yz"], "default": "xy"},
           "depth": {"type": "number"}, "offset": {"type": "number"},
           "on_face": {"type": "object",
           "description": "{feature_id, face} face in top|bottom|left|right|front|back — overrides plane/offset, resolved live at regen"},
           "operation": {"enum": ["union", "subtract", "intersect"], "default": "union"}})
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


@operation("update_sketch", "mutate", "Re-solve an existing sketch extrude with an edited sketch spec (and optional plane/offset/depth/on_face).",
           {"feature_id": {"type": "string"}, "sketch": {"type": "object"},
           "plane": {"enum": ["xy", "xz", "yz"]}, "depth": {"type": "number"}, "offset": {"type": "number"},
           "on_face": {"type": "object", "description": "{feature_id, face} — see add_sketch"}})
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


@operation("add_modifier", "mutate", "Apply a modifier: fillet/chamfer/shell/hole/pattern/transform. By default it wraps the "
           "whole body; pass 'target' (a feature id) to apply it to one feature — e.g. pattern a single hole.",
           {"modifier": {"enum": ["fillet", "chamfer", "shell", "hole", "pattern", "transform"]},
           "target": {"type": "string", "description": "feature id to modify (default: whole body)"},
           "params": {"type": "object"}})
def _add_modifier(doc_id, args):
    doc = _get(doc_id)
    mod = _make_modifier(args["modifier"], args.get("params"))
    tree = feature_ops.add_modifier(doc.feature_tree, mod, target_id=args.get("target"))
    document_service.update_document(doc_id, feature_tree=tree)
    return {"message": f"added {args['modifier']}", "feature_id": mod["id"]}


@operation("set_feature_params", "mutate", "Change a feature's parameters (merged). Lengths in metres.",
           {"feature_id": {"type": "string"}, "params": {"type": "object"}})
def _set_feature_params(doc_id, args):
    doc = _get(doc_id)
    tree = feature_ops.set_params(doc.feature_tree, args["feature_id"], args["params"])
    document_service.update_document(doc_id, feature_tree=tree)
    return {"message": f"updated feature {args['feature_id']}"}


@operation("delete_feature", "mutate", "Remove a feature by id.",
           {"feature_id": {"type": "string"}})
def _delete_feature(doc_id, args):
    doc = _get(doc_id)
    tree = feature_ops.remove_node(doc.feature_tree, args["feature_id"])
    document_service.update_document(doc_id, feature_tree=tree)
    return {"message": f"deleted feature {args['feature_id']}"}


@operation("add_dimension", "mutate", "Add a point-to-point dimension (metres).",
           {"a": _VEC, "b": _VEC, "label": {"type": "string"}})
def _add_dimension(doc_id, args):
    from app.models.schemas import AddDimensionRequest

    req = AddDimensionRequest(a=args["a"], b=args["b"], label=args.get("label"), source=args.get("source", "user"))
    dim = document_service.add_dimension(doc_id, req)
    if dim is None:
        raise KeyError(doc_id)
    return {"dimension": dim.model_dump()}


@operation("set_feature_suppressed", "mutate", "Suppress or unsuppress a feature (skip it during regeneration without deleting).",
           {"feature_id": {"type": "string"}, "suppressed": {"type": "boolean"}})
def _set_feature_suppressed(doc_id, args):
    doc = _get(doc_id)
    tree = feature_ops.set_suppressed(doc.feature_tree, args["feature_id"], bool(args.get("suppressed", True)))
    document_service.update_document(doc_id, feature_tree=tree)
    state = "suppressed" if args.get("suppressed", True) else "unsuppressed"
    return {"message": f"{state} feature {args['feature_id']}"}


@operation("set_feature_keep", "mutate", "Mark a feature as must-keep so generative optimization cannot remove it. Use this "
           "for anything the part needs regardless of load — mounting bosses, bearing seats, "
           "bolt holes. Its material is pinned solid and the optimizer routes load around it.",
           {"feature_id": {"type": "string"}, "keep": {"type": "boolean", "default": True}})
def _set_feature_keep(doc_id, args):
    doc = _get(doc_id)
    keep = bool(args.get("keep", True))
    tree = feature_ops.set_keep(doc.feature_tree, args["feature_id"], keep)
    # Flagging a keep changes no geometry, so this must not invalidate the solid;
    # persist the tree without forcing a re-evaluation.
    document_service.update_document(doc_id, feature_tree=tree)
    return {
        "message": f"{'keeping' if keep else 'no longer keeping'} feature {args['feature_id']}",
        "kept_features": [n.get("id") for n in feature_ops.kept_features(tree)],
    }


@operation("list_kept_features", "query", "List the features currently flagged must-keep for optimization.",
           {})
def _list_kept_features(doc_id, args):
    doc = _get(doc_id)
    return {"features": [{"id": n.get("id"), "type": n.get("type"), "name": n.get("name")}
                         for n in feature_ops.kept_features(doc.feature_tree)]}


@operation("reorder_feature", "mutate", "Move a feature up/down within its parent group.",
           {"feature_id": {"type": "string"}, "direction": {"enum": ["up", "down"]}})
def _reorder_feature(doc_id, args):
    doc = _get(doc_id)
    tree = feature_ops.reorder(doc.feature_tree, args["feature_id"], args.get("direction", "up"))
    document_service.update_document(doc_id, feature_tree=tree)
    return {"message": f"moved {args['feature_id']} {args.get('direction', 'up')}"}


@operation("ai_edit", "mutate", "Apply a natural-language edit to the whole part.",
           {"instruction": {"type": "string"}})
def _ai_edit(doc_id, args):
    document_service.ai_edit_document(doc_id, args["instruction"])
    return {"message": "applied AI edit"}


@operation("list_materials", "query", "List the engineering material catalog (density, modulus, yield, cost).",
           {})
def _list_materials(doc_id, args):
    from app.services import materials

    return {"materials": [m.model_dump() for m in materials.list_materials()]}


@operation("set_material", "mutate", "Assign an engineering material to the part (drives mass, sim, DFM, quotes).",
           {"material_id": {"type": "string", "description": "e.g. al-6061, steel-304, ti-6al4v, pla"}})
def _set_material(doc_id, args):
    doc = document_service.set_material(doc_id, args["material_id"])
    if doc is None:
        raise KeyError(doc_id)
    from app.services import materials

    m = materials.get_material(doc.material_id)
    return {"message": f"material set to {m.name}", "material_id": m.id, "mass_kg": doc.derived.mass}


@operation("set_parameters", "mutate", "Set named driving parameters/equations (width=100, hole_d=width/10). Feature params "
           "may reference them as expression strings ('=width/1000'). Lengths are mm.",
           {"parameters": {"type": "array",
           "description": "[{name, expr, unit: mm|deg|count|ratio}], ordered; later may use earlier"}})
def _set_parameters(doc_id, args):
    doc = document_service.set_parameters(doc_id, args.get("parameters") or [])
    if doc is None:
        raise KeyError(doc_id)
    return {"message": f"set {len(doc.parameters)} parameter(s)",
            "parameters": [p.model_dump() for p in doc.parameters],
            "error": doc.derived.error}


@operation("get_parameters", "query", "List the document's driving parameters/equations.",
           {})
def _get_parameters(doc_id, args):
    doc = _get(doc_id)
    return {"parameters": [p.model_dump() for p in doc.parameters]}


@operation("mass_properties", "query", "Volume, mass, center of mass, surface area and inertia tensor (uses the assigned material).",
           {})
def _mass_properties(doc_id, args):
    from app.services import mass_properties

    return mass_properties.mass_properties(doc_id)


@operation("profile_cut", "query", "2D laser/plasma/waterjet cut of the part outline from sheet, kerf-compensated. "
           "Fetch G-code at GET /api/documents/{id}/profile-cut.gcode.",
           {"kerf_mm": {"type": "number", "default": 0.2}, "plane": {"enum": ["xy", "xz", "yz"], "default": "xy"},
           "feed_mm_min": {"type": "number"}, "power": {"type": "number"}})
def _profile_cut(doc_id, args):
    from app.services.cam import profile_cut

    out = profile_cut.profile_cut(
        doc_id, kerf_mm=args.get("kerf_mm", 0.2), plane=args.get("plane", "xy"),
        feed_mm_min=args.get("feed_mm_min", 1200.0), power=args.get("power", 100.0))
    return {"message": f"cut {out['path_count']} contour(s), {out['cut_length_mm']}mm",
            "path_count": out["path_count"], "cut_length_mm": out["cut_length_mm"],
            "estimated_minutes": out["estimated_minutes"], "gcode_bytes": len(out["gcode"])}


@operation("generate_drawing", "query", "Generate a dimensioned multi-view 2D engineering drawing (front/top/right + section). "
           "Fetch the full SVG at GET /api/documents/{id}/drawing.svg.",
           {"section": {"type": "boolean", "default": True}})
def _generate_drawing(doc_id, args):
    from app.services import drawing

    out = drawing.generate_drawing(doc_id, section=args.get("section", True))
    # Keep the op result light for agents; the full SVG is fetched via the endpoint.
    return {"message": "generated 2D drawing", "views": out["views"],
            "bbox_mm": out["bbox_mm"], "material": out["material"], "svg_bytes": len(out["svg"])}


@operation("compute_fit", "query", "ISO-286 fit/tolerance limits for a nominal size. 'fit' is a single spec ('H7') "
           "or a hole/shaft fit ('H7/g6' → clearance range + classification).",
           {"nominal_mm": {"type": "number"}, "fit": {"type": "string"}})
def _compute_fit(doc_id, args):
    from app.services import tolerances

    nominal = float(args["nominal_mm"])
    code = str(args["fit"])
    if "/" in code:
        return tolerances.fit(nominal, code)
    return tolerances.limits_mm(nominal, code)


@operation("set_tolerances", "mutate", "Set the drawing's general tolerance (ISO 2768 f|m|c|v), fit/± callouts and "
           "GD&T feature-control frames.",
           {"general_tolerance": {"type": "string"},
           "tolerances": {"type": "array", "description": "[{label, nominal_mm, kind:fit|pm, spec, plus, minus}]"},
           "gdt": {"type": "array", "description": "[{feature, symbol, tolerance_mm, datums:[]}]"}})
def _set_tolerances(doc_id, args):
    doc = document_service.set_tolerances(
        doc_id, args.get("general_tolerance"), args.get("tolerances") or [], args.get("gdt") or [])
    if doc is None:
        raise KeyError(doc_id)
    return {"message": f"set {len(doc.tolerances)} tolerance callout(s), {len(doc.gdt)} GD&T frame(s)",
            "general_tolerance": doc.general_tolerance}


@operation("release_revision", "mutate", "Cut an immutable released revision (next letter A/B/C…) from the current geometry.",
           {"notes": {"type": "string"}, "label": {"type": "string"}})
def _release_revision(doc_id, args):
    rev = document_service.release_revision(doc_id, notes=args.get("notes", ""), label=args.get("label", ""))
    if rev is None:
        raise KeyError(doc_id)
    return {"message": f"released revision {rev.letter}", "letter": rev.letter,
            "state": rev.state, "released_at": rev.released_at}


@operation("set_lifecycle_state", "mutate", "Set the document lifecycle state: draft | in_review | released | obsolete.",
           {"state": {"type": "string"}})
def _set_lifecycle_state(doc_id, args):
    doc = document_service.set_lifecycle_state(doc_id, args["state"])
    if doc is None:
        raise KeyError(doc_id)
    return {"message": f"state = {doc.lifecycle_state}", "lifecycle_state": doc.lifecycle_state}


@operation("list_revisions", "query", "Lifecycle state and released revisions (letters).",
           {})
def _list_revisions(doc_id, args):
    doc = _get(doc_id)
    return {"lifecycle_state": doc.lifecycle_state,
            "revisions": [{"letter": r.letter, "label": r.label, "state": r.state,
                           "released_at": r.released_at, "notes": r.notes} for r in doc.revisions]}


@operation("set_configurations", "mutate", "Define part-family variants that override driving parameters, e.g. "
           "[{name:'M4', overrides:{bore:'4'}}]. Pass 'active' to switch to one.",
           {"configurations": {"type": "array", "description": "[{name, overrides:{param: expr}}]"},
           "active": {"type": "string"}})
def _set_configurations(doc_id, args):
    doc = document_service.set_configurations(doc_id, args.get("configurations") or [], args.get("active"))
    if doc is None:
        raise KeyError(doc_id)
    return {"message": f"set {len(doc.configurations)} configuration(s)",
            "configurations": [c.model_dump() for c in doc.configurations],
            "active": doc.active_configuration}


@operation("activate_configuration", "mutate", "Switch the live variant by name (empty = base parameters); geometry re-evaluates.",
           {"name": {"type": "string"}})
def _activate_configuration(doc_id, args):
    doc = document_service.activate_configuration(doc_id, args.get("name"))
    if doc is None:
        raise KeyError(doc_id)
    return {"message": f"activated {doc.active_configuration or 'base'}",
            "active": doc.active_configuration,
            "bbox": _bbox_summary(doc)}


@operation("list_configurations", "query", "List the document's variants and the active one.",
           {})
def _list_configurations(doc_id, args):
    doc = _get(doc_id)
    return {"configurations": [c.model_dump() for c in doc.configurations],
            "active": doc.active_configuration}


def _bbox_summary(doc) -> dict:
    b = doc.derived.bbox
    if b is None:
        return {}
    return {"x": round((b.max.x - b.min.x) * 1000, 2),
            "y": round((b.max.y - b.min.y) * 1000, 2),
            "z": round((b.max.z - b.min.z) * 1000, 2)}


@operation("slice_part", "query", "Slice the body for FDM 3D printing (SDF slicer). Returns layer/time/filament "
           "stats; the G-code is stored on the document for download or printing.",
           {"settings": {"type": "object",
           "description": "optional overrides: layer_height (mm), wall_count, "
           "infill (0..1), temp_nozzle, temp_bed, speed_print …"}})
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


@operation("print_part", "mutate", "Upload the sliced G-code to a Moonraker/Klipper printer and optionally start it. "
           "Slices with defaults first if the part hasn't been sliced.",
           {"printer_url": {"type": "string", "description": "Moonraker base URL, e.g. http://printer.local:7125"},
           "start": {"type": "boolean", "default": False},
           "settings": {"type": "object", "description": "slice settings if slicing is needed"}})
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





def catalog() -> List[Dict[str, Any]]:
    return [{"name": o.name, "kind": o.kind, "description": o.description, "params": o.params}
            for o in OPERATIONS.values()]


def dispatch(op_name: str, doc_id: str, args: Dict[str, Any] | None) -> Dict[str, Any]:
    op = OPERATIONS.get(op_name)
    if op is None:
        raise ValueError(f"unknown operation: {op_name}")
    return op.handler(doc_id, args or {})

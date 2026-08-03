"""AI-native operation API: discrete, typed CAD operations over a document.

Both the UI and an agent (via REST or the MCP server) drive geometry through
this catalog rather than emitting whole trees. Each operation has a name, kind
(query/mutate), a description, and a parameter block that ``services.op_schema``
turns into strict JSON Schema — so the surface is not merely self-describing but
machine-checkable: arguments are validated before a handler runs, and a
constrained decoder can be pointed straight at it.
"""

from __future__ import annotations

import copy
import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List

from app.services import document_service, feature_ops
from app.services.op_errors import (
    BadArgument,
    EvaluationFailed,
    InternalError,
    MissingArgument,
    NoSuchDocument,
    NoSuchFeature,
    OperationError,
    PreconditionFailed,
    UnknownOperation,
    require,
)

_LOGGER = logging.getLogger(__name__)



@dataclass
class Operation:
    name: str
    kind: str  # "query" | "mutate"
    description: str
    params: Dict[str, Any]
    handler: Callable[[str, Dict[str, Any]], Dict[str, Any]]


_VEC = {"type": "object", "properties": {"x": {"type": "number"}, "y": {"type": "number"}, "z": {"type": "number"}}}
_REQ_VEC = {**_VEC, "required": True}

# `add_primitive` and `add_modifier` used to declare `params: {"type": "object"}`
# — an opaque blob whose real shape lived only in prose. That is the one thing a
# constrained decoder cannot work with and the likeliest place for a model to
# invent key names. The union of every shape's / modifier's keys is spelled out
# instead, with the description saying which ones apply where. Handlers still
# apply their own per-shape defaults; this is about naming the vocabulary.
_PRIMITIVE_PARAMS = {
    "type": "object",
    "description": "shape dimensions in metres; which keys apply depends on `shape`",
    "properties": {
        "size": {**_VEC, "description": "box only: full extents"},
        "radius": {"type": "number", "description": "sphere / cylinder"},
        "base_radius": {"type": "number", "description": "cone"},
        "height": {"type": "number", "description": "cylinder / cone"},
        "center": {**_VEC, "description": "all shapes; defaults to the origin"},
        "axis": {**_VEC, "description": "cylinder / cone; defaults to +Z"},
    },
    "additionalProperties": False,
}

_MODIFIER_TARGET = {"type": "string",
                    "description": "feature id to modify (default: the whole body)"}

_EDGE_SELECTION = {
    "type": ["array", "string"], "required": True,
    "description": 'edge ids from list_edges, or the string "all" for every edge',
}

_MODIFIER_PARAMS = {
    "type": "object",
    "description": "modifier settings in metres; which keys apply depends on `modifier`",
    "properties": {
        "radius": {"type": "number",
                   "description": "fillet radius / chamfer setback / hole radius"},
        "thickness": {"type": "number", "description": "shell"},
        "depth": {"type": "number", "description": "hole"},
        "count": {"type": "integer", "description": "pattern"},
        "spacing": {"type": "number", "description": "pattern"},
        "kind": {"enum": ["linear", "circular"], "description": "pattern"},
        "translate": {**_VEC, "description": "transform"},
        "rotate": {**_VEC, "description": "transform: XYZ euler degrees"},
        "scale": {"type": "number", "description": "transform"},
        "center": {**_VEC, "description": "hole / pattern"},
        "axis": {**_VEC, "description": "hole / pattern"},
    },
    "additionalProperties": False,
}

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

def _one_of(args: Dict[str, Any], names: List[str], *, op: str, what: str) -> None:
    """Refuse a call that names no geometry to work from.

    A schema `required` list cannot say "profile *or* sketch", so this is a
    domain check — the kind `op_schema.validate`'s docstring reserves for the
    handler. It exists because the alternative was worse than a missing
    argument: `add_extrude {"depth": 0.008}` invented a 50 x 30 mm rectangle and
    returned `ok: true`. A default is the answer a caller would have chosen; a
    rectangle nobody mentioned is not a default, it is an invention reported as
    a success.
    """
    if not any(args.get(name) is not None for name in names):
        raise MissingArgument(
            f"{op} needs {what}: pass "
            + " or ".join(f"'{n}'" for n in names)
            + ".", arg=names[0])


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
    raise BadArgument(f"unknown shape: {shape}", arg="shape")


def _make_modifier(kind: str, params: Dict[str, Any] | None) -> Dict[str, Any]:
    p = params or {}
    axis = p.get("axis", {"x": 0, "y": 0, "z": 1})
    center = p.get("center", {"x": 0, "y": 0, "z": 0})
    # fillet/chamfer are absent: `_add_modifier` routes them to the exact edge
    # treatments before reaching here, because the smooth-boolean features this
    # table used to build are inert with a single child.
    table = {
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
        raise BadArgument(f"unknown modifier: {kind}", arg="modifier")
    typ, mp = table[kind]
    return {"type": typ, "name": kind.capitalize(), "params": mp}


def _get(doc_id: str):
    doc = document_service.get_document(doc_id)
    if doc is None:
        raise NoSuchDocument(f"no document {doc_id!r}")
    return doc


# ── handlers ──

@operation("list_features", "query", "List all features in the document (id, type, name).",
           {})
def _list_features(doc_id, args):
    doc = _get(doc_id)
    return {"features": [{"id": n.get("id"), "type": n["type"], "name": n.get("name")}
                         for n in feature_ops.flatten(doc.feature_tree)]}


@operation("get_feature", "query", "Get one feature's full definition.",
           {"feature_id": {"type": "string", "required": True,
            "description": "id of the feature, from list_features"}})
def _get_feature(doc_id, args):
    doc = _get(doc_id)
    fid = require(args, "feature_id")
    node = feature_ops.find_node(doc.feature_tree, fid)
    if node is None:
        raise NoSuchFeature(f"no feature {fid!r}", arg="feature_id")
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
           {"a": _REQ_VEC, "b": _REQ_VEC})
def _measure(doc_id, args):
    a, b = require(args, "a"), require(args, "b")
    d = math.dist([a["x"], a["y"], a["z"]], [b["x"], b["y"], b["z"]])
    return {"distance_mm": round(d * 1000, 4)}


_COMBINE_TARGET = {
    "type": "string",
    "description": "combine with this feature alone instead of with the whole body — "
                   "how you cut a hole through a boss without cutting the plate under it",
}


def _combined(doc_id, node, args, what: str) -> Dict[str, Any]:
    """Add a body to the part, optionally against one feature, and report both ids.

    ``group_id`` is the boolean the edit created or extended. A caller that wants
    to keep building on the *combination* — a third body joining the same group,
    a modifier over the pair — needs that, and it is not the same thing as the
    id of the body just added.
    """
    doc = _get(doc_id)
    operation = args.get("operation", "union")
    target = args.get("target")
    tree = feature_ops.add_primitive(doc.feature_tree, node, operation, target_id=target)
    document_service.update_document(doc_id, feature_tree=tree)
    parent = feature_ops.parent_of(tree, node["id"])
    return {"message": f"added {what} ({operation})", "feature_id": node["id"],
            **({"group_id": parent["id"]} if parent and parent.get("id") else {})}


@operation("add_primitive", "mutate", "Add a box/sphere/cylinder/cone, unioned or subtracted. Lengths in metres. "
           "Combines with the whole body unless 'target' names one feature to combine with instead.",
           {"shape": {"enum": ["box", "sphere", "cylinder", "cone"], "required": True,
            "description": "which primitive to build; it decides which `params` keys apply"},
           "operation": {"enum": ["union", "subtract", "intersect"], "default": "union",
            "description": "how the new body joins the part: add it, cut it away, or keep only the overlap"},
           "target": _COMBINE_TARGET,
           "params": _PRIMITIVE_PARAMS})
def _add_primitive(doc_id, args):
    prim = _make_primitive(require(args, "shape"), args.get("params"))
    return _combined(doc_id, prim, args, str(args["shape"]))


_SKETCH_REF = {"type": "string",
               "description": "a SketchFeature id from create_sketch, in place of 'profile'. "
                              "Several features can read one sketch; editing it moves all of them"}


def _sketch_reference(doc_id: str, args: dict, params: dict, ref_key: str,
                      arg_key: str = "sketch") -> None:
    """Bind a consumer to a sketch feature, if one was named.

    The profile is substituted at evaluation time (`feature_ops.resolve_sketches`)
    rather than copied here, which is what makes the sharing real: a copy would
    drift the moment the sketch was edited, and nothing would notice.
    """
    sketch_id = args.get(arg_key)
    if not sketch_id:
        return
    node = feature_ops.find_node(_get(doc_id).feature_tree, sketch_id)
    if node is None or node.get("type") != feature_ops.SKETCH_TYPE:
        raise NoSuchFeature(f"no sketch feature {sketch_id!r} — create_sketch makes one",
                            arg=arg_key)
    params[ref_key] = sketch_id


@operation("add_extrude", "mutate", "Sketch a 2D profile on a plane and extrude it into a solid (unioned or subtracted). "
           "Pass 'sketch' with a create_sketch feature id instead of 'profile' to extrude a shared, constrained sketch.",
           {"profile": {"type": "object", "description": "{type:rect|circle|polygon|loops, ...} in metres. 'loops' is the sketch form: {loops:[{curves:[{kind:line|arc|circle, ...}]}]}, nested loops read even-odd so an inner loop is a hole"},
           "sketch": _SKETCH_REF,
           "plane": {"enum": ["xy", "xz", "yz"], "default": "xy",
            "description": "the base plane the 2D profile is drawn on"},
           "offset": {"type": "number"}, "depth": {"type": "number"},
           "operation": {"enum": ["union", "subtract", "intersect"], "default": "union",
            "description": "how the new body joins the part: add it, cut it away, or keep only the overlap"},
           "target": _COMBINE_TARGET})
def _add_extrude(doc_id, args):
    doc = _get(doc_id)
    _one_of(args, ["profile", "sketch"], op="add_extrude",
            what="a 2D profile to extrude")
    params = {
        "profile": args.get("profile", {"type": "rect", "width": 0.05, "height": 0.03, "cx": 0.0, "cy": 0.0}),
        "plane": args.get("plane", "xy"),
        "offset": args.get("offset", 0.0),
        "depth": args.get("depth", 0.02),
    }
    _sketch_reference(doc_id, args, params, "sketch_ref")
    node = {"type": "ExtrudeFeature", "name": "Extrude", "params": params}
    return _combined(doc_id, node, args, "extrude")


@operation("add_revolve", "mutate", "Revolve a 2D profile (authored in radius/height, metres) about an axis to make a "
           "turned part — shaft, wheel, pulley, bottle. angle<360 = partial revolve.",
           {"profile": {"type": "object", "description": "{type:rect|circle|polygon|loops} in (radius,height)"},
           "sketch": _SKETCH_REF,
           "axis": {"enum": ["x", "y", "z"], "default": "z",
            "description": "the axis the profile is revolved about"}, "center": _VEC,
           "angle": {"type": "number", "default": 360},
           "operation": {"enum": ["union", "subtract", "intersect"], "default": "union",
            "description": "how the new body joins the part: add it, cut it away, or keep only the overlap"},
           "target": _COMBINE_TARGET})
def _add_revolve(doc_id, args):
    doc = _get(doc_id)
    _one_of(args, ["profile", "sketch"], op="add_revolve",
            what="a 2D profile in (radius, height) to revolve")
    params = {
        "profile": args.get("profile", {"type": "rect", "width": 0.01, "height": 0.03, "cx": 0.02, "cy": 0.0}),
        "axis": args.get("axis", "z"), "center": args.get("center", {"x": 0, "y": 0, "z": 0}),
        "angle": args.get("angle", 360.0)}
    _sketch_reference(doc_id, args, params, "sketch_ref")
    node = {"type": "RevolveFeature", "name": "Revolve", "params": params}
    return _combined(doc_id, node, args, "revolve")


@operation("add_sweep", "mutate", "Sweep a round profile of 'radius' along a polyline 'path' (metres) — pipe, tube, wire, rail.",
           {"path": {"type": "array", "description": "[[x,y,z], …] in metres"},
           "radius": {"type": "number"},
           "operation": {"enum": ["union", "subtract", "intersect"], "default": "union",
            "description": "how the new body joins the part: add it, cut it away, or keep only the overlap"},
           "target": _COMBINE_TARGET})
def _add_sweep(doc_id, args):
    doc = _get(doc_id)
    _one_of(args, ["path"], op="add_sweep", what="a polyline to sweep along")
    node = {"type": "SweepFeature", "name": "Sweep", "params": {
        "path": args.get("path", [[0, 0, 0], [0, 0, 0.05]]), "radius": args.get("radius", 0.005)}}
    return _combined(doc_id, node, args, "sweep")


@operation("add_loft", "mutate", "Loft between two profiles at two heights along a plane normal — adapters, tapers, funnels.",
           {"profile_a": {"type": "object", "description": "the start profile — same shapes as add_extrude"},
           "profile_b": {"type": "object", "description": "the end profile — same shapes as add_extrude"},
           "sketch_a": _SKETCH_REF, "sketch_b": _SKETCH_REF,
           "plane": {"enum": ["xy", "xz", "yz"], "default": "xy",
            "description": "the base plane the 2D profile is drawn on"},
           "offset_a": {"type": "number"}, "offset_b": {"type": "number"},
           "operation": {"enum": ["union", "subtract", "intersect"], "default": "union",
            "description": "how the new body joins the part: add it, cut it away, or keep only the overlap"},
           "target": _COMBINE_TARGET})
def _add_loft(doc_id, args):
    doc = _get(doc_id)
    _one_of(args, ["profile_a", "sketch_a"], op="add_loft", what="a start profile")
    _one_of(args, ["profile_b", "sketch_b"], op="add_loft", what="an end profile")
    params = {
        "profile_a": args.get("profile_a", {"type": "rect", "width": 0.04, "height": 0.04, "cx": 0, "cy": 0}),
        "profile_b": args.get("profile_b", {"type": "circle", "radius": 0.015, "cx": 0, "cy": 0}),
        "plane": args.get("plane", "xy"), "offset_a": args.get("offset_a", 0.0),
        "offset_b": args.get("offset_b", 0.05)}
    _sketch_reference(doc_id, args, params, "sketch_ref_a", "sketch_a")
    _sketch_reference(doc_id, args, params, "sketch_ref_b", "sketch_b")
    node = {"type": "LoftFeature", "name": "Loft", "params": params}
    return _combined(doc_id, node, args, "loft")


@operation("add_ruled_loft", "mutate", "Loft between two profiles with a real ruled surface — straight lines between "
           "the two rims, which is what every CAD system means by 'loft'. Distinct from "
           "add_loft, whose distance-field blend bulges at the waist: they are different "
           "shapes, both useful. Set ruled=false for a smooth surface through the sections. "
           "Requires the exact kernel.",
           {"profile_a": {"type": "object", "description": "the start profile — same shapes as add_extrude"},
           "profile_b": {"type": "object", "description": "the end profile — same shapes as add_extrude"},
           "sketch_a": _SKETCH_REF, "sketch_b": _SKETCH_REF,
           "plane": {"enum": ["xy", "xz", "yz"], "default": "xy",
            "description": "the base plane the 2D profile is drawn on"},
           "offset_a": {"type": "number"}, "offset_b": {"type": "number"},
           "ruled": {"type": "boolean", "default": True,
           "description": "straight rulings between the sections; false blends smoothly"},
           "operation": {"enum": ["union", "subtract", "intersect"], "default": "union",
            "description": "how the new body joins the part: add it, cut it away, or keep only the overlap"},
           "target": _COMBINE_TARGET})
def _add_ruled_loft(doc_id, args):
    doc = _get(doc_id)
    _one_of(args, ["profile_a", "sketch_a"], op="add_ruled_loft", what="a start profile")
    _one_of(args, ["profile_b", "sketch_b"], op="add_ruled_loft", what="an end profile")
    params = {
        "profile_a": args.get("profile_a", {"type": "rect", "width": 0.04, "height": 0.04, "cx": 0, "cy": 0}),
        "profile_b": args.get("profile_b", {"type": "circle", "radius": 0.015, "cx": 0, "cy": 0}),
        "plane": args.get("plane", "xy"),
        "offset_a": args.get("offset_a", 0.0), "offset_b": args.get("offset_b", 0.05),
        "ruled": bool(args.get("ruled", True))}
    _sketch_reference(doc_id, args, params, "sketch_ref_a", "sketch_a")
    _sketch_reference(doc_id, args, params, "sketch_ref_b", "sketch_b")
    node = {"type": "RuledLoftFeature", "name": "Ruled loft", "params": params}
    previous = doc.feature_tree
    tree = feature_ops.add_primitive(previous, node, args.get("operation", "union"),
                                     target_id=args.get("target"))
    _apply_exact(doc_id, previous, tree, "a ruled loft between those profiles")
    return {"message": "added ruled loft", "feature_id": node["id"]}


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
           "operation": {"enum": ["union", "subtract", "intersect"], "default": "union",
            "description": "how the new body joins the part: add it, cut it away, or keep only the overlap"},
           "target": _COMBINE_TARGET})
def _add_sheet_metal(doc_id, args):
    doc = _get(doc_id)
    node = {"type": "SheetMetalFeature", "name": "Sheet metal", "params": {
        "thickness": args.get("thickness", 0.0015),
        "length": args.get("length", 0.10),
        "width": args.get("width", 0.06),
        "bend_radius": args.get("bend_radius", 0.002),
        "k_factor": args.get("k_factor", 0.44),
        "flanges": args.get("flanges", [])}}
    return _combined(doc_id, node, args, "sheet metal base flange")


@operation("add_flange", "mutate", "Bend a new edge flange off a sheet-metal body. Positive angle bends up, negative down.",
           {"edge": {"enum": ["x+", "x-", "y+", "y-"], "default": "x+",
            "description": "which edge of the base flange to bend"},
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
        raise PreconditionFailed("no sheet-metal feature found; create one with add_sheet_metal first")

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
        raise PreconditionFailed("this document has no sheet-metal feature")
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
           {"plane": {"enum": ["xy", "xz", "yz"], "default": "yz",
            "description": "the mirror plane"}, "offset": {"type": "number"},
           "target": {"type": "string", "description": "feature id to mirror (default: whole body)"}})
def _add_mirror(doc_id, args):
    doc = _get(doc_id)
    mod = {"type": "MirrorFeature", "name": "Mirror",
           "params": {"plane": args.get("plane", "yz"), "offset": args.get("offset", 0.0)}}
    tree = feature_ops.add_modifier(doc.feature_tree, mod, target_id=args.get("target"))
    document_service.update_document(doc_id, feature_tree=tree)
    return {"message": "added mirror", "feature_id": mod["id"]}


def _face_reference(tree, feature_id: str, face_id: str) -> dict:
    """The stored descriptor for one of a feature's faces, by id."""
    from app.services import datum

    try:
        _, table = datum.feature_faces(tree, feature_id)
    except KeyError as exc:
        raise BadArgument(str(exc), arg="on_face")
    for descriptor in table:
        if descriptor["id"] == face_id:
            return descriptor
    raise BadArgument(
        f"no face {face_id!r} on feature {feature_id!r}. Call list_faces with "
        f"feature={feature_id!r} for the {len(table)} it has.", arg="on_face")


def _resolve_on_face(tree, args, params):
    """If args carry an ``on_face`` ref, store it and bake an initial plane/offset.

    Two forms, and which one you get depends on what the body can support.
    ``{feature_id, face}`` is symbolic — "the top face of f1" — resolved against
    that feature's live geometry, and the only thing that works when the body has
    no exact form. ``{feature_id, face_id}`` names a real face from `list_faces`,
    resolved through the Sprint-8 naming scheme, so it follows an edit that
    re-topologises the body and reports the face as gone rather than sliding onto
    a neighbour. Both are re-resolved at every regen by
    `feature_ops.resolve_datums`.
    """
    from app.services import datum

    ref = args.get("on_face")
    if not isinstance(ref, dict) or not ref.get("feature_id"):
        return
    if ref.get("face_id"):
        # The whole descriptor is stored, not the id — see `_selected`: an id is a
        # hash of the face's current measurements and changes when an upstream
        # dimension moves it, which is precisely the edit this must survive.
        descriptor = _face_reference(tree, ref["feature_id"], ref["face_id"])
        params["on_face"] = {"feature_id": ref["feature_id"],
                             "face_id": ref["face_id"], "face_ref": descriptor}
        try:
            plane, offset, origin_uv, _ = datum.resolve_face_id_frame(
                tree, ref["feature_id"], descriptor)
        except (KeyError, ValueError) as exc:
            # Authoring-time only: a face id that resolves to nothing right now is
            # a mistake in the call, not the later "it used to be there" case.
            raise BadArgument(str(exc), arg="on_face")
    else:
        params["on_face"] = {"feature_id": ref["feature_id"], "face": ref.get("face", "top")}
        try:
            plane, offset, origin_uv = datum.resolve_face_frame(
                tree, ref["feature_id"], ref.get("face", "top"))
        except (KeyError, ValueError):
            return  # fall back to explicit plane/offset already in params
    params["plane"] = plane
    params["offset"] = offset
    params["plane_origin"] = list(origin_uv)  # zero the sketch on the face


_SKETCH_ARG = {
    "type": "object", "required": True,
    "description": "{points, lines, arcs, circles, constraints}. Constraints: fixed, "
                   "coincident, horizontal, vertical, distance, parallel, perpendicular, "
                   "equal_length, radius, point_on_line, arc_radius, tangent, concentric, "
                   "symmetric, angle, midpoint, equal_radius, point_on_circle",
}


def _solved_sketch(spec: dict) -> dict:
    """Solve, and refuse only the one state that is genuinely broken.

    `under_constrained` is the normal state of a sketch someone is still drawing,
    and `over_constrained` is consistent — refusing either would make the canvas
    unusable. `conflicting` means the constraints cannot all hold, so whatever
    body came out would be a quietly wrong one; that is a typed failure naming
    the constraints that are fighting.
    """
    from app.services import sketch_solver

    solved = sketch_solver.solve_sketch(spec)
    if solved["status"] == "conflicting":
        raise BadArgument(solved["message"], arg="sketch")
    return solved


def _sketch_result(solved: dict) -> dict:
    """The solver facts an operation reports back.

    `converged`/`residual` are kept because callers read them, but they are not
    the answer on their own — an unconstrained sketch converges trivially.
    """
    return {"converged": solved["converged"], "residual": solved["residual"],
            "status": solved["status"], "dof": solved["dof"],
            "redundant": solved["redundant"],
            "redundant_groups": solved["redundant_groups"],
            "solver_message": solved["message"]}


@operation("add_sketch", "mutate", "Solve a constrained 2D sketch (points/lines/arcs/circles + constraints) and extrude the profile. "
           "Arcs and circles reach the kernel as curves, so a sketched bore is a real cylindrical face. "
           "Reports status (well_constrained|under_constrained|over_constrained) and remaining dof; "
           "constraints that cannot all hold are refused as BAD_ARG naming them. "
           "Pass on_face to place the sketch on another feature's face so it follows that feature through edits.",
           {"sketch": _SKETCH_ARG,
           "plane": {"enum": ["xy", "xz", "yz"], "default": "xy",
            "description": "the base plane the 2D profile is drawn on"},
           "depth": {"type": "number"}, "offset": {"type": "number"},
           "on_face": {"type": "object",
           "description": "{feature_id, face} face in top|bottom|left|right|front|back — overrides plane/offset, resolved live at regen"},
           "operation": {"enum": ["union", "subtract", "intersect"], "default": "union",
            "description": "how the new body joins the part: add it, cut it away, or keep only the overlap"},
           "target": _COMBINE_TARGET})
def _add_sketch(doc_id, args):
    from app.services import sketch_solver

    doc = _get(doc_id)
    spec = require(args, "sketch")
    solved = _solved_sketch(spec)
    # Loops of typed curves, not a ring of points: an arc that reaches the kernel
    # as an arc becomes a cylindrical face, which is what makes a sketched bore
    # visible to the hole spec, DFM and edge selection.
    profile = sketch_solver.sketch_to_loops(spec, solved)
    params = {
        "profile": profile,
        "sketch": spec,  # keep the editable source so the sketch can be reopened
        "plane": args.get("plane", "xy"),
        "offset": args.get("offset", 0.0),
        "depth": args.get("depth", 0.02),
    }
    _resolve_on_face(doc.feature_tree, args, params)
    node = {"type": "ExtrudeFeature", "name": "Sketch", "params": params}
    tree = feature_ops.add_primitive(doc.feature_tree, node, args.get("operation", "union"),
                                     target_id=args.get("target"))
    document_service.update_document(doc_id, feature_tree=tree)
    return {"message": "added sketch extrude", "feature_id": node["id"],
            **_sketch_result(solved)}


_ON_FACE_ARG = {
    "type": "object",
    "description": "{feature_id, face} with face in top|bottom|left|right|front|back, or "
                   "{feature_id, face_id} naming a real planar face from list_faces — the "
                   "second follows a re-topologising edit and reports the face as gone "
                   "rather than sliding onto a neighbour. Overrides plane/offset.",
}


@operation("create_sketch", "mutate", "Create a constrained 2D sketch as its own feature, to be consumed by "
           "add_extrude / add_revolve / add_loft via their 'sketch' argument. It builds no body "
           "of its own, several features can read one sketch, and editing it with update_sketch "
           "moves every one of them.",
           {"sketch": _SKETCH_ARG,
           "plane": {"enum": ["xy", "xz", "yz"], "default": "xy",
            "description": "the base plane the 2D profile is drawn on"},
           "offset": {"type": "number"},
           "on_face": _ON_FACE_ARG,
           "name": {"type": "string", "description": "a name for the sketch feature"}})
def _create_sketch(doc_id, args):
    from app.services import sketch_solver

    doc = _get(doc_id)
    spec = require(args, "sketch")
    solved = _solved_sketch(spec)
    params = {
        "sketch": spec,
        "profile": sketch_solver.sketch_to_loops(spec, solved),
        "plane": args.get("plane", "xy"),
        "offset": args.get("offset", 0.0),
        "solved": _sketch_result(solved),
    }
    _resolve_on_face(doc.feature_tree, args, params)
    node = {"type": feature_ops.SKETCH_TYPE, "name": args.get("name") or "Sketch",
            "params": params}
    # Joined as an ordinary union child so it gets a deterministic id from the
    # one allocator; `resolve_sketches` takes it back out before evaluation, so
    # the boolean collapses and the body is exactly what it was.
    tree = feature_ops.add_primitive(doc.feature_tree, node, "union")
    document_service.update_document(doc_id, feature_tree=tree)
    return {"message": "created sketch", "feature_id": node["id"],
            "plane": params["plane"], "offset": params["offset"],
            **_sketch_result(solved)}


@operation("update_sketch", "mutate", "Re-solve a sketch with an edited spec. Works on a create_sketch feature — "
           "in which case every feature reading it updates — or on an add_sketch extrude.",
           {"feature_id": {"type": "string", "required": True,
            "description": "id of the feature, from list_features"},
           "sketch": _SKETCH_ARG,
           "plane": {"enum": ["xy", "xz", "yz"],
            "description": "move the sketch to this base plane; omit to leave it where it is"},
           "depth": {"type": "number"}, "offset": {"type": "number"},
           "on_face": _ON_FACE_ARG})
def _update_sketch(doc_id, args):
    from app.services import sketch_solver

    doc = _get(doc_id)
    fid = require(args, "feature_id")
    node = feature_ops.find_node(doc.feature_tree, fid)
    if node is None or node.get("type") not in ("ExtrudeFeature", feature_ops.SKETCH_TYPE):
        raise NoSuchFeature(f"no sketch or extrude feature {fid!r}", arg="feature_id")
    spec = require(args, "sketch")
    solved = _solved_sketch(spec)
    params = {"profile": sketch_solver.sketch_to_loops(spec, solved), "sketch": spec}
    for key in ("plane", "offset", "depth"):
        if key in args:
            params[key] = args[key]
    if node.get("type") == feature_ops.SKETCH_TYPE:
        params["solved"] = _sketch_result(solved)
        params.pop("depth", None)          # a sketch has no thickness
    _resolve_on_face(doc.feature_tree, args, params)
    tree = feature_ops.set_params(doc.feature_tree, fid, params)
    document_service.update_document(doc_id, feature_tree=tree)
    consumers = [n["id"] for n in feature_ops.sketch_consumers(tree, fid)]
    return {"message": "updated sketch", "consumers": consumers,
            **_sketch_result(solved)}


@operation("check_sketch", "query", "Re-solve a sketch and report what it establishes — status, remaining "
           "degrees of freedom, conflicting or redundant constraints — without changing anything. "
           "Pass feature_id to check the sketch stored on a feature, or sketch to check one you have not saved.",
           {"feature_id": {"type": "string", "description": "a feature carrying a sketch (add_sketch made it)"},
           "sketch": {"type": "object", "description": "an unsaved sketch spec, in place of feature_id"}})
def _check_sketch(doc_id, args):
    from app.services import sketch_solver

    spec = args.get("sketch")
    if spec is None:
        fid = require(args, "feature_id")
        node = feature_ops.find_node(_get(doc_id).feature_tree, fid)
        if node is None:
            raise NoSuchFeature(f"no feature {fid!r}", arg="feature_id")
        spec = (node.get("params") or {}).get("sketch")
        if not spec:
            raise PreconditionFailed(
                f"feature {fid!r} carries no sketch — only features made by add_sketch do",
                arg="feature_id")
    solved = sketch_solver.solve_sketch(spec)
    return {"points": solved["points"], "radii": solved["radii"],
            "conflicts": solved["conflicts"],
            "rank": solved["rank"], "variables": solved["variables"],
            "equations": solved["equations"],
            **_sketch_result(solved)}


@operation("add_modifier", "mutate", "Apply a modifier: fillet/chamfer/shell/hole/pattern/transform. By default it wraps the "
           "whole body; pass 'target' (a feature id) to apply it to one feature — e.g. pattern a single hole. "
           "fillet/chamfer round or cut back every edge of the body; for named edges use fillet_edges/chamfer_edges.",
           {"modifier": {"enum": ["fillet", "chamfer", "shell", "hole", "pattern", "transform"], "required": True,
            "description": "which treatment to apply; it decides which `params` keys apply"},
           "target": _MODIFIER_TARGET,
           "params": _MODIFIER_PARAMS})
def _add_modifier(doc_id, args):
    kind = require(args, "modifier")
    if kind in ("fillet", "chamfer"):
        return _blend_whole_body(doc_id, kind, args)

    doc = _get(doc_id)
    mod = _make_modifier(kind, args.get("params"))
    tree = feature_ops.add_modifier(doc.feature_tree, mod, target_id=args.get("target"))
    document_service.update_document(doc_id, feature_tree=tree)
    return {"message": f"added {kind}", "feature_id": mod["id"]}


def _blend_whole_body(doc_id, kind: str, args) -> Dict[str, Any]:
    """`add_modifier(fillet|chamfer)` with nothing named — round the whole body.

    This used to do nothing at all, and report success while doing it. The
    modifier it built was a `FilletFeature`, which is a *smooth boolean between
    bodies*; wrapping the root gives it exactly one child, and a smooth union of
    one body is that body — so `resolve_suppressed` collapsed the single-child
    boolean away before evaluation. The op returned `ok`, a feature id and an
    unchanged volume. For an agent that is worse than a failure: it teaches a
    transition that does not exist.

    A whole-body fillet is a coherent instruction, so it is honoured rather than
    refused — as an edge fillet over every edge, which is what it means. That
    needs addressable topology, so without the exact kernel it is a precondition
    failure that says which operation would have worked.
    """
    size = float((args.get("params") or {}).get(
        "radius", 0.01 if kind == "fillet" else 0.005))
    if size <= 0:
        raise BadArgument(f"{kind} radius must be positive", arg="params")

    try:
        edges = _selected(doc_id, "all", "edge")
    except PreconditionFailed as exc:
        raise PreconditionFailed(
            f"a whole-body {kind} is an edge treatment and needs addressable "
            f"topology: {exc}. The distance-field `{kind}` modifier is a smooth "
            "blend *between* two bodies and has nothing to blend here.") from exc

    doc = _get(doc_id)
    if kind == "fillet":
        node = {"type": "EdgeFilletFeature", "name": "Fillet",
                "params": {"radius": size, "edges": edges}}
    else:
        node = {"type": "EdgeChamferFeature", "name": "Chamfer",
                "params": {"distance": size, "edges": edges}}

    tree = feature_ops.add_modifier(doc.feature_tree, node, target_id=args.get("target"))
    _apply_exact(doc_id, doc.feature_tree, tree,
                 f"a {size * 1000:g} mm {kind} on all {len(edges)} edge(s)")
    return {"message": f"{kind}ed all {len(edges)} edge(s) at {size * 1000:g} mm",
            "feature_id": node["id"], "edges": [e["id"] for e in edges]}


@operation("set_feature_params", "mutate", "Change a feature's parameters (merged). Lengths in metres.",
           {"feature_id": {"type": "string", "required": True,
            "description": "id of the feature, from list_features"},
           "params": {"type": "object", "required": True,
           "description": "the same keys the feature already has — read them with get_feature first"}})
def _set_feature_params(doc_id, args):
    doc = _get(doc_id)
    tree = feature_ops.set_params(doc.feature_tree, require(args, "feature_id"), require(args, "params"))
    document_service.update_document(doc_id, feature_tree=tree)
    return {"message": f"updated feature {args['feature_id']}"}


@operation("delete_feature", "mutate", "Remove a feature by id.",
           {"feature_id": {"type": "string", "required": True,
            "description": "id of the feature, from list_features"}})
def _delete_feature(doc_id, args):
    doc = _get(doc_id)
    tree = feature_ops.remove_node(doc.feature_tree, require(args, "feature_id"))
    document_service.update_document(doc_id, feature_tree=tree)
    return {"message": f"deleted feature {args['feature_id']}"}


@operation("add_dimension", "mutate", "Add a point-to-point dimension (metres).",
           {"a": _REQ_VEC, "b": _REQ_VEC,
           "label": {"type": "string", "description": "text to show instead of the measured distance"}})
def _add_dimension(doc_id, args):
    from app.models.schemas import AddDimensionRequest

    req = AddDimensionRequest(a=require(args, "a"), b=require(args, "b"), label=args.get("label"), source=args.get("source", "user"))
    dim = document_service.add_dimension(doc_id, req)
    if dim is None:
        raise NoSuchDocument(f"no document {doc_id!r}")
    return {"dimension": dim.model_dump()}


@operation("set_feature_suppressed", "mutate", "Suppress or unsuppress a feature (skip it during regeneration without deleting).",
           {"feature_id": {"type": "string", "required": True,
            "description": "id of the feature, from list_features"}, "suppressed": {"type": "boolean",
            "description": "true removes the feature from evaluation without deleting it"}})
def _set_feature_suppressed(doc_id, args):
    doc = _get(doc_id)
    tree = feature_ops.set_suppressed(doc.feature_tree, require(args, "feature_id"), bool(args.get("suppressed", True)))
    document_service.update_document(doc_id, feature_tree=tree)
    state = "suppressed" if args.get("suppressed", True) else "unsuppressed"
    return {"message": f"{state} feature {args['feature_id']}"}


@operation("set_feature_keep", "mutate", "Mark a feature as must-keep so generative optimization cannot remove it. Use this "
           "for anything the part needs regardless of load — mounting bosses, bearing seats, "
           "bolt holes. Its material is pinned solid and the optimizer routes load around it.",
           {"feature_id": {"type": "string", "required": True,
            "description": "id of the feature, from list_features"}, "keep": {"type": "boolean", "default": True,
            "description": "true pins this feature solid so optimization cannot remove it"}})
def _set_feature_keep(doc_id, args):
    doc = _get(doc_id)
    keep = bool(args.get("keep", True))
    tree = feature_ops.set_keep(doc.feature_tree, require(args, "feature_id"), keep)
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
           {"feature_id": {"type": "string", "required": True,
            "description": "id of the feature, from list_features"}, "direction": {"enum": ["up", "down"],
            "description": "move the feature earlier (up) or later (down) in the tree"}})
def _reorder_feature(doc_id, args):
    doc = _get(doc_id)
    tree = feature_ops.reorder(doc.feature_tree, require(args, "feature_id"), args.get("direction", "up"))
    document_service.update_document(doc_id, feature_tree=tree)
    return {"message": f"moved {args['feature_id']} {args.get('direction', 'up')}"}


@operation("ai_edit", "mutate", "Apply a natural-language edit to the whole part.",
           {"instruction": {"type": "string", "required": True,
            "description": "what to change, in words — applies to the whole part"}})
def _ai_edit(doc_id, args):
    document_service.ai_edit_document(doc_id, require(args, "instruction"))
    return {"message": "applied AI edit"}


@operation("list_materials", "query", "List the engineering material catalog (density, modulus, yield, cost).",
           {})
def _list_materials(doc_id, args):
    from app.services import materials

    return {"materials": [m.model_dump() for m in materials.list_materials()]}


@operation("set_material", "mutate", "Assign an engineering material to the part (drives mass, sim, DFM, quotes).",
           {"material_id": {"type": "string", "required": True, "description": "e.g. al-6061, steel-304, ti-6al4v, pla"}})
def _set_material(doc_id, args):
    doc = document_service.set_material(doc_id, require(args, "material_id"))
    if doc is None:
        raise NoSuchDocument(f"no document {doc_id!r}")
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
        raise NoSuchDocument(f"no document {doc_id!r}")
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


def _exact_body(doc_id):
    """The document's exact build, or a precondition failure explaining why not.

    The gate in front of every topology-addressing operation. Both refusals are
    worth telling apart: the deployment has no exact kernel, or this particular
    body cannot be built exactly. Returns the whole `ExactBody` — the shape plus
    the entity tags a selection has to carry to survive a re-topologising edit.
    """
    from app.cad_kernel.evaluator import brep

    if not brep.AVAILABLE:
        raise PreconditionFailed(
            "this deployment has no exact kernel installed (pip install cadquery-ocp), "
            "so faces and edges are not addressable")

    _get(doc_id)  # typed NoSuchDocument before anything geometric happens
    body = document_service.get_exact_body(doc_id)
    if body is None:
        raise PreconditionFailed(
            "this body has no exact form, so it has no addressable faces or edges. "
            "Smooth-boolean fillet/chamfer, shell, sheet metal, imported and "
            "optimized bodies are evaluated as a distance field instead.")
    return body


def _exact_shape(doc_id):
    """Just the shape. Kept because most callers want nothing else."""
    return _exact_body(doc_id).shape


@operation("list_faces", "query", "List the body's faces with stable ids, so they can be selected. "
           "Each face reports its kind (plane/cylinder/cone/sphere), area, centre and, "
           "for cylinders, radius — enough to tell which is which without seeing it. "
           "Pass 'feature' to list one feature's own faces instead, which is the id space "
           "a sketch's on_face.face_id is resolved in.",
           {"feature": {"type": "string",
            "description": "list the faces of this feature's subtree rather than the whole body"}})
def _list_faces(doc_id, args):
    from app.cad_kernel.core import naming

    feature_id = args.get("feature")
    if feature_id:
        shape = _feature_exact_shape(doc_id, feature_id)
    else:
        shape = _exact_shape(doc_id)
    table = naming.face_table(shape)
    return {"faces": [{"id": f["id"], "kind": f["kind"], "area_mm2": f["area_mm2"],
                       "center_mm": f["center_mm"],
                       **({"radius_mm": f["radius_mm"]} if f.get("radius_mm") else {}),
                       **({"normal": f["normal"]} if f.get("normal") else {})}
                      for f in table],
            "count": len(table),
            **({"feature": feature_id} if feature_id else {})}


def _feature_exact_shape(doc_id, feature_id):
    """The exact body of one feature's subtree.

    Faces are named relative to the body they are on — the descriptor is
    normalised to its bounding box, which is what lets a resize keep a selection
    — so a sub-body's face ids are its own, and are the ids `on_face.face_id`
    means. Scoping the reference to a feature rather than to the whole body is
    also what keeps a sketch's own extrusion out of the datum it sits on.
    """
    from app.cad_kernel.evaluator import brep
    from app.services import datum

    if not brep.AVAILABLE:
        raise PreconditionFailed(
            "this deployment has no exact kernel installed (pip install cadquery-ocp), "
            "so faces and edges are not addressable")
    try:
        shape, _ = datum.feature_shape(_get(doc_id).feature_tree, feature_id)
    except KeyError as exc:
        raise NoSuchFeature(str(exc), arg="feature")
    if shape is None:
        raise PreconditionFailed(
            f"feature {feature_id!r} has no exact form, so it has no addressable faces")
    return shape


@operation("list_edges", "query", "List the body's edges with stable ids, so they can be filleted or "
           "chamfered. Each edge reports its kind (line/circle), length, midpoint and, "
           "for circles, radius.",
           {"kind": {"enum": ["all", "line", "circle"], "default": "all",
           "description": "filter to straight edges or circular ones"},
           "min_length_mm": {"type": "number",
           "description": "ignore edges shorter than this — useful for skipping slivers"}})
def _list_edges(doc_id, args):
    from app.cad_kernel.core import naming

    table = naming.edge_table(_exact_shape(doc_id))
    wanted = str(args.get("kind", "all"))
    floor = float(args.get("min_length_mm") or 0.0)
    if wanted != "all":
        table = [e for e in table if e["kind"] == wanted]
    table = [e for e in table if e["length_mm"] >= floor]
    return {"edges": [{"id": e["id"], "kind": e["kind"], "length_mm": e["length_mm"],
                       "midpoint_mm": e["midpoint_mm"],
                       **({"radius_mm": e["radius_mm"]} if e.get("radius_mm") else {})}
                      for e in table],
            "count": len(table)}


def _selected(doc_id, ids, kind: str) -> List[Dict[str, Any]]:
    """Turn the ids a caller selected into durable references.

    An id alone is a hash of the entity's current measurements, so storing just
    that would break the moment an upstream dimension moved it. The whole
    descriptor is stored instead — including the generative-history ``tag``, the
    entity's identity as opposed to its measurements. `naming.resolve_references`
    follows the tag first, so a selection survives an upstream edit that *splits*
    what it named, and falls back to nearest-descriptor match where there is no
    history to follow.
    """
    from app.cad_kernel.core import naming

    body = _exact_body(doc_id)
    shape, entities = body.shape, body.entities
    table = (naming.face_table(shape, entities) if kind == "face"
             else naming.edge_table(shape, entities))
    by_id = {d["id"]: d for d in table}

    if isinstance(ids, str) and ids.strip().lower() == "all":
        # "Round every edge" is a real instruction and a common one. Without the
        # shorthand it costs a list_edges call and a verbatim echo of every id,
        # which is a lot of tokens to spend saying "all of them".
        wanted = [d["id"] for d in table]
    else:
        wanted = [ids] if isinstance(ids, str) else list(ids or [])
    if not wanted:
        raise MissingArgument(f"select at least one {kind} (see list_{kind}s)",
                              arg=f"{kind}s")

    missing = [i for i in wanted if i not in by_id]
    if missing:
        raise BadArgument(
            f"no such {kind}(s): {', '.join(missing)}. Call list_{kind}s for the "
            f"{len(table)} available on this body.", arg=f"{kind}s")
    return [by_id[i] for i in wanted]


def _apply_exact(doc_id: str, previous_tree: Dict[str, Any], tree: Dict[str, Any],
                 what: str) -> None:
    """Commit a tree only the exact kernel can build, or undo it and say why.

    These operations are the one place where "an unevaluatable tree is a
    warning" is the wrong contract. That rule exists so a feature can be bound
    to a parameter expression before the parameter is defined — a real workflow
    where the document recovers on the next edit. An edge fillet that OCC
    refuses does not recover: the radius was too big, or the edges do not meet,
    and the document is now a body that cannot be meshed, exported or measured.
    So the edit is rolled back and the failure is typed, which is also what
    makes it legible to an agent — a `PRECONDITION` naming the radius beats an
    `ok` whose volume happens to be zero.
    """
    updated = document_service.update_document(doc_id, feature_tree=tree)
    error = updated.derived.error if (updated and updated.derived) else None
    if not error:
        return
    document_service.update_document(doc_id, feature_tree=previous_tree)
    raise PreconditionFailed(f"{what} could not be built: {error}")


@operation("fillet_edges", "mutate", "Round named edges of the body to a radius — a real fillet on selected "
           "edges, not a blend between bodies. Pick edge ids with list_edges. "
           "Requires the exact kernel.",
           {"edges": _EDGE_SELECTION,
           "radius": {"type": "number", "required": True, "description": "metres"},
           "target": _MODIFIER_TARGET})
def _fillet_edges(doc_id, args):
    doc = _get(doc_id)
    radius = float(require(args, "radius"))
    if radius <= 0:
        raise BadArgument("radius must be positive", arg="radius")
    references = _selected(doc_id, require(args, "edges"), "edge")

    node = {"type": "EdgeFilletFeature", "name": "Fillet",
            "params": {"radius": radius, "edges": references}}
    tree = feature_ops.add_modifier(doc.feature_tree, node, target_id=args.get("target"))
    _apply_exact(doc_id, doc.feature_tree, tree,
                 f"a {radius * 1000:g} mm fillet on {len(references)} edge(s)")
    return {"message": f"filleted {len(references)} edge(s) at {radius * 1000:g} mm",
            "feature_id": node["id"], "edges": [r["id"] for r in references]}


@operation("chamfer_edges", "mutate", "Cut named edges back at 45° by a distance — a real chamfer on selected "
           "edges. Pick edge ids with list_edges. Requires the exact kernel.",
           {"edges": _EDGE_SELECTION,
           "distance": {"type": "number", "required": True, "description": "metres"},
           "target": _MODIFIER_TARGET})
def _chamfer_edges(doc_id, args):
    doc = _get(doc_id)
    distance = float(require(args, "distance"))
    if distance <= 0:
        raise BadArgument("distance must be positive", arg="distance")
    references = _selected(doc_id, require(args, "edges"), "edge")

    node = {"type": "EdgeChamferFeature", "name": "Chamfer",
            "params": {"distance": distance, "edges": references}}
    tree = feature_ops.add_modifier(doc.feature_tree, node, target_id=args.get("target"))
    _apply_exact(doc_id, doc.feature_tree, tree,
                 f"a {distance * 1000:g} mm chamfer on {len(references)} edge(s)")
    return {"message": f"chamfered {len(references)} edge(s) at {distance * 1000:g} mm",
            "feature_id": node["id"], "edges": [r["id"] for r in references]}


@operation("shell_faces", "mutate", "Hollow the body to a wall thickness, removing the named faces so it is "
           "open there. Pick face ids with list_faces. This is a real shell — the "
           "`shell` modifier hollows everything with no opening. Requires the exact kernel.",
           {"faces": {"type": ["array", "string"], "required": True,
           "description": "face ids from list_faces — the openings"},
           "thickness": {"type": "number", "required": True, "description": "wall thickness, metres"},
           "target": _MODIFIER_TARGET})
def _shell_faces(doc_id, args):
    doc = _get(doc_id)
    thickness = float(require(args, "thickness"))
    if thickness <= 0:
        raise BadArgument("thickness must be positive", arg="thickness")
    references = _selected(doc_id, require(args, "faces"), "face")

    node = {"type": "FaceShellFeature", "name": "Shell",
            "params": {"thickness": thickness, "faces": references}}
    tree = feature_ops.add_modifier(doc.feature_tree, node, target_id=args.get("target"))
    _apply_exact(doc_id, doc.feature_tree, tree,
                 f"a {thickness * 1000:g} mm shell through {len(references)} face(s)")
    return {"message": f"shelled to {thickness * 1000:g} mm through {len(references)} face(s)",
            "feature_id": node["id"], "faces": [r["id"] for r in references]}


# Features that hold references to named topology, and so can lose one.
_ADDRESSES_TOPOLOGY = {"EdgeFilletFeature", "EdgeChamferFeature", "FaceShellFeature"}


def _lost_references(doc_id: str, doc=None) -> int:
    """How many stored face/edge selections stopped resolving, on a body that built.

    Gated on the tree actually containing a feature that holds references — a
    dict walk — because `get_exact_body` is an OpenCascade build and the vast
    majority of edits are to documents with nothing selectable in them. Where the
    gate does open, the document already needs the exact kernel to evaluate at
    all, and the build is cached for the `check_references` or `list_edges` call
    that usually follows.
    """
    from app.cad_kernel.core.feature_tree import walk_nodes
    from app.cad_kernel.evaluator import brep

    doc = doc or document_service.get_document(doc_id)
    if not brep.AVAILABLE or doc is None:
        return 0
    if not any(n.get("type") in _ADDRESSES_TOPOLOGY for n in walk_nodes(doc.feature_tree)):
        return 0

    body = document_service.get_exact_body(doc_id)
    if body is None:
        return 0
    return sum(1 for report in body.references for entry in report["references"]
               if entry["status"] in ("deleted", "unresolved"))


_REFERENCE_STATUS = {
    "matched": "still names exactly one entity",
    "split": "an upstream edit divided it; the reference now names every piece",
    "deleted": "an upstream edit destroyed it — reselect and reapply",
    "unresolved": "nothing on the current body matches it within tolerance",
}


def _sketch_plane_references(doc_id) -> List[Dict[str, Any]]:
    """Face-id sketch planes, resolved as the build would resolve them.

    A sketch placed on a named face is a reference like any other and fails the
    same way — the face it sat on can be split by a cut or destroyed by a
    resize. It is not collected during the exact build the way a fillet's edges
    are (a datum is resolved before the body is built, not during it), so it is
    resolved here.
    """
    from app.cad_kernel.core.feature_tree import walk_nodes
    from app.services import datum

    tree = _get(doc_id).feature_tree
    rows: List[Dict[str, Any]] = []
    for node in walk_nodes(tree):
        ref = (node.get("params") or {}).get("on_face")
        if not isinstance(ref, dict) or not ref.get("face_id"):
            continue
        try:
            _, _, _, status = datum.resolve_face_id_frame(
                tree, ref["feature_id"], ref.get("face_ref") or ref["face_id"])
        except Exception:  # noqa: BLE001 — OCC raises many types, and this is a report
            # Anything that stops the reference resolving *is* the answer here:
            # the point of the query is to say which selections no longer land,
            # and a failure to rebuild the host is one of the ways they don't.
            # `resolve_datums` treats the same failure the same way.
            status = "unresolved"
        rows.append({"feature_id": node.get("id"), "type": node.get("type"),
                     "kind": "face", "references": [
                         {"id": ref["face_id"], "status": status,
                          "on": ref["feature_id"],
                          "meaning": _REFERENCE_STATUS.get(status, status)}]})
    return rows


@operation("check_references", "query",
           "Report whether the faces and edges the body's fillets, chamfers and shells "
           "were placed on still exist. An upstream dimension change can split an entity "
           "in two (the reference then names both) or destroy it outright — and a "
           "reference that lost its target is the one failure a geometry check cannot "
           "see, because the body it leaves behind is perfectly valid, just not what "
           "was asked for. Requires the exact kernel.",
           {})
def _check_references(doc_id, args):
    from app.cad_kernel.evaluator import brep

    if not brep.AVAILABLE:
        raise PreconditionFailed(
            "this deployment has no exact kernel installed (pip install cadquery-ocp), "
            "so faces and edges are not addressable")
    _get(doc_id)
    verdicts, error = document_service.reference_verdicts(doc_id)

    features, lost, split = [], 0, 0
    for report in verdicts:
        rows = []
        for entry in report["references"]:
            status = entry["status"]
            if status in ("deleted", "unresolved"):
                lost += 1
            elif status == "split":
                split += 1
            rows.append({**entry, "meaning": _REFERENCE_STATUS.get(status, status)})
        features.append({**report, "references": rows})

    for row in _sketch_plane_references(doc_id):
        if row["references"][0]["status"] in ("deleted", "unresolved"):
            lost += 1
        features.append(row)

    if not features and error:
        # No feature got as far as resolving anything, so the build failed for an
        # unrelated reason and there is nothing here to report on.
        raise PreconditionFailed(f"this body has no exact form: {error}")
    return {"features": features, "lost": lost, "split": split,
            "ok": lost == 0 and not error,
            **({"error": error} if error else {}),
            "message": (f"{lost} reference(s) no longer resolve" if lost
                        else f"every reference resolves ({split} split by a later edit)"
                        if split else "every reference resolves")}


@operation("bodies", "query",
           "List the disjoint solids the part evaluates to, largest first, each with its "
           "own volume, size and position. A document holds one feature tree, but a tree "
           "can evaluate to several separate solids — deliberately, or because a cut went "
           "further than intended. Volume and mass are already correct for that; this is "
           "what tells the two cases apart. Declare the intended count with a 'bodies' "
           "requirement in set_spec and check_geometry stops reporting it as damage.",
           {"resolution": {"type": "number", "default": 128,
           "description": "meshing resolution for the split"}})
def _bodies(doc_id, args):
    from app.services import geometry_check

    _get(doc_id)
    solid = document_service.get_solid(doc_id, resolution=int(args.get("resolution", 128)))
    found = geometry_check.bodies_of(solid)
    total = sum(b["volume_mm3"] for b in found)
    largest = found[0]["volume_mm3"] if found else 0.0
    return {
        "count": len(found),
        "bodies": found,
        "total_volume_mm3": round(total, 4),
        # A part severed by accident usually leaves one big piece and slivers;
        # two intended parts do not. Worth stating rather than making the caller
        # divide two numbers.
        "largest_fraction": round(largest / total, 4) if total > 0 else 0.0,
        "message": (f"{len(found)} separate bodies" if len(found) != 1
                    else "one connected body"),
    }


@operation("check_geometry", "query", "Validate the body as a solid: non-empty, watertight, consistent winding, "
           "single connected body. Use this to verify an edit produced real geometry before "
           "going on to manufacturing steps.",
           {"resolution": {"type": "number", "default": 128,
           "description": "meshing resolution for the check"}})
def _check_geometry(doc_id, args):
    from app.services import geometry_check

    return geometry_check.check_document(doc_id, resolution=int(args.get("resolution", 128)))


@operation("dfm", "query", "Design-for-manufacture report. 'additive' (LPBF, the default) reports overhang, "
           "support volume, printability and a quote; 'milling', 'molding' and 'casting' report what "
           "needs real topology — wall thickness, smallest bore, trapped voids, plus draft and "
           "undercuts for a mould or 3-axis tool access for a cutter. Everything except the "
           "additive-specific rows is measured on the exact body, so it does not move with "
           "'resolution'. Non-additive processes need the exact kernel.",
           {"process": {"enum": ["additive", "milling", "molding", "casting"], "default": "additive",
           "description": "what the part will be made by"},
           "build_axis": {"enum": ["auto", "x", "y", "z"], "default": "auto",
           "description": "build direction for additive; mould pull / tool approach otherwise "
                          "('auto' means +Z there — which way a mould opens is a decision, "
                          "not something to infer)"},
           "resolution": {"type": "number", "default": 128,
           "description": "meshing resolution; affects the additive rows only"}})
def _dfm(doc_id, args):
    """DFM was reachable only over REST, so an agent driving the op catalog could
    build a part but never check whether it could be made."""
    from app.services import document_analysis

    _get(doc_id)  # typed NoSuchDocument rather than the router's HTTPException
    process = str(args.get("process", "additive"))
    try:
        report = document_analysis.dfm_document(
            doc_id, build_axis=str(args.get("build_axis", "auto")),
            resolution=int(args.get("resolution", 128)), process=process)
    except ValueError as exc:
        # "this part has no exact form" is a different previous step, not a
        # different argument — model it the way the rest of the catalog does.
        raise PreconditionFailed(str(exc)) from exc
    return report.model_dump()


@operation("simulate", "query", "Linear-static FEA: von Mises stress, displacement and safety factor "
           "against the material's yield, from the load cases and supports on the document's "
           "optimization setup. An 8-node hexahedral solve on the body itself, so the answer "
           "depends on the shape, where the load is and where the part is held. Check `solver` "
           "in the result: 'closed-form estimate' means no real solve ran and the numbers are "
           "not an analysis.",
           {"elements": {"type": "number", "default": 8000,
           "description": "element budget; more is slower and resolves stress further"},
           "resolution": {"type": "number", "default": 128,
           "description": "meshing resolution for the fallback estimate only"}})
def _simulate(doc_id, args):
    """`simulate` was deliberately kept out of the catalog while it was a
    closed-form guess — a verifiable signal that means nothing is worse than no
    signal, because a model will optimise it. Now that a real solve backs it, it
    belongs in the action space, and the result says which one produced it."""
    from app.services import document_analysis

    _get(doc_id)
    return document_analysis.analyse_document(
        doc_id, resolution=int(args.get("resolution", 128)),
        elements=int(args["elements"]) if args.get("elements") else None)


@operation("profile_cut", "query", "2D laser/plasma/waterjet cut of the part outline from sheet, kerf-compensated. "
           "Fetch G-code at GET /api/documents/{id}/profile-cut.gcode.",
           {"kerf_mm": {"type": "number", "default": 0.2}, "plane": {"enum": ["xy", "xz", "yz"], "default": "xy",
            "description": "the base plane the 2D profile is drawn on"},
           "feed_mm_min": {"type": "number"},
           "power": {"type": "number", "description": "cutter power as a fraction of the machine maximum"}})
def _profile_cut(doc_id, args):
    from app.services.cam import profile_cut

    out = profile_cut.profile_cut(
        doc_id, kerf_mm=args.get("kerf_mm", 0.2), plane=args.get("plane", "xy"),
        feed_mm_min=args.get("feed_mm_min", 1200.0), power=args.get("power", 100.0))
    return {"message": f"cut {out['path_count']} contour(s), {out['cut_length_mm']}mm",
            "path_count": out["path_count"], "cut_length_mm": out["cut_length_mm"],
            "estimated_minutes": out["estimated_minutes"], "gcode_bytes": len(out["gcode"])}


_DRAWING_SPEC = {
    "type": "object",
    "description": "what to draw — every field here is a drafting decision, not geometry",
    "properties": {
        "views": {"type": "array",
                  "description": "any of front, back, top, bottom, left, right "
                                 "(default: front, top, right)"},
        "sections": {"type": "array",
                     "description": "[{plane: xy|xz|yz, offset_mm?, label?}] — offset_mm "
                                    "defaults to the middle of the body"},
        "dimensions": {"type": "array",
                       "description": "[{view, type: horizontal|vertical|aligned, "
                                      "from:[h_mm,v_mm], to:[h_mm,v_mm], offset?, label?}]. "
                                      "Points are model mm on that view's own two axes "
                                      "(FRONT is X,Z; TOP is X,Y; RIGHT is Y,Z). Omit "
                                      "'label' and the true model distance is printed."},
        "annotations": {"type": "array",
                        "description": "[{view, at:[h_mm,v_mm], text | gdt_feature | "
                                       "tolerance_label, dx?, dy?, frame?}] — a leader from "
                                       "a point on the part to a callout. Naming a "
                                       "gdt_feature or tolerance_label pulls the text from "
                                       "the document so it cannot drift."},
        "projection": {"enum": ["first", "third"], "default": "third",
                       "description": "projection angle convention: third angle (ISO A) or first"},
        "sheet": {"enum": ["auto", "a4", "a3", "a2", "a1", "a0"], "default": "auto",
                  "description": "sheet size; auto picks the smallest that fits"},
        "scale": {"description": "'auto' picks the largest standard scale that fits, "
                                 "or give a number (1.0 = full size)"},
        "show_hidden": {"type": "boolean", "default": True,
                        "description": "dashed lines for detail behind the front face"},
    },
    "additionalProperties": False,
}


@operation("generate_drawing", "query", "Generate a dimensioned multi-view 2D engineering drawing. "
           "You choose the views, section planes, dimensions, annotations, projection angle, "
           "sheet and scale; projection and section-cutting are exact. Fetch the full SVG at "
           "GET /api/documents/{id}/drawing.svg.",
           {"spec": _DRAWING_SPEC,
           "section": {"type": "boolean", "default": True,
           "description": "legacy shorthand: add a mid-body section when no spec.sections given"}})
def _generate_drawing(doc_id, args):
    from app.services import drawing

    _get(doc_id)
    out = drawing.generate_drawing(doc_id, section=args.get("section", True),
                                   spec=args.get("spec"))
    # Keep the op result light for agents; the full SVG is fetched via the endpoint.
    return {"message": f"generated {out['scale_text']} drawing on a "
                       f"{out['sheet_mm']['width']:.0f}×{out['sheet_mm']['height']:.0f} mm sheet",
            "views": out["views"], "hidden": out["hidden"], "sections": out["sections"],
            "bbox_mm": out["bbox_mm"], "traced_mm": out["traced_mm"],
            "scale_text": out["scale_text"], "projection": out["projection"],
            "sheet_mm": out["sheet_mm"], "dimensions": out["dimensions"],
            "annotations": out["annotations"],
            "material": out["material"], "svg_bytes": len(out["svg"])}


@operation("check_drawing", "query", "Validate a drawing as a manufacturing document: views carry "
           "geometry, the part is dimensioned, the views match the body, sections cut "
           "material. Use it to verify a drawing says something about the part.",
           {"spec": _DRAWING_SPEC})
def _check_drawing(doc_id, args):
    from app.services import drawing

    _get(doc_id)
    return drawing.check_drawing(doc_id, spec=args.get("spec"))


@operation("compute_fit", "query", "ISO-286 fit/tolerance limits for a nominal size. 'fit' is a single spec ('H7') "
           "or a hole/shaft fit ('H7/g6' → clearance range + classification).",
           {"nominal_mm": {"type": "number", "required": True},
           "fit": {"type": "string", "required": True,
            "description": "ISO fit designation, e.g. H7 or g6"}})
def _compute_fit(doc_id, args):
    from app.services import tolerances

    nominal = float(require(args, "nominal_mm"))
    code = str(require(args, "fit"))
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
        raise NoSuchDocument(f"no document {doc_id!r}")
    return {"message": f"set {len(doc.tolerances)} tolerance callout(s), {len(doc.gdt)} GD&T frame(s)",
            "general_tolerance": doc.general_tolerance}


@operation("set_spec", "mutate", "Record what the part is supposed to be: measurable requirements the body "
           "is scored against by verify_against_spec. Kinds: bounding_box (axis + value/tolerance or "
           "min/max, mm), mass (g), volume (cm³), material (id), hole (diameter mm + count), "
           "feature (a feature type that must be present), watertight, bodies. "
           "Set this before modelling and the score tells you how close the part is.",
           {"requirements": {"type": "array", "required": True,
           "description": "[{id?, kind, label?, axis?, value?, tolerance?, min?, max?, "
                          "diameter?, diameter_tolerance?, count?}] — [] clears the spec"},
           "goal": {"type": "string", "description": "the request in words, for the record"}})
def _set_spec(doc_id, args):
    from app.services.design_spec import SpecError

    _get(doc_id)
    spec = {"requirements": require(args, "requirements")}
    if args.get("goal"):
        spec["goal"] = args["goal"]
    try:
        doc = document_service.set_spec(doc_id, spec)
    except SpecError as exc:
        raise BadArgument(str(exc), arg="requirements")
    count = len((doc.spec or {}).get("requirements", []))
    return {"message": f"set {count} requirement(s)" if count else "cleared the spec",
            "requirements": count}


@operation("get_spec", "query", "The design spec this part is judged against, if one is set.",
           {})
def _get_spec(doc_id, args):
    doc = _get(doc_id)
    spec = doc.spec or {}
    return {"goal": spec.get("goal"), "requirements": spec.get("requirements", [])}


@operation("verify_against_spec", "query", "Score the current body against the design spec: one pass/fail row per "
           "requirement, plus the fraction met. This is the check that distinguishes a "
           "correct part from a merely valid one — check_geometry says the solid is well "
           "formed, this says it is the right solid.",
           {})
def _verify_against_spec(doc_id, args):
    from app.services import design_spec

    doc = _get(doc_id)
    try:
        return design_spec.verify(doc)
    except design_spec.SpecError as exc:
        raise PreconditionFailed(str(exc))


@operation("release_revision", "mutate", "Cut an immutable released revision (next letter A/B/C…) from the current geometry.",
           {"notes": {"type": "string", "description": "what changed in this revision"},
           "label": {"type": "string", "description": "a name for the revision"}})
def _release_revision(doc_id, args):
    rev = document_service.release_revision(doc_id, notes=args.get("notes", ""), label=args.get("label", ""))
    if rev is None:
        raise NoSuchDocument(f"no document {doc_id!r}")
    return {"message": f"released revision {rev.letter}", "letter": rev.letter,
            "state": rev.state, "released_at": rev.released_at}


@operation("set_lifecycle_state", "mutate", "Set the document lifecycle state: draft | in_review | released | obsolete.",
           {"state": {"enum": ["draft", "in_review", "released", "obsolete"], "required": True,
            "description": "the lifecycle state to move the document to"}})
def _set_lifecycle_state(doc_id, args):
    doc = document_service.set_lifecycle_state(doc_id, require(args, "state"))
    if doc is None:
        raise NoSuchDocument(f"no document {doc_id!r}")
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
           "active": {"type": "string", "description": "name of the configuration to make current"}})
def _set_configurations(doc_id, args):
    doc = document_service.set_configurations(doc_id, args.get("configurations") or [], args.get("active"))
    if doc is None:
        raise NoSuchDocument(f"no document {doc_id!r}")
    return {"message": f"set {len(doc.configurations)} configuration(s)",
            "configurations": [c.model_dump() for c in doc.configurations],
            "active": doc.active_configuration}


@operation("activate_configuration", "mutate", "Switch the live variant by name (empty = base parameters); geometry re-evaluates.",
           {"name": {"type": "string",
            "description": "the configuration to activate; omit to clear back to the base"}})
def _activate_configuration(doc_id, args):
    doc = document_service.activate_configuration(doc_id, args.get("name"))
    if doc is None:
        raise NoSuchDocument(f"no document {doc_id!r}")
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


@operation("slice_part", "mutate", "Slice the body for FDM 3D printing (SDF slicer). Returns layer/time/filament "
           "stats; the G-code is stored on the document for download or printing.",
           {"settings": {"type": "object",
           "description": "optional overrides: layer_height (mm), wall_count, "
           "infill (0..1), temp_nozzle, temp_bed, speed_print …"}})
def _slice_part(doc_id, args):
    """FDM-slice the body; stats come back, the G-code lands on the document.

    Registered "mutate" because it writes ``derived.cam``. As a "query" it
    bypassed the editor-access check and the quota meter, and returned
    ``document=None`` — so a caller never saw the G-code it had just produced.
    """
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
           {"printer_url": {"type": "string", "required": True, "description": "Moonraker base URL, e.g. http://printer.local:7125"},
           "start": {"type": "boolean", "default": False,
            "description": "begin the print after uploading, rather than just uploading"},
           "settings": {"type": "object", "description": "slice settings if slicing is needed"}})
def _print_part(doc_id, args):
    """Upload the sliced G-code to a Moonraker/Klipper printer (slice first if needed)."""
    import base64

    from app.services.machine import moonraker_client as mr
    from app.services.machine.url_guard import UrlNotAllowed, validate_url

    try:
        url = validate_url(require(args, "printer_url"))
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
    """The self-describing tool catalog.

    Carries both the descriptor block every existing consumer reads and a strict
    JSON Schema per operation, which is what a function-calling API or a
    grammar-constrained decoder actually needs.
    """
    from app.services import op_schema

    return [op_schema.describe(o.name, o.kind, o.description, o.params)
            for o in OPERATIONS.values()]


def dispatch(op_name: str, doc_id: str, args: Dict[str, Any] | None) -> Dict[str, Any]:
    """Run one operation, raising a typed OperationError on failure.

    The raising form. Prefer :func:`execute` for anything that needs to *observe*
    a failure rather than propagate it — an agent loop, a training rollout, the
    HTTP layer.
    """
    from app.services import op_schema

    op = OPERATIONS.get(op_name)
    if op is None:
        raise UnknownOperation(f"unknown operation: {op_name!r}", arg="op")
    # Refuse a malformed call before it can half-apply an edit to the tree.
    op_schema.validate(op.params, args, op=op_name)
    return op.handler(doc_id, args or {})


def _as_operation_error(exc: BaseException) -> OperationError:
    """Classify an exception escaping a handler.

    Handlers raise typed errors directly now, but the layers beneath them
    (feature_ops, the kernel, numpy) still raise plain built-ins, and an
    unclassified exception must never reach the caller as an opaque 500.
    """
    if isinstance(exc, OperationError):
        return exc
    if isinstance(exc, KeyError):
        # feature_ops raises KeyError("feature <id> not found") from its mutators.
        return NoSuchFeature(str(exc).strip("'\""))
    if isinstance(exc, (ValueError, TypeError)):
        return BadArgument(str(exc))
    return InternalError(f"{type(exc).__name__}: {exc}")


def execute(
    op_name: str,
    doc_id: str,
    args: Dict[str, Any] | None = None,
    *,
    include_state: bool = True,
    rollback_on_eval_error: bool = False,
    rationale: str = "",
) -> Dict[str, Any]:
    """Run one operation and return a full result envelope. Never raises.

    This is the contract an agent is trained against, so it answers the three
    questions the old response could not: did it work, why not, and what changed.
    ``ok`` is computed rather than hardcoded — it was previously a constant
    ``True`` that no failure could ever clear.

    A tree that does not currently evaluate is reported as a **warning**, not a
    failure. That is deliberate: binding a feature to a parameter expression
    before defining the parameter leaves the document briefly unevaluatable and
    is a supported, normal workflow — the whole reason ``derived.error`` exists
    is so "a malformed edit never wedges the document". The operation did what it
    was asked; the geometry is simply not resolvable yet. The condition is still
    fully visible, in ``warnings`` and in ``state.error``, so a reward function
    can penalise it without the API pretending the call failed.

    ``rollback_on_eval_error`` opts into the stricter contract an RL environment
    may prefer: any edit that leaves the body unevaluatable is undone, so the
    document is always in a valid state and there are no absorbing failure
    states. Off by default because it would break the parametric workflow above.
    """
    from app.services import observation, trajectories

    started = time.perf_counter()

    op = OPERATIONS.get(op_name)
    if op is None:
        known = ", ".join(sorted(OPERATIONS))
        env = {
            "ok": False, "op": op_name, "result": {}, "warnings": [],
            "error": UnknownOperation(f"unknown operation {op_name!r}; known: {known}",
                                      arg="op").to_dict(),
        }
        trajectories.record_step(op_name, doc_id, args, env, rationale=rationale)
        return env

    doc = document_service.get_document(doc_id)
    if doc is None:
        env = {"ok": False, "op": op_name, "result": {}, "warnings": [],
               "error": NoSuchDocument(f"no document {doc_id!r}").to_dict()}
        trajectories.record_step(op_name, doc_id, args, env, rationale=rationale)
        return env

    mutates = op.kind == "mutate"
    before = observation.snapshot(doc)
    obs_before = observation.summarize(doc)
    # Only a mutating op can need rolling back, and only then is the copy worth it.
    tree_before = copy.deepcopy(doc.feature_tree) if (mutates and rollback_on_eval_error) else None

    def _elapsed_ms() -> float:
        return round((time.perf_counter() - started) * 1000, 2)

    try:
        from app.services import op_schema

        op_schema.validate(op.params, args, op=op_name)
        result = op.handler(doc_id, args or {})
    except Exception as exc:  # noqa: BLE001 — classified, never swallowed
        err = _as_operation_error(exc)
        if isinstance(err, InternalError):
            _LOGGER.exception("operation %s failed unexpectedly", op_name)
        after_doc = document_service.get_document(doc_id)
        env = {
            "ok": False, "op": op_name, "result": {}, "error": err.to_dict(), "warnings": [],
            "state": observation.summarize(after_doc) if (include_state and after_doc) else None,
        }
        # A failed call is training data too — arguably the most valuable kind,
        # since it is what teaches recovery.
        trajectories.record_step(op_name, doc_id, args, env, obs_before=obs_before,
                                 latency_ms=_elapsed_ms(), rationale=rationale)
        return env

    from app.services import units

    after_doc = document_service.get_document(doc_id)
    envelope: Dict[str, Any] = {
        "ok": True,
        "op": op_name,
        "result": result if isinstance(result, dict) else {"value": result},
        "error": None,
        # A length far outside part scale is almost always the metres/millimetres
        # confusion this API's shape invites — the observation reads back in mm
        # and the arguments are in m. It is a warning rather than a refusal
        # because a 2 m sheet is a real part, and the copilot forwards warnings
        # to the planner, so naming the suspicion is actionable.
        "warnings": units.implausible(args, op.params),
    }

    if mutates and after_doc is not None and after_doc.derived.error:
        eval_error = after_doc.derived.error
        if rollback_on_eval_error:
            document_service.update_document(doc_id, feature_tree=tree_before)
            after_doc = document_service.get_document(doc_id)
            envelope["ok"] = False
            envelope["result"] = {}
            envelope["error"] = EvaluationFailed(
                f"{eval_error} (the edit was rolled back)").to_dict()
        else:
            envelope["warnings"].append({"code": EvaluationFailed.code, "message": eval_error})

    if mutates and after_doc is not None and not after_doc.derived.error:
        lost = _lost_references(doc_id, after_doc)
        if lost:
            # The body built, so nothing else here will notice — but a fillet that
            # was placed on four edges and now reaches three is a part that
            # quietly stopped matching what was asked for, which is exactly the
            # silent success this catalog is not allowed to contain.
            envelope["warnings"].append({
                "code": PreconditionFailed.code,
                "message": f"{lost} face/edge selection(s) no longer resolve after this "
                           f"edit; call check_references to see which"})

    if after_doc is not None:
        envelope["delta"] = observation.delta(before, observation.snapshot(after_doc))
        if include_state:
            envelope["state"] = observation.summarize(after_doc)

    trajectories.record_step(op_name, doc_id, args, envelope, obs_before=obs_before,
                             latency_ms=_elapsed_ms(), rationale=rationale)
    return envelope

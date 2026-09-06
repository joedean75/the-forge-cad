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
from typing import Any, Callable, Dict, List, NamedTuple, Optional

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
    # Which kind of thing the id addresses. Every operation used to take a
    # document, so `doc_id` was the whole story; an assembly is neither a
    # `DesignDocument` nor squeezable into one — it has no feature tree, and what
    # an agent needs to see is which part sits where. Published in the catalog so
    # a caller knows what id to pass rather than discovering it from a 404.
    subject: str = "document"


_VEC = {"type": "object", "properties": {"x": {"type": "number"}, "y": {"type": "number"}, "z": {"type": "number"}}}
_REQ_VEC = {**_VEC, "required": True}
_AXIS = {**_VEC, "description": "direction to cut along; +Z by default"}
# Listed rather than derived from `parts_catalog` at import: the enum reaches
# the JSON Schema a constrained decoder is pointed at, and a schema that
# depends on an import cycle is a schema that is sometimes empty.
_METRIC_SIZES = ("M2", "M2.5", "M3", "M4", "M5", "M6", "M8", "M10", "M12")
# Same reasoning. The generated catalog only fills the first four; the rest
# exist because an imported supplier part has to be filed somewhere, and
# "electronics" is where a connector goes.
_PART_CATEGORIES = ("fastener", "bearing", "motor", "extrusion", "electronics",
                    "pneumatic", "hardware", "other")
_PART_SOURCES = ("3dfindit", "traceparts", "grabcad", "manufacturer", "other")

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

#: The recess a hole has at its mouth. `simple` is a plain bore.
_HOLE_TYPES = ("simple", "counterbore", "countersink")

#: How a pattern places its copies. Imported rather than restated so the enum a
#: constrained decoder reads and the kernel's own table cannot drift apart.
from app.cad_kernel.geom.patterns import PATTERN_KINDS as _PATTERN_KINDS  # noqa: E402

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
        "kind": {"enum": list(_PATTERN_KINDS),
                 "description": "pattern: linear/circular use axis+spacing+center; "
                                "curve spaces `count` instances by equal arc length "
                                "along `path`; table places them at `transforms`"},
        "path": {"type": "array", "items": _VEC,
                 "description": "pattern (curve): the polyline to space instances along"},
        "transforms": {
            "type": "array",
            "items": {"type": "object",
                      "properties": {"translate": _VEC, "rotate": _VEC},
                      "additionalProperties": False},
            "description": "pattern (table): one {translate, rotate} per instance. "
                           "Complete — the first entry is the first instance, and "
                           "there is no implicit copy at the origin"},
        "translate": {**_VEC, "description": "transform"},
        "rotate": {**_VEC, "description": "transform: XYZ euler degrees"},
        "scale": {"type": "number", "description": "transform"},
        "center": {**_VEC, "description": "hole / pattern"},
        "axis": {**_VEC, "description": "hole / pattern"},
        # The machined-hole shape. Present here as well as on `add_hole` because
        # the decompiler emits `add_modifier("hole")` for a `HoleFeature`, and a
        # key the schema drops is a counterbore that silently vanishes on replay.
        "hole_type": {"enum": _HOLE_TYPES, "description": "hole"},
        "recess_radius": {"type": "number", "description": "hole: counterbore/countersink"},
        "recess_depth": {"type": "number", "description": "hole: counterbore/countersink"},
        "countersink_angle": {"type": "number", "description": "hole: included angle"},
        "tip_angle": {"type": "number", "description": "hole: drill point"},
        "through": {"type": "boolean", "description": "hole"},
        "thread": {"type": "string", "description": "hole: tap callout, e.g. M6"},
    },
    "additionalProperties": False,
}

# Populated by @operation as each handler is defined, so a new operation is one
# decorated function — the registry used to be a 180-line list sitting far from
# the handlers it named, which meant editing two distant places and kept the
# description away from the code it describes.
OPERATIONS: Dict[str, Operation] = {}


def operation(name: str, kind: str, description: str, params: Dict[str, Any],
              subject: str = "document"):
    """Register a handler as a callable operation.

    `kind` is "query" (read-only) or "mutate". `params` is the JSON-schema-ish
    shape published to agents in the catalog. `subject` is what the id argument
    names — a part document, or an assembly.
    """

    def decorate(fn):
        if name in OPERATIONS:
            raise ValueError(f"duplicate operation {name!r}")
        OPERATIONS[name] = Operation(name, kind, description, params, fn, subject)
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
                                 "center": center, "axis": axis,
                                 "hole_type": p.get("hole_type", "simple"),
                                 "recess_radius": p.get("recess_radius", 0.0),
                                 "recess_depth": p.get("recess_depth", 0.0),
                                 "countersink_angle": p.get("countersink_angle", 90.0),
                                 "tip_angle": p.get("tip_angle", 0.0),
                                 "through": p.get("through", False),
                                 "thread": p.get("thread", "")}),
        "pattern": ("PatternFeature", {"kind": p.get("kind", "linear"), "count": p.get("count", 3),
                                       "spacing": p.get("spacing", 0.05), "axis": p.get("axis", {"x": 1, "y": 0, "z": 0}),
                                       "center": center,
                                       "path": p.get("path") or [],
                                       "transforms": p.get("transforms") or []}),
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


def _load_case_digest(doc) -> list:
    """The loads and supports a solve reads, as plain data for a fingerprint.

    They live on `document.optimization`, which is a whole setup object — most
    of it (volume fraction, objective, keep regions) is about a SIMP run and
    changes nothing about a linear-static solve. Fingerprinting the whole thing
    would call a stress report stale because somebody moved a slider that FEA
    never looks at.
    """
    setup = getattr(doc, "optimization", None)
    if setup is None:
        return []
    return [lc.model_dump() for lc in (setup.load_cases or [])]


def _record(doc, kind: str, result: dict,
            settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """File a derived reading against the document and stamp the result.

    Two things come back to the caller that the raw report cannot carry: the
    fingerprint of the inputs it was a reading of, and whether it is still
    current. Before this the answer lived only in the browser tab that ran the
    analysis — cleared on the next edit, and simply absent after a reload or for
    a second viewer, who saw a report with no way to tell what it was about.

    The fingerprint is taken *before* the work in the callers that take time
    (see `derived_cache.publish`); here the work has already finished
    synchronously, so the document cannot have moved underneath it.
    """
    from app.services import derived_cache

    derived_cache.publish(doc, kind, result, settings=settings)
    document_service.persist_derived(doc)
    product = derived_cache.held(doc, kind)
    return {**result,
            "fingerprint": product.fingerprint,
            "computed_at": product.computed_at}


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
        # A new document is three datum planes and no material, so its extent
        # really is zero. Reported in the shape every caller already reads —
        # `size_mm` going missing would be a second thing to handle — with
        # `body` saying which kind of zero this is.
        return {"size_mm": {"x": 0.0, "y": 0.0, "z": 0.0}, "mass_kg": 0.0,
                "body": False,
                "note": "this document has no body yet — add a solid first"}
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
            "solver_message": solved["message"],
            # Present only when the sketch is heading for the solver's ceiling.
            # A band below the refusal, because the sketch that gets refused is
            # usually the one after the one that took four seconds — and the
            # caller is the only one who can decide to split it.
            **({"size_note": solved["size_note"]} if solved.get("size_note") else {})}


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


@operation("add_plane", "mutate",
           "Add a datum plane — a reference to sketch on or measure from, not "
           "material. A new document starts with three (Top/Front/Right) and they "
           "are ordinary features: delete_feature removes one, and this puts one "
           "back at any orientation and offset. An offset plane is how you sketch "
           "at a height nothing has a face at yet.",
           {"plane": {"enum": ["xy", "xz", "yz"], "default": "xy",
                      "description": "orientation: xy (top), xz (front), yz (right)"},
            "offset": {"type": "number", "default": 0.0,
                       "description": "distance along the plane normal"},
            "size": {"type": "number",
                     "description": "how big to draw it — a display extent, not a "
                                    "boundary; the plane itself is unbounded"},
            "name": {"type": "string", "description": "a name for the plane"}})
def _add_plane(doc_id, args):
    from app.cad_kernel.ops.datum_plane import DEFAULT_SIZE, NORMAL_AXIS

    doc = _get(doc_id)
    plane = args.get("plane", "xy")
    offset = float(args.get("offset", 0.0) or 0.0)
    node = {
        "type": feature_ops.DATUM_PLANE_TYPE,
        "name": args.get("name") or f"Plane {plane.upper()}",
        "params": {"plane": plane, "offset": offset,
                   "size": float(args.get("size") or DEFAULT_SIZE)},
    }
    # Joined as an ordinary union child, exactly like a sketch: it takes its id
    # from the one allocator, and the prune stage lifts it back out before
    # anything evaluates, so the body is unchanged.
    tree = feature_ops.add_primitive(doc.feature_tree, node, "union")
    document_service.update_document(doc_id, feature_tree=tree)
    return {"message": f"added {node['name']}", "feature_id": node["id"],
            "plane": plane, "offset_mm": round(offset * 1000, 4),
            "normal_axis": NORMAL_AXIS[plane]}


@operation("list_planes", "query",
           "The datum planes in this document: orientation, offset and whether "
           "anything can still be sketched on them.",
           {})
def _list_planes(doc_id, args):
    from app.cad_kernel.core.feature_tree import walk_nodes
    from app.cad_kernel.ops.datum_plane import NORMAL_AXIS

    doc = _get(doc_id)
    planes = []
    for node in walk_nodes(doc.feature_tree):
        if node.get("type") != feature_ops.DATUM_PLANE_TYPE:
            continue
        params = node.get("params") or {}
        plane = params.get("plane", "xy")
        planes.append({
            "feature_id": node.get("id"), "name": node.get("name"),
            "plane": plane, "offset_mm": round(float(params.get("offset", 0.0)) * 1000, 4),
            "normal_axis": NORMAL_AXIS.get(plane, "z"),
        })
    return {"planes": planes, "count": len(planes)}


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
           "description": "ignore edges shorter than this — useful for skipping slivers"},
           "polyline": {"type": "boolean", "default": False,
           "description": "also return points along each edge (mm) — for drawing and "
                          "hit-testing in a viewport. Off by default: it is a lot of "
                          "numbers and it tells a planner nothing."}})
def _list_edges(doc_id, args):
    from app.cad_kernel.core import naming

    shape = _exact_shape(doc_id)
    table = naming.edge_table(shape)
    wanted = str(args.get("kind", "all"))
    floor = float(args.get("min_length_mm") or 0.0)
    if wanted != "all":
        table = [e for e in table if e["kind"] == wanted]
    table = [e for e in table if e["length_mm"] >= floor]

    shapes = {}
    if args.get("polyline"):
        # Re-explored rather than carried on the descriptor: `edge_table` is on
        # the hot path of every topology operation and a polyline is the one
        # thing in this listing nothing else wants.
        shapes = {naming.edge_descriptor(edge, naming.body_bounds(shape))["id"]: edge
                  for edge in naming.edges(shape)}

    rows = []
    for e in table:
        row = {"id": e["id"], "kind": e["kind"], "length_mm": e["length_mm"],
               "midpoint_mm": e["midpoint_mm"],
               **({"radius_mm": e["radius_mm"]} if e.get("radius_mm") else {})}
        edge = shapes.get(e["id"])
        if edge is not None:
            row["polyline_mm"] = naming.edge_polyline(edge)
        rows.append(row)
    return {"edges": rows, "count": len(rows)}


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
           "edges, not a blend between bodies. Pass `radius_end` for a variable "
           "fillet that runs from one radius to the other along each edge. Pick "
           "edge ids with list_edges. Requires the exact kernel.",
           {"edges": _EDGE_SELECTION,
           "radius": {"type": "number", "required": True, "description": "metres"},
           "radius_end": {"type": "number",
                          "description": "radius at the far end of each edge, for a "
                                         "variable fillet. Omit for a constant one."},
           "target": _MODIFIER_TARGET})
def _fillet_edges(doc_id, args):
    doc = _get(doc_id)
    radius = float(require(args, "radius"))
    if radius <= 0:
        raise BadArgument("radius must be positive", arg="radius")
    radius_end = float(args.get("radius_end") or 0.0)
    if radius_end < 0:
        raise BadArgument("radius_end must be positive", arg="radius_end")
    references = _selected(doc_id, require(args, "edges"), "edge")

    node = {"type": "EdgeFilletFeature", "name": "Fillet",
            "params": {"radius": radius, "radius_end": radius_end,
                       "edges": references}}
    tree = feature_ops.add_modifier(doc.feature_tree, node, target_id=args.get("target"))
    span = (f"a {radius * 1000:g}-{radius_end * 1000:g} mm variable fillet"
            if radius_end > 0 else f"a {radius * 1000:g} mm fillet")
    _apply_exact(doc_id, doc.feature_tree, tree,
                 f"{span} on {len(references)} edge(s)")
    return {"message": f"filleted {len(references)} edge(s) — {span}",
            "feature_id": node["id"], "edges": [r["id"] for r in references],
            "variable": radius_end > 0}


@operation("add_mate_connector", "mutate",
           "Put a named coordinate frame on the part for assemblies to mate "
           "against. `z_axis` is the mate axis — what a revolute turns about and "
           "a slider travels along — and `x_axis` fixes rotation about it. The "
           "frame lives in the part, so editing the part carries every mate that "
           "uses it. Contributes no geometry.",
           {"origin": {**_VEC, "description": "where the frame sits, in part space"},
            "z_axis": {**_VEC, "description": "the mate axis; +Z by default"},
            "x_axis": {**_VEC, "description": "in-plane reference fixing rotation "
                                              "about the mate axis; +X by default"},
            "name": {"type": "string",
                     "description": "what to call it, e.g. 'left bearing bore'"}})
def _add_mate_connector(doc_id, args):
    doc = _get(doc_id)
    node = {"type": "MateConnectorFeature",
            "name": str(args.get("name") or "Mate connector"),
            "params": {"origin": args.get("origin") or {"x": 0.0, "y": 0.0, "z": 0.0},
                       "z_axis": args.get("z_axis") or {"x": 0.0, "y": 0.0, "z": 1.0},
                       "x_axis": args.get("x_axis") or {"x": 1.0, "y": 0.0, "z": 0.0}}}
    # Bodyless, so it joins the tree the way a datum plane does — beside the
    # body rather than wrapping it. A union with a frame would be a boolean with
    # a child that has no solid, which `resolve_sketches` then prunes to nothing.
    tree = feature_ops.add_primitive(doc.feature_tree, node, "union")
    document_service.update_document(doc_id, feature_tree=tree)
    return {"message": f"added mate connector {node['name']!r}",
            "feature_id": node["id"], "connector_id": node["id"]}


@operation("list_mate_connectors", "query",
           "The named frames on this part that an assembly can mate against.",
           {})
def _list_mate_connectors(doc_id, args):
    from app.cad_kernel.ops.mate_connector import MateConnectorFeature

    doc = _get(doc_id)
    out = []
    for node in feature_ops.flatten(doc.feature_tree):
        if node.get("type") != "MateConnectorFeature":
            continue
        origin, rotation = MateConnectorFeature.from_dict(node).frame()
        out.append({
            "connector_id": node.get("id"),
            "name": node.get("name"),
            # Millimetres out, as everywhere an observation is read back.
            "origin_mm": [round(float(c) * 1000.0, 4) for c in origin],
            # Orthonormalised, which is what the solver will actually use — a
            # caller that supplied a skewed x_axis should see what it became.
            "x_axis": [round(float(c), 6) for c in rotation[:, 0]],
            "z_axis": [round(float(c), 6) for c in rotation[:, 2]],
        })
    return {"connectors": out}


@operation("add_helix", "mutate",
           "Wind a round wire into a helix — a coil spring, an auger flight, a "
           "coil. A true helix, not a polyline sweep: `add_sweep` is a union of "
           "capsules and facets visibly at any segment count a document can "
           "afford. Requires the exact kernel.",
           {"helix_radius": {"type": "number", "default": 0.01,
                             "description": "of the coil, to the wire's centreline"},
            "wire_radius": {"type": "number", "default": 0.001,
                            "description": "of the round section being wound"},
            "pitch": {"type": "number", "default": 0.004,
                      "description": "rise per turn; must exceed the wire diameter "
                                     "or consecutive turns intersect"},
            "turns": {"type": "number", "default": 5.0,
                      "description": "how many revolutions; may be fractional"},
            "center": {**_VEC, "description": "where the axis starts"},
            "axis": {**_VEC, "description": "which way it winds up; +Z by default"},
            "right_handed": {"type": "boolean", "default": True,
                             "description": "handedness — a real distinction for a "
                                            "spring that has to nest with something"},
            "target": _MODIFIER_TARGET})
def _add_helix(doc_id, args):
    doc = _get(doc_id)
    node = {"type": "HelixFeature", "name": "Helix",
            "params": {"helix_radius": float(args.get("helix_radius") or 0.01),
                       "wire_radius": float(args.get("wire_radius") or 0.001),
                       "pitch": float(args.get("pitch") or 0.004),
                       "turns": float(args.get("turns") or 5.0),
                       "center": args.get("center") or {"x": 0.0, "y": 0.0, "z": 0.0},
                       "axis": args.get("axis") or {"x": 0.0, "y": 0.0, "z": 1.0},
                       "right_handed": bool(args.get("right_handed", True))}}
    p = node["params"]
    if p["helix_radius"] <= 0 or p["wire_radius"] <= 0 or p["turns"] <= 0:
        raise BadArgument("a helix needs a positive radius, wire and turn count",
                          arg="helix_radius")
    if p["wire_radius"] >= p["helix_radius"]:
        raise BadArgument(
            f"the wire ({p['wire_radius'] * 1000:g} mm radius) is thicker than the "
            f"coil it is wound on ({p['helix_radius'] * 1000:g} mm) — the turns "
            f"would pass through the axis", arg="wire_radius")
    if p["pitch"] <= 2.0 * p["wire_radius"]:
        raise BadArgument(
            f"a pitch of {p['pitch'] * 1000:g} mm is not more than the wire "
            f"diameter ({p['wire_radius'] * 2000:g} mm), so consecutive turns "
            f"would intersect", arg="pitch")

    tree = feature_ops.add_primitive(doc.feature_tree, node, "union",
                                     target_id=args.get("target"))
    _apply_exact(doc_id, doc.feature_tree, tree,
                 f"a {p['turns']:g}-turn helix")
    return {"message": f"wound a {p['turns']:g}-turn helix of "
                       f"{p['wire_radius'] * 2000:g} mm wire",
            "feature_id": node["id"],
            "free_length_mm": round(p["pitch"] * p["turns"] * 1000, 3)}


@operation("add_draft", "mutate",
           "Taper named faces away from a pull direction so the part releases "
           "from a mould or a die. This is the treatment for what `dfm` with "
           "process=molding already measures and reports. The faces pivot about "
           "the body's parting line — its extreme against the pull — so the "
           "part is tapered rather than resized. Pick face ids with list_faces. "
           "Requires the exact kernel.",
           {"faces": {"type": ["array", "string"], "required": True,
                      "description": 'face ids from list_faces, or "all"'},
            "angle": {"type": "number", "default": 2.0,
                      "description": "draft angle; 1-3 is the usual range for injection "
                                     "moulding, more for a textured surface"},
            "pull": {**_VEC, "description": "the direction the tool opens in; +Z by default"},
            "neutral_offset": {"type": "number",
                               "description": "move the pivot plane along the pull "
                                              "direction from the parting line"},
            "target": _MODIFIER_TARGET})
def _add_draft(doc_id, args):
    doc = _get(doc_id)
    angle = float(args.get("angle") or 2.0)
    if not 0 < angle < 90:
        raise BadArgument(
            f"a draft angle of {angle:g} degrees is not a taper — use 0 < angle < 90",
            arg="angle")
    references = _selected(doc_id, require(args, "faces"), "face")

    node = {"type": "DraftFeature", "name": "Draft",
            "params": {"angle": angle,
                       "pull": args.get("pull") or {"x": 0.0, "y": 0.0, "z": 1.0},
                       "neutral_offset": float(args.get("neutral_offset") or 0.0),
                       "faces": references}}
    tree = feature_ops.add_modifier(doc.feature_tree, node, target_id=args.get("target"))
    _apply_exact(doc_id, doc.feature_tree, tree,
                 f"a {angle:g}° draft on {len(references)} face(s)")
    return {"message": f"drafted {len(references)} face(s) by {angle:g}°",
            "feature_id": node["id"], "faces": [r["id"] for r in references],
            "angle": angle}


@operation("add_rib", "mutate",
           "Fuse a stiffening rib onto the body: a wall running from `start` to "
           "`end`, growing `height` along `up`, `thickness` at its tip and "
           "opened out at the base by `draft`. Dimensioned rather than grown to "
           "the next face — the height is a parameter — but the tip, draft and "
           "height stay in the relationship a moulder needs, which a hand-built "
           "box does not.",
           # Explicit units: `start` is a boolean elsewhere in this catalog
           # (`print_part`), so the name alone cannot decide, and a descriptor's
           # own unit is what `units.unit_of` reads first.
           {"start": {**_VEC, "required": True, "unit": "m",
                      "description": "one end of the rib's centreline, at its base"},
            "end": {**_VEC, "required": True, "unit": "m",
                    "description": "the other end"},
            "up": {**_VEC, "description": "the direction the rib grows; +Z by default"},
            "thickness": {"type": "number", "default": 0.002,
                          "description": "at the tip, which is what a mould tool is cut to"},
            "height": {"type": "number", "default": 0.01, "description": "how tall"},
            "draft": {"type": "number", "default": 0.0,
                      "description": "per side, opening the base out"},
            "target": _MODIFIER_TARGET})
def _add_rib(doc_id, args):
    doc = _get(doc_id)
    if not feature_ops.has_solid(doc.feature_tree):
        raise PreconditionFailed(
            "a rib stiffens a body and there is no body yet — add one with "
            "add_primitive, add_extrude or apply_template first.")
    start, end = require(args, "start"), require(args, "end")
    thickness = float(args.get("thickness") or 0.002)
    height = float(args.get("height") or 0.01)
    if thickness <= 0 or height <= 0:
        raise BadArgument("a rib needs a positive thickness and height",
                          arg="thickness" if thickness <= 0 else "height")
    draft = float(args.get("draft") or 0.0)
    if not 0 <= draft < 90:
        raise BadArgument("draft is per side and must be 0 <= draft < 90", arg="draft")

    node = {"type": "RibFeature", "name": "Rib",
            "params": {"start": dict(start), "end": dict(end),
                       "up": args.get("up") or {"x": 0.0, "y": 0.0, "z": 1.0},
                       "thickness": thickness, "height": height, "draft": draft}}
    # Validated here rather than at evaluation: a rib whose `up` is parallel to
    # its run has no cross-section, and `derived.error` would report that as a
    # document-wide warning instead of naming the argument that caused it.
    from app.cad_kernel.geom import ribs
    try:
        ribs.rib_prism(start=(start["x"], start["y"], start["z"]),
                       end=(end["x"], end["y"], end["z"]),
                       up=(node["params"]["up"]["x"], node["params"]["up"]["y"],
                           node["params"]["up"]["z"]),
                       thickness=thickness, height=height, draft=draft)
    except ValueError as exc:
        raise BadArgument(str(exc), arg="up") from exc

    tree = feature_ops.add_modifier(doc.feature_tree, node, target_id=args.get("target"))
    document_service.update_document(doc_id, feature_tree=tree)
    return {"message": f"added a {thickness * 1000:g} mm rib, {height * 1000:g} mm tall",
            "feature_id": node["id"], "draft": draft}


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
_ADDRESSES_TOPOLOGY = {"EdgeFilletFeature", "EdgeChamferFeature", "FaceShellFeature",
                       "DraftFeature"}


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

    doc = _get(doc_id)  # typed NoSuchDocument rather than the router's HTTPException
    process = str(args.get("process", "additive"))
    build_axis = str(args.get("build_axis", "auto"))
    resolution = int(args.get("resolution", 128))
    try:
        report = document_analysis.dfm_document(
            doc_id, build_axis=build_axis, resolution=resolution, process=process)
    except ValueError as exc:
        # "this part has no exact form" is a different previous step, not a
        # different argument — model it the way the rest of the catalog does.
        raise PreconditionFailed(str(exc)) from exc
    # The requested process, not `report.process` — that one is a display label
    # ('LPBF', 'MILLING'), so fingerprinting on it would make an additive report
    # and a milling report indistinguishable to the freshness check. The
    # `resolution` argument is a *setting* here, not the geometry's: it meshes
    # the additive rows, and the body was evaluated at whatever it was
    # evaluated at.
    return _record(doc, "dfm", report.model_dump(),
                   {"process": process, "build_axis": build_axis,
                    "resolution": resolution})


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

    doc = _get(doc_id)
    resolution = int(args.get("resolution", 128))
    elements = int(args["elements"]) if args.get("elements") else None
    report = document_analysis.analyse_document(
        doc_id, resolution=resolution, elements=elements)
    return _record(doc, "analysis", report,
                   {"elements": elements, "load_cases": _load_case_digest(doc)})


@operation("verify_toolpath", "query",
           "Cut the stock with the planned 2.5-axis path and compare what is "
           "left against the finished part. Reports gouge — material inside the "
           "part that the cut removed, which is a scrapped part and so a failing "
           "check — and leftover, stock still standing that is not part of the "
           "body. Some leftover is unavoidable: a 2.5-axis path cannot reach an "
           "undercut. This checks the same passes the G-code is written from.",
           {"diameter_mm": {"type": "number", "default": 3.0, "unit": "mm",
                            "description": "flat endmill diameter"},
            "stepdown_mm": {"type": "number", "default": 1.0, "unit": "mm",
                            "description": "depth of cut per level"},
            "stepover_mm": {"type": "number", "default": 1.2, "unit": "mm",
                            "description": "sideways bite between raster passes"},
            "stock_margin_mm": {"type": "number", "default": 4.0, "unit": "mm",
                                "description": "stock added around the part"},
            "samples": {"type": "integer", "default": 128,
                        "description": "grid samples across the longest side; a "
                                       "feature thinner than one cell is invisible "
                                       "to the comparison"}})
def _verify_toolpath(doc_id, args):
    from app.services.cam.toolpath import ToolParams
    from app.services.cam.verify import verify_toolpaths

    doc = _get(doc_id)
    if not feature_ops.has_solid(doc.feature_tree):
        raise PreconditionFailed(
            "there is nothing to machine — this document has no body yet")
    tree = document_service.resolved_tree(doc)
    # `x or default` would turn an explicit 0 into the default and machine the
    # part with a 3 mm cutter the caller never asked for. A zero tool is a
    # mistake worth naming, not one worth silently correcting.
    def _mm(name: str, default: float) -> float:
        value = args.get(name)
        return default if value is None else float(value)

    tool = ToolParams(diameter_mm=_mm("diameter_mm", 3.0),
                      stepdown_mm=_mm("stepdown_mm", 1.0),
                      stepover_mm=_mm("stepover_mm", 1.2),
                      stock_margin_mm=_mm("stock_margin_mm", 4.0))
    for name, value in (("diameter_mm", tool.diameter_mm),
                        ("stepdown_mm", tool.stepdown_mm),
                        ("stepover_mm", tool.stepover_mm)):
        if value <= 0:
            raise BadArgument(f"{name} must be positive, got {value:g}", arg=name)
    from app.cad_kernel.core.feature_tree import Feature

    return verify_toolpaths(Feature.from_dict(tree["root"]), tool,
                            samples=int(args.get("samples") or 128))


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
        report = design_spec.verify(doc)
    except design_spec.SpecError as exc:
        raise PreconditionFailed(str(exc))
    return _record(doc, "spec", report)


@operation("list_standard_parts", "query",
           "Browse the purchased parts you can place. Two kinds in one list. "
           "Generated: screws, nuts, washers, dowel pins, ball bearings, NEMA "
           "steppers and T-slot extrusion, built from the published "
           "dimensional standard rather than downloaded from anyone. Imported: "
           "supplier CAD files this account brought in itself. Every row states "
           "`fidelity` (`as-standard` / `envelope` / `supplier`) and `geometry` "
           "(`exact` — faces and edges are addressable — or `mesh`). Each "
           "row\'s `key` goes to insert_standard_part on an assembly.",
           {"category": {"enum": list(_PART_CATEGORIES),
                         "description": "narrow to one kind"},
            "search": {"type": "string",
                       "description": "match against the designation, e.g. \'M4\' or \'NEMA\'"},
            "source": {"enum": list(_PART_SOURCES),
                       "description": "only the parts imported from this "
                                      "portal; omit for the whole catalog"},
            "limit": {"type": "integer", "default": 40,
                      "description": "how many rows to return"}})
def _list_standard_parts(doc_id, args):
    from app.services import caller, standard_parts

    doc = _get(doc_id)
    # The caller's own imported parts, not the document owner's. On a shared
    # document those differ, and answering with the owner's would disclose the
    # designations, manufacturers and part numbers of everything they imported
    # to anyone they granted view access. Falls back to the owner only for a
    # direct in-process call (tests, rollouts), where there is no other caller.
    owner = caller.acting_account_id() or doc.owner_id
    return standard_parts.listing(
        owner, category=args.get("category"), search=args.get("search"),
        source=args.get("source"), limit=int(args.get("limit", 40) or 40))


@operation("list_templates", "query",
           "The starter parts: a plate, an L-bracket, an open-top enclosure and "
           "a stepped shaft. Each is an ordinary feature tree with its driving "
           "dimensions already named as parameters, so the first edit is "
           "changing a number. Build one with apply_template.",
           {})
def _list_templates(doc_id, args):
    from app.services import templates

    _get(doc_id)
    return {"templates": templates.describe()}


@operation("apply_template", "mutate",
           "Build a starter part into this document — a plate, an L-bracket, an "
           "enclosure or a shaft, with named driving parameters. The template "
           "is an operation sequence, so what it builds is an ordinary editable "
           "tree and every dimension is a parameter you can change with "
           "set_parameters. Only into a document with no body yet: browse them "
           "with list_templates.",
           {"template": {"type": "string", "required": True,
                         "description": "template key, e.g. plate or bracket"}})
def _apply_template(doc_id, args):
    from app.services import templates

    doc = _get(doc_id)
    key = require(args, "template")
    template = templates.by_key(key)
    if template is None:
        raise BadArgument(
            f"no template {key!r}; browse them with list_templates "
            f"({', '.join(sorted(templates.TEMPLATES))})", arg="template")
    # A template names its own parameters and combines with the whole body. Run
    # into a part that already has one and it unions a plate onto whatever was
    # there and silently rebinds `length` — so this is a starting point or it is
    # nothing. The remedy is a different earlier step: a new document.
    if feature_ops.has_solid(doc.feature_tree):
        raise PreconditionFailed(
            f"'{template.key}' is a starting point and this document already has "
            f"a body; apply it to a new part instead")

    outcome = templates.apply(template.key, doc_id)
    if not outcome.get("ok"):
        raise PreconditionFailed(
            f"the '{template.key}' template stopped at step {outcome['failed_step']} "
            f"({outcome['failed_op']}): {outcome['error']}")
    return {"message": f"built {outcome['name']} with "
                       f"{len(outcome['parameters'])} driving parameters",
            **outcome}


def _axis_extent(bbox, axis: Dict[str, float]) -> tuple:
    """`(centre, half-extent)` of the body along `axis`, both in metres.

    Exact for any direction, not just the six axis-aligned ones: the support
    function of a box in direction *n* is `sum(|n_i| * half_i)`, so a hole
    drilled along (1,1,1) gets its mouth placed on the real silhouette rather
    than on a guess.
    """
    import math as _math

    n = (float(axis.get("x", 0.0)), float(axis.get("y", 0.0)), float(axis.get("z", 1.0)))
    length = _math.sqrt(sum(c * c for c in n)) or 1.0
    n = tuple(c / length for c in n)
    lo = (bbox.min.x, bbox.min.y, bbox.min.z)
    hi = (bbox.max.x, bbox.max.y, bbox.max.z)
    centre = tuple((a + b) / 2.0 for a, b in zip(lo, hi))
    half = sum(abs(n[i]) * (hi[i] - lo[i]) / 2.0 for i in range(3))
    return centre, n, half


@operation("add_hole", "mutate",
           "Drill a hole, with the recess a real one has: plain, counterbored "
           "for a socket head, or countersunk for a flat head. `tapped` takes a "
           "metric designation and drills the tap-drill size for it (an M6 gets "
           "5 mm, not 6) and records the thread as a callout — the helix itself "
           "is not modelled. `depth` measures to the shoulder, so a `tip_angle` "
           "adds a drill point below it rather than moving the bottom. With no "
           "`center` the mouth is placed on the body's own face along `axis`, "
           "which is what makes a counterbore land in material.",
           {"radius": {"type": "number",
                       "description": "bore radius; ignored when `tapped` is given"},
            "depth": {"type": "number",
                      "description": "to the shoulder. Defaults to the body's extent along `axis`."},
            "center": {**_VEC, "description": "bore midpoint. Defaults to the body's centre "
                                              "offset so the mouth sits on its face."},
            "axis": _AXIS,
            "hole_type": {"enum": list(_HOLE_TYPES), "default": "simple",
                          "description": "the recess at the mouth"},
            "recess_radius": {"type": "number",
                              "description": "counterbore / countersink radius at the mouth"},
            "recess_depth": {"type": "number",
                             "description": "counterbore depth; for a countersink, "
                                            "overrides the angle"},
            "countersink_angle": {"type": "number", "default": 90.0,
                                  "description": "countersink included angle"},
            "tip_angle": {"type": "number", "default": 0.0,
                          "description": "drill point below the shoulder; 118 is a jobber "
                                         "drill, 0 leaves a flat bottom"},
            "through": {"type": "boolean", "default": False,
                        "description": "cut all the way out of the far side"},
            "tapped": {"enum": list(_METRIC_SIZES),
                       "description": "drill the tap-drill size for this thread and record it"},
            "target": _MODIFIER_TARGET})
def _add_hole(doc_id, args):
    from app.cad_kernel.geom import holes

    doc = _get(doc_id)
    if not feature_ops.has_solid(doc.feature_tree):
        raise PreconditionFailed(
            "there is no material to drill through — this document has no body "
            "yet. Add one with add_primitive, add_extrude or apply_template first.")

    tapped = args.get("tapped")
    if tapped and args.get("radius") is not None:
        raise BadArgument(
            "pass either 'radius' or 'tapped', not both — a tapped hole's bore "
            "is the tap-drill size for the thread and is not free to choose.",
            arg="tapped")
    if tapped:
        try:
            radius = holes.tap_drill_radius(tapped)
        except KeyError as exc:
            raise BadArgument(f"unknown thread: {tapped}", arg="tapped") from exc
    else:
        radius = float(args.get("radius") or 0.005)
    if radius <= 0:
        raise BadArgument("hole radius must be positive", arg="radius")

    axis = args.get("axis") or {"x": 0.0, "y": 0.0, "z": 1.0}
    bbox = doc.derived.bbox
    if bbox is not None:
        body_centre, unit, half = _axis_extent(bbox, axis)
    else:
        body_centre, unit, half = (0.0, 0.0, 0.0), (0.0, 0.0, 1.0), 0.05

    depth = float(args.get("depth") or (2.0 * half))
    if depth <= 0:
        raise BadArgument("hole depth must be positive", arg="depth")

    if args.get("center") is not None:
        centre = args["center"]
    else:
        # Mouth flush with the body's face along the axis, bore reaching inward.
        mouth = tuple(body_centre[i] + unit[i] * half for i in range(3))
        centre = {"x": mouth[0] - unit[0] * depth / 2.0,
                  "y": mouth[1] - unit[1] * depth / 2.0,
                  "z": mouth[2] - unit[2] * depth / 2.0}

    hole_type = str(args.get("hole_type") or "simple")
    recess_radius = float(args.get("recess_radius") or 0.0)
    if hole_type != "simple" and recess_radius <= radius:
        raise BadArgument(
            f"a {hole_type} needs 'recess_radius' larger than the bore radius "
            f"({radius * 1000:.2f} mm); got {recess_radius * 1000:.2f} mm.",
            arg="recess_radius")
    if hole_type == "counterbore" and float(args.get("recess_depth") or 0.0) <= 0:
        raise BadArgument("a counterbore needs 'recess_depth'", arg="recess_depth")

    node = {"type": "HoleFeature",
            "name": f"{tapped} tapped hole" if tapped else f"{hole_type.capitalize()} hole",
            "params": {"radius": radius, "depth": depth, "center": dict(centre),
                       "axis": dict(axis), "hole_type": hole_type,
                       "recess_radius": recess_radius,
                       "recess_depth": float(args.get("recess_depth") or 0.0),
                       "countersink_angle": float(args.get("countersink_angle") or 90.0),
                       "tip_angle": float(args.get("tip_angle") or 0.0),
                       "through": bool(args.get("through", False)),
                       "thread": str(tapped or "")}}
    tree = feature_ops.add_modifier(doc.feature_tree, node, target_id=args.get("target"))
    document_service.update_document(doc_id, feature_tree=tree)
    return {"message": f"drilled a {radius * 2000:.2f} mm {hole_type} hole"
                       + (f" tapped {tapped}" if tapped else ""),
            "feature_id": node["id"], "diameter_mm": round(radius * 2000, 3),
            "depth_mm": round(depth * 1000, 3), "hole_type": hole_type,
            "thread": tapped or None, "through": bool(args.get("through", False))}


@operation("clearance_hole", "mutate",
           "Cut a properly sized through-hole for a metric screw — ISO 273 "
           "clearance, so an M4 gets 4.5 mm and not 4. Optionally counterbored "
           "for a socket head. Takes the screw size rather than a diameter, "
           "which is the number you actually know.",
           {"screw": {"enum": list(_METRIC_SIZES), "required": True,
                      "description": "the screw the hole is for, e.g. M4"},
            "center": _VEC,
            "depth": {"type": "number",
                      "description": "how deep to cut. Defaults to through the body."},
            "fit": {"enum": ["fine", "medium", "coarse"], "default": "medium",
                    "description": "ISO 273 series; medium is the general-assembly one"},
            "counterbore": {"type": "boolean", "default": False,
                            "description": "sink the head flush (socket head cap screw)"},
            "axis": _AXIS})
def _clearance_hole(doc_id, args):
    from app.services import parts_catalog

    doc = _get(doc_id)
    # Named `screw`, not `size`: `size` is a *length* everywhere else in this
    # catalog and `units` folds that into the description at build time, so a
    # thread designation under that name would be published as "in metres".
    screw = require(args, "screw")
    try:
        hole = parts_catalog.clearance_hole(screw, str(args.get("fit", "medium")))
    except KeyError as exc:
        raise BadArgument(f"unknown screw or fit: {exc}", arg="screw") from exc

    if not feature_ops.has_solid(doc.feature_tree):
        raise PreconditionFailed(
            "there is no material to drill through — this document has no body yet")

    # One `HoleFeature`, not a clearance cylinder with a second wider cylinder
    # stacked on it. Two features meant `list_features` showed two rows for one
    # hole, editing the diameter left the counterbore behind, and the counterbore
    # depth had to be doubled to compensate for a cylinder that is *centred* on
    # its origin — which is why the old table value was divided by 500 rather
    # than by 1000. The recess is now measured from the mouth, so the published
    # ISO 4762 head height goes in as itself.
    axis = args.get("axis") or {"x": 0.0, "y": 0.0, "z": 1.0}
    bbox = doc.derived.bbox
    if bbox is not None:
        body_centre, unit, half = _axis_extent(bbox, axis)
    else:
        body_centre, unit, half = (0.0, 0.0, 0.0), (0.0, 0.0, 1.0), 0.05
    depth = float(args.get("depth") or (2.0 * half))
    if args.get("center") is not None:
        centre = args["center"]
    else:
        centre = {k: body_centre[i] + unit[i] * (half - depth / 2.0)
                  for i, k in enumerate("xyz")}

    counterbored = bool(args.get("counterbore"))
    node = {"type": "HoleFeature", "name": f"{screw} clearance",
            "params": {"radius": hole["radius_m"], "depth": depth,
                       "center": dict(centre), "axis": dict(axis),
                       "hole_type": "counterbore" if counterbored else "simple",
                       "recess_radius": hole["counterbore_dia_mm"] / 2000.0,
                       "recess_depth": hole["counterbore_depth_mm"] / 1000.0,
                       "through": args.get("depth") is None}}
    tree = feature_ops.add_modifier(doc.feature_tree, node)
    document_service.update_document(doc_id, feature_tree=tree)
    return {"message": f"cut a {hole['diameter_mm']} mm clearance hole for {screw}",
            "feature_id": node["id"], **hole,
            "counterbored": counterbored}


@operation("workflow_status", "query",
           "What each mode of the editor can do right now, and whether what it "
           "is showing is still true. One vocabulary for all six: 'blocked' (a "
           "prerequisite is missing, and it says which and what to do), 'ready', "
           "'queued', 'running' (with a fraction), 'current' (the held result "
           "was computed from exactly these inputs), 'stale' (a result is held "
           "but the inputs moved) and 'failed'. Read this before deciding what "
           "to run: it distinguishes 'nothing computed yet' from 'nothing to "
           "compute with', which an empty panel cannot.",
           {"settings": {"type": "object",
                         "description": "per-mode call settings that affect freshness, "
                                        "e.g. {\"make\": {\"process\": \"milling\"}} — a "
                                        "DFM report for additive is not current for milling"}})
def _workflow_status(doc_id, args):
    from app.services import workflow

    doc = _get(doc_id)
    settings = args.get("settings") or {}
    return {"modes": workflow.overview(doc, settings),
            "package": workflow.status(doc, workflow.PACKAGE,
                                       settings.get("package")).as_dict()}


@operation("start_analysis", "mutate",
           "Queue a linear-static FEA solve as a background job and return its "
           "id. Use this instead of `simulate` when raising `elements`: above "
           "roughly 20k free degrees of freedom the direct solve is abandoned "
           "for conjugate gradient and the wall clock goes to minutes, which is "
           "longer than a request should hold. Poll with `job_status`. The "
           "result is published against the document only if the part, the "
           "material and the load cases have not moved while it ran.",
           {"elements": {"type": "number",
                         "description": "element budget; more is slower and resolves "
                                        "stress further"},
            "resolution": {"type": "number", "default": 128,
                           "description": "meshing resolution for the fallback estimate only"}})
def _start_analysis(doc_id, args):
    from app.services import derived_cache, job_handlers, jobs

    doc = _get(doc_id)
    resolution = int(args.get("resolution", 128))
    elements = args.get("elements")
    settings = {"elements": elements, "load_cases": _load_case_digest(doc)}
    fingerprint = derived_cache.analysis_fingerprint(doc, "analysis", settings)
    try:
        job = jobs.enqueue(job_handlers.ANALYSIS, doc_id,
                           {"elements": elements, "resolution": resolution},
                           owner_id=doc.owner_id, input_fingerprint=fingerprint,
                           document_revision=doc.revision,
                           message="Queued linear-static solve")
    except jobs.QueueFull as full:
        raise PreconditionFailed(str(full)) from full
    return {"job_id": job.id, "status": job.status, "kind": job.kind,
            "input_fingerprint": fingerprint,
            "message": "queued; poll job_status with this job_id"}


@operation("job_status", "query",
           "The state of one background job on this document: queued, running "
           "(with a progress fraction), succeeded, failed, cancel_requested or "
           "cancelled, plus its result or a typed error. Omit `job_id` to list "
           "every job against the document, newest first — which is how a "
           "client that lost track of an id finds the work still running.",
           {"job_id": {"type": "string",
                       "description": "the job to read; omit to list them all"}})
def _job_status(doc_id, args):
    from app.services import jobs

    _get(doc_id)
    job_id = args.get("job_id")
    if not job_id:
        return {"jobs": [j.public() for j in jobs.for_subject(doc_id)]}
    job = jobs.get(str(job_id))
    # The subject check is the access check: the router has already decided the
    # caller may read *this document*, and a job id from another one must not
    # resolve through it.
    if job is None or job.subject_id != doc_id:
        raise NoSuchDocument(f"no job {job_id!r} on this document")
    return job.public()


@operation("cancel_job", "mutate",
           "Ask a background job to stop. A queued job is cancelled outright; a "
           "running one is asked, and stops at the next checkpoint its handler "
           "chooses — a solve already inside a linear factorisation is not "
           "interruptible, so the state goes to 'cancel_requested' first and "
           "the result says so rather than pretending otherwise.",
           {"job_id": {"type": "string", "required": True,
                       "description": "the job to stop"}})
def _cancel_job(doc_id, args):
    from app.services import jobs

    _get(doc_id)
    job_id = str(require(args, "job_id"))
    job = jobs.get(job_id)
    if job is None or job.subject_id != doc_id:
        raise NoSuchDocument(f"no job {job_id!r} on this document")
    return (jobs.request_cancel(job_id) or job).public()


@operation("export", "query",
           "Write the part out as a file somebody else can open. Returns a URL "
           "keyed by the file's content hash — fetch it with GET, it is never "
           "inline. step: exact solid for another CAD package. stl: mesh for a "
           "slicer. pdf: the dimensioned drawing sheet a shop reads. dxf: the "
           "drawing's geometry on layers, for CAM. svg: the sheet as a web image. "
           "Exporting the same body twice returns the same URL.",
           {"format": {"enum": ["step", "stl", "pdf", "dxf", "svg"], "required": True,
                       "description": "which file to write"},
            "spec": {**_DRAWING_SPEC,
                     "description": "drawing formats only (pdf/dxf/svg): which views, "
                                    "sections, dimensions and sheet — see generate_drawing"}})
def _export(doc_id, args):
    from app.services import artifacts

    _get(doc_id)
    return artifacts.export(doc_id, require(args, "format"), spec=args.get("spec"))


@operation("release_package", "query",
           "Build the handover package for a released revision: the exact STEP "
           "solid, the drawing as PDF and DXF, the mesh, a one-line BOM, and the "
           "score against the design spec — one zip, pinned to a revision letter. "
           "Built from the revision snapshot, not from the document as it stands, "
           "so it is a release rather than a screenshot. Needs a released "
           "revision: call release_revision first.",
           {"revision": {"type": "string",
                         "description": "revision letter, e.g. 'A'. Defaults to the latest released."}})
def _release_package(doc_id, args):
    from app.services import release_package

    _get(doc_id)
    return release_package.build(doc_id, revision=args.get("revision"))


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
    from app.models.schemas import CamResult
    from app.services import artifacts, slicer

    settings = slicer.SliceSettings(**(args.get("settings") or {}))
    out = slicer.slice_document(doc_id, settings)
    doc = _get(doc_id)
    doc.derived.cam = CamResult(
        # An artifact URL, not a megabyte of base64 riding on the document. The
        # inline form was stripped on persist, so a reload lost a program the
        # toolpath behind it had not changed.
        gcode_url=artifacts.store_gcode(doc_id, out["gcode"], doc.name or "part"),
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
    from app.services import artifacts
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
    gcode = artifacts.gcode_text(doc_id, doc.derived.cam.gcode_url)
    if gcode is None:
        raise PreconditionFailed(
            "the sliced G-code is no longer available; slice the part again")
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

    return [{**op_schema.describe(o.name, o.kind, o.description, o.params),
             "subject": o.subject}
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


class _Subject(NamedTuple):
    """How to fetch, observe and diff whatever an operation's id names."""

    fetch: Callable[[str], Any]
    summarize: Callable[[Any], dict]
    snapshot: Callable[[Any], dict]
    delta: Callable[[dict, dict], dict]


def _subjects() -> Dict[str, _Subject]:
    from app.services import assembly_service, observation

    return {
        "document": _Subject(document_service.get_document, observation.summarize,
                             observation.snapshot, observation.delta),
        "assembly": _Subject(assembly_service.get_assembly, observation.summarize_assembly,
                             observation.snapshot_assembly, observation.assembly_delta),
    }


class _SubjectTable:
    """Built on first use — `assembly_service` imports this module back."""

    _table: Optional[Dict[str, _Subject]] = None

    def __getitem__(self, name: str) -> _Subject:
        if _SubjectTable._table is None:
            _SubjectTable._table = _subjects()
        return _SubjectTable._table[name]


_SUBJECTS = _SubjectTable()


def execute(
    op_name: str,
    doc_id: str,
    args: Dict[str, Any] | None = None,
    *,
    include_state: bool = True,
    rollback_on_eval_error: bool = False,
    rationale: str = "",
    expected_revision: Optional[int] = None,
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
    from app.services import trajectories

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

    # Mutating operations run under the document's write lock, so the
    # expected-revision check and the write it guards cannot be separated.
    # Without that they can be: two edits arriving together both read revision
    # 3, both pass the check, both apply, and both report revision 4 — handing a
    # caller that chains `expected_revision` a number its own write never
    # produced. Queries take no lock; they only read.
    if op.kind == "mutate":
        with document_service.writing(doc_id):
            return _apply(op, op_name, doc_id, args, started,
                          include_state=include_state,
                          rollback_on_eval_error=rollback_on_eval_error,
                          rationale=rationale, expected_revision=expected_revision)
    return _apply(op, op_name, doc_id, args, started,
                  include_state=include_state,
                  rollback_on_eval_error=rollback_on_eval_error,
                  rationale=rationale, expected_revision=expected_revision)


def _apply(op, op_name: str, doc_id: str, args: Dict[str, Any] | None,
           started: float, *, include_state: bool, rollback_on_eval_error: bool,
           rationale: str, expected_revision: Optional[int]) -> Dict[str, Any]:
    """`execute`'s body, so the mutating path can be wrapped in one lock.

    Split out purely to keep the critical section a `with` block rather than
    ninety lines of re-indented code — the flow is unchanged.
    """
    from app.services import trajectories

    subject = _SUBJECTS[op.subject]
    doc = subject.fetch(doc_id)
    if doc is None:
        env = {"ok": False, "op": op_name, "result": {}, "warnings": [],
               "error": NoSuchDocument(f"no {op.subject} {doc_id!r}").to_dict()}
        trajectories.record_step(op_name, doc_id, args, env, rationale=rationale)
        return env

    mutates = op.kind == "mutate"
    if mutates and expected_revision is not None:
        # Checked here, before the handler runs, rather than inside each of the
        # sixty mutators. A conflict means the caller wrote against a document
        # somebody else has since moved; the fix is to re-read and decide, not
        # to change an argument, which is what makes PRECONDITION the right code
        # and its remedy the right advice.
        actual = getattr(doc, "revision", 0)
        if actual != expected_revision:
            env = {
                "ok": False, "op": op_name, "result": {}, "warnings": [],
                "error": PreconditionFailed(
                    f"{op.subject} {doc_id} is at revision {actual}, not "
                    f"{expected_revision}; it changed since you read it — "
                    f"re-read it and re-apply the change").to_dict(),
                "revision": actual,
            }
            trajectories.record_step(op_name, doc_id, args, env, rationale=rationale)
            return env
    before = subject.snapshot(doc)
    obs_before = subject.summarize(doc)
    # Only a mutating op can need rolling back, and only then is the copy worth it.
    tree_before = copy.deepcopy(doc.feature_tree) if (mutates and rollback_on_eval_error) else None

    def _elapsed_ms() -> float:
        return round((time.perf_counter() - started) * 1000, 2)

    try:
        from app.services import instrumentation, op_schema

        op_schema.validate(op.params, args, op=op_name)
        with instrumentation.span(f"op.{op_name}"):
            result = op.handler(doc_id, args or {})
    except Exception as exc:  # noqa: BLE001 — classified, never swallowed
        err = _as_operation_error(exc)
        if isinstance(err, InternalError):
            _LOGGER.exception("operation %s failed unexpectedly", op_name)
        after_doc = subject.fetch(doc_id)
        env = {
            "ok": False, "op": op_name, "result": {}, "error": err.to_dict(), "warnings": [],
            "state": subject.summarize(after_doc) if (include_state and after_doc) else None,
        }
        # A failed call is training data too — arguably the most valuable kind,
        # since it is what teaches recovery.
        trajectories.record_step(op_name, doc_id, args, env, obs_before=obs_before,
                                 latency_ms=_elapsed_ms(), rationale=rationale)
        return env

    from app.services import units

    after_doc = subject.fetch(doc_id)
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

    if op.subject == "document" and mutates and after_doc is not None and after_doc.derived.error:
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

    if (op.subject == "document" and mutates and after_doc is not None
            and not after_doc.derived.error):
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
        envelope["delta"] = subject.delta(before, subject.snapshot(after_doc))
        # The revision this call left behind, so a caller chaining writes can
        # pass it as the next `expected_revision` without a re-read. Absent for
        # an assembly, which has no revision counter of its own.
        if hasattr(after_doc, "revision"):
            envelope["revision"] = after_doc.revision
        if include_state:
            envelope["state"] = subject.summarize(after_doc)

    trajectories.record_step(op_name, doc_id, args, envelope, obs_before=obs_before,
                             latency_ms=_elapsed_ms(), rationale=rationale)
    return envelope


# Registration by import, at the bottom because `assembly_operations` imports
# `operation` from this module: by the time this line runs everything it needs
# exists. The alternative — a list of modules to load — is the import list this
# codebase has removed everywhere else.
from app.services import assembly_operations  # noqa: E402,F401  (registers assembly ops)

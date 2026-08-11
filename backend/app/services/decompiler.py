"""Recover the operation sequence that would build an existing feature tree.

The generation path that matters most produces no tool-use data at all:
``create_document(prompt=…)`` asks an LLM for a whole feature tree in one shot,
so the action space is never exercised and nothing is learned about *calling* it.
Waiting for agents to build a corpus one session at a time is slow.

This turns the problem around. Every tree already in the datastore — however it
was authored — implies a sequence of operations that would construct it. Walking
the tree and emitting that sequence converts the existing corpus, and every
future whole-tree generation, into tool-use trajectories without running a model.

The load-bearing property is that it is **self-verifying**. A decompiled sequence
is replayed and the resulting geometry hashed against the original; only an exact
match is offered as training data. Wrong demonstrations are worse than none, so
anything that does not reproduce is reported as a failure rather than exported.
This is why deterministic feature ids had to come first — without them a replay
cannot be compared to what it replays.

Not everything is recoverable. ``ImportFeature`` and ``OptimizeFeature`` carry
opaque voxel caches that no operation can reconstruct; trees containing them are
reported unsupported instead of being silently approximated.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from app.cad_kernel.core.feature_tree import FeatureTree, node_children
from app.cad_kernel.geom.meshing import mesh_hash
from app.cad_kernel.kernel import Kernel

_LOGGER = logging.getLogger(__name__)

# Primitive feature type -> (shape name, the params add_primitive understands).
_PRIMITIVES: Dict[str, Tuple[str, Tuple[str, ...]]] = {
    "BoxFeature": ("box", ("size", "center")),
    "SphereFeature": ("sphere", ("radius", "center")),
    "CylinderFeature": ("cylinder", ("radius", "height", "center", "axis")),
    "ConeFeature": ("cone", ("base_radius", "height", "center", "axis")),
}

# Feature type -> (op name, the args that op takes). These build a body directly
# rather than going through add_primitive's shape table.
_BUILDERS: Dict[str, Tuple[str, Tuple[str, ...]]] = {
    "ExtrudeFeature": ("add_extrude", ("profile", "plane", "offset", "depth")),
    "RevolveFeature": ("add_revolve", ("profile", "axis", "center", "angle")),
    "SweepFeature": ("add_sweep", ("path", "radius")),
    "LoftFeature": ("add_loft", ("profile_a", "profile_b", "plane", "offset_a", "offset_b")),
    "RuledLoftFeature": ("add_ruled_loft",
                         ("profile_a", "profile_b", "plane", "offset_a", "offset_b", "ruled")),
    "SheetMetalFeature": ("add_sheet_metal",
                          ("thickness", "length", "width", "bend_radius", "k_factor", "flanges")),
}

# Feature type -> (modifier name, its params) for add_modifier.
#
# FilletFeature/ChamferFeature are absent on purpose. They are smooth booleans
# between bodies, and no operation constructs one any more: `add_modifier`
# ("fillet") now builds a real `EdgeFilletFeature`, so emitting it here would
# replay into a different shape. See `_BLENDS` below for how they are handled.
_MODIFIERS: Dict[str, Tuple[str, Tuple[str, ...]]] = {
    "ShellFeature": ("shell", ("thickness",)),
    "HoleFeature": ("hole", ("radius", "depth", "center", "axis")),
    "PatternFeature": ("pattern", ("kind", "count", "spacing", "axis", "center")),
    "TransformFeature": ("transform", ("translate", "rotate", "scale")),
}

# Features that act on named topology. Their stored references carry the whole
# descriptor, but the operation takes ids — and since the body is rebuilt
# identically on replay, the ids it produces are the same ones.
_TOPOLOGY_OPS = {
    "EdgeFilletFeature": ("fillet_edges", "edges", "radius"),
    "EdgeChamferFeature": ("chamfer_edges", "edges", "distance"),
    "FaceShellFeature": ("shell_faces", "faces", "thickness"),
}

# Bodies whose geometry is carried as data rather than parameters, so no
# sequence of operations reconstructs them: a voxel grid for the first two, a
# serialized OCC payload for the third. The import operations that produce them
# take a *file*, which is not something a replay has.
_OPAQUE = {
    "ImportFeature": "a cached voxel grid",
    "OptimizeFeature": "a cached voxel grid",
    "BrepBodyFeature": "a serialized B-rep payload",
}

# Smooth booleans between bodies. With one child they are inert — a smooth union
# of a single body is that body, which is exactly what the evaluator's
# single-child collapse does — so they are stepped through rather than emitted.
# With two or more they blend, and nothing in the catalog builds that.
_BLENDS = {"FilletFeature", "ChamferFeature"}


def _blend_passthrough(node: dict) -> Optional[dict]:
    """The lone child of an inert blend, or None if it really does blend."""
    children = node_children(node)
    return children[0] if len(children) == 1 else None


class Unsupported(Exception):
    """This tree cannot be expressed as a sequence of catalog operations.

    ``feature`` names the type that could not be emitted, when one is to blame.
    Aggregated across a corpus that is the difference between "8% of our
    documents are unrecoverable" and "fix `OptimizeFeature` first" — see
    `corpus_report`.
    """

    def __init__(self, message: str, feature: Optional[str] = None):
        super().__init__(message)
        self.feature = feature


def _pick(params: Optional[dict], keys: Tuple[str, ...]) -> dict:
    """The subset of `params` an operation accepts, skipping absent keys.

    Passing only what is present lets each handler apply its own documented
    default, so a decompiled call looks like one a person would write.
    """
    p = params or {}
    return {k: p[k] for k in keys if p.get(k) is not None}


# A sketch is geometry other features consume, so it is emitted *before* the
# body — once, however many features read it — and they refer back to its step.
# Emitting one per consumer would rebuild the same outline twice and lose the
# sharing, and the replayed tree would not hash-match the original.
_SKETCH = "SketchFeature"

# A datum plane is a reference, not material. Like a sketch it lives in the tree
# and contributes no body, so it is emitted as its own step and filtered out of
# every "which children are bodies" question. Without this every document made
# from the blank template — which is three planes — decompiles as unsupported,
# and `corpus_report` reports zero for feature types that are perfectly fine.
_PLANE = "DatumPlaneFeature"
_BODYLESS = (_SKETCH, _PLANE)

# feature type -> {stored reference param: (operation argument, the profile
# param the sketch supplied)}. The profile is dropped when a sketch supplied it:
# passing both would be redundant now and would let the two drift apart on the
# next edit.
_SKETCH_ARGS: Dict[str, Dict[str, Tuple[str, str]]] = {
    "ExtrudeFeature": {"sketch_ref": ("sketch", "profile")},
    "RevolveFeature": {"sketch_ref": ("sketch", "profile")},
    "LoftFeature": {"sketch_ref_a": ("sketch_a", "profile_a"),
                    "sketch_ref_b": ("sketch_b", "profile_b")},
    "RuledLoftFeature": {"sketch_ref_a": ("sketch_a", "profile_a"),
                         "sketch_ref_b": ("sketch_b", "profile_b")},
}

_CREATE_SKETCH_KEYS = ("sketch", "plane", "offset", "on_face")


class _Emit:
    """Accumulator for one decompilation.

    Three things travel together and every emitter needs at least two of them:
    the steps so far, which step created each sketch, and which step created each
    *feature* — the last because a sketch placed on a face names the feature it
    sits on, and that name is a deterministic id which only holds if the op order
    is the same. Emission order is not the tree order, so the reference is turned
    into a `{"$step": i}` and resolved at replay.
    """

    def __init__(self) -> None:
        self.steps: List[dict] = []
        self.sketches: Dict[str, int] = {}
        self.made: Dict[str, int] = {}
        # Which default orientations have already been accounted for by the
        # blank template a replay starts from — see `_is_default_plane`.
        self.default_planes: set = set()

    def push(self, op: str, args: dict, node: Optional[dict] = None) -> int:
        self.steps.append({"op": op, "args": args})
        index = len(self.steps) - 1
        if node is not None and node.get("id"):
            self.made[node["id"]] = index
        return index


def _decompile_with_context(tree: Dict[str, Any]) -> Tuple[List[dict], _Emit]:
    root = (tree or {}).get("root")
    if not root:
        raise Unsupported("tree has no root")

    ctx = _Emit()
    _emit_body(root, ctx)
    return ctx.steps, ctx


def decompile(tree: Dict[str, Any]) -> List[dict]:
    """The operation sequence that builds `tree`.

    Each step is ``{"op", "args"}``. A step that must refer to a feature created
    earlier carries ``args["target"] = {"$step": i}``; :func:`replay` substitutes
    the real id, because the id a feature gets is only known once it is created.
    """
    return _decompile_with_context(tree)[0]


def _emit_sketch(node: dict, ctx: _Emit) -> int:
    """`create_sketch` for one sketch, once, at the point it is first needed.

    Emitted lazily rather than up front so it lands *after* whatever its
    `on_face` names: a sketch on the top of a plate cannot be created before the
    plate exists. Lazily also means once — a sketch two features read is one
    `create_sketch` and two references, which is both the truth and the only way
    the replayed tree hash-matches.
    """
    sketch_id = node.get("id")
    if sketch_id in ctx.sketches:
        return ctx.sketches[sketch_id]
    params = node.get("params") or {}
    if not params.get("sketch"):
        raise Unsupported("SketchFeature with no sketch spec to re-solve", _SKETCH)
    args = _pick(params, _CREATE_SKETCH_KEYS)
    if node.get("name"):
        args["name"] = node["name"]
    on_face = args.get("on_face")
    if isinstance(on_face, dict) and on_face.get("feature_id"):
        host = ctx.made.get(on_face["feature_id"])
        if host is None:
            raise Unsupported(
                f"sketch {sketch_id} is placed on {on_face['feature_id']}, which "
                "nothing in this sequence has created yet", _SKETCH)
        # Drop the stored descriptor: the body is rebuilt identically on replay,
        # so the id it produces is the same one, and re-passing a descriptor
        # measured against the *old* body would be the stale half of a pair.
        args["on_face"] = {k: v for k, v in on_face.items() if k != "face_ref"}
        args["on_face"]["feature_id"] = {"$step": host}
    return ctx.sketches.setdefault(sketch_id, ctx.push("create_sketch", args, node))


def _sketch_of(tree_node: dict, sketch_id: str) -> Optional[dict]:
    from app.cad_kernel.core.feature_tree import walk_nodes

    for node in walk_nodes(tree_node):
        if node.get("type") == _SKETCH and node.get("id") == sketch_id:
            return node
    return None


def _body_children(node: dict) -> List[dict]:
    """A boolean's children that are actually bodies. Sketches and planes are not."""
    return [c for c in node_children(node) if c.get("type") not in _BODYLESS]


def _is_default_plane(node: dict, ctx: _Emit) -> bool:
    """Whether this plane is one a blank document already has.

    Replay runs into a **new document**, and a new document opens with Top/Front/
    Right at the origin. Emitting `add_plane` for those would rebuild the source
    with six planes instead of three — and the verification would not catch it,
    because planes carry no geometry and the mesh hashes would match exactly. A
    demonstration that quietly doubles the datums teaches that.

    Matched on orientation and offset rather than on id, because an id is not
    stable across a rebuild, and once per orientation, because a second XY plane
    at the origin genuinely was added by hand.
    """
    from app.cad_kernel.ops.datum_plane import DEFAULT_PLANES

    params = node.get("params") or {}
    plane = params.get("plane", "xy")
    if plane not in {p for _, p in DEFAULT_PLANES}:
        return False
    if abs(float(params.get("offset", 0.0) or 0.0)) > 1e-12:
        return False
    if plane in ctx.default_planes:
        return False
    ctx.default_planes.add(plane)
    return True


def _emit_plane(node: dict, ctx: _Emit) -> Optional[int]:
    """`add_plane` for one datum plane, or None when the template supplies it."""
    if _is_default_plane(node, ctx):
        return None
    params = node.get("params") or {}
    args = _pick(params, ("plane", "offset", "size"))
    if node.get("name"):
        args["name"] = node["name"]
    return ctx.push("add_plane", args, node)


def _emit_body(node: dict, ctx: _Emit, root: Optional[dict] = None) -> int:
    """Emit ops building `node` as the whole body. Returns its step index."""
    root = root if root is not None else node
    node_type = node.get("type")

    if node_type == _SKETCH:
        # A document that is only a sketch has no body yet — a legitimate state
        # halfway through drawing one, and `resolve_sketches` gives it the same
        # placeholder an all-suppressed tree gets.
        return _emit_sketch(node, ctx)

    if node_type == _PLANE:
        # And a document that is only planes is what every new one starts as.
        return _emit_plane(node, ctx)

    if node_type in _OPAQUE:
        raise Unsupported(
            f"{node_type} carries {_OPAQUE[node_type]}, which no operation can rebuild "
            f"— it was imported or solved, not modelled", node_type)

    if node_type == "BooleanFeature":
        children = _body_children(node)
        bodyless = [c for c in node_children(node) if c.get("type") in _BODYLESS]
        if not children:
            # A union holding only planes and sketches is a document with no
            # body yet — which every new one is, so this is the common case
            # rather than a malformed tree. Rebuilding it is rebuilding them.
            if not bodyless:
                raise Unsupported("BooleanFeature with no children", "BooleanFeature")
            index = -1
            for child in bodyless:
                emitted = (_emit_sketch(child, ctx) if child.get("type") == _SKETCH
                           else _emit_plane(child, ctx))
                if emitted is not None:
                    index = emitted
            return index
        operation = (node.get("params") or {}).get("operation", "union")
        index = _emit_body(children[0], ctx, root)             # the base body
        for child in children[1:]:
            index = _emit_onto(child, operation, ctx, root)     # combined onto it
        # A sketch nobody reads, and every datum plane, still have to be
        # created, or the replayed tree is missing features the original has.
        for child in bodyless:
            if child.get("type") == _SKETCH:
                _emit_sketch(child, ctx)
            else:
                _emit_plane(child, ctx)
        return index

    if node_type == "MirrorFeature":
        children = _body_children(node)
        if not children:
            raise Unsupported("MirrorFeature with no children", "MirrorFeature")
        _emit_body(children[0], ctx, root)
        params = node.get("params") or {}
        return ctx.push("add_mirror", _pick(params, ("plane", "offset")), node)

    if node_type in _TOPOLOGY_OPS:
        children = _body_children(node)
        if not children:
            raise Unsupported(f"{node_type} with no children", node_type)
        _emit_body(children[0], ctx, root)
        op_name, args = _topology_args(node)
        return ctx.push(op_name, args, node)

    if node_type in _BLENDS:
        passthrough = _blend_passthrough(node)
        if passthrough is None:
            raise Unsupported(
                f"{node_type} blends two bodies; no operation builds that", node_type)
        return _emit_body(passthrough, ctx, root)

    if node_type in _MODIFIERS:
        children = _body_children(node)
        if not children:
            raise Unsupported(f"{node_type} with no children", node_type)
        _emit_body(children[0], ctx, root)
        # No target: wrapping the whole body, which is add_modifier's default.
        return ctx.push("add_modifier", _modifier_args(node), node)

    return _emit_leaf(node, "union", ctx, root)


def _emit_onto(node: dict, operation: str, ctx: _Emit, root: dict,
               target: Optional[dict] = None) -> int:
    """Emit ops adding `node` with `operation` — onto the body, or onto `target`.

    ``target`` is a ``{"$step": i}`` / ``{"$group": i}`` reference, resolved at
    replay time because the id a feature gets is only known once it is created.
    """
    node_type = node.get("type")

    if node_type in _OPAQUE:
        raise Unsupported(
            f"{node_type} carries {_OPAQUE[node_type]}, which no operation can rebuild "
            f"— it was imported or solved, not modelled", node_type)

    if node_type in _BLENDS:
        passthrough = _blend_passthrough(node)
        if passthrough is None:
            raise Unsupported(
                f"{node_type} blends two bodies; no operation builds that", node_type)
        return _emit_onto(passthrough, operation, ctx, root, target)

    if node_type in _MODIFIERS or node_type == "MirrorFeature":
        # A modifier sitting under a boolean was applied to that one feature, not
        # to the whole body — so it replays as a targeted add_modifier.
        children = _body_children(node)
        if not children:
            raise Unsupported(f"{node_type} with no children", node_type)
        inner = _emit_onto(children[0], operation, ctx, root, target)
        if node_type == "MirrorFeature":
            args = _pick(node.get("params") or {}, ("plane", "offset"))
            args["target"] = {"$step": inner}
            return ctx.push("add_mirror", args, node)
        args = _modifier_args(node)
        args["target"] = {"$step": inner}
        return ctx.push("add_modifier", args, node)

    if node_type == "BooleanFeature":
        # `union(plate, subtract(boss, bore))` — a hole through a boss that must
        # not cut the plate under it. The first child joins the body with the
        # *outer* operation; the rest are then combined with that child alone,
        # using the inner one. That needs `target`, which is why the builders
        # grew it: without it this whole shape was unrecoverable, and it is one
        # whole-tree generation produces routinely.
        children = _body_children(node)
        if not children:
            raise Unsupported("BooleanFeature with no children", "BooleanFeature")
        inner = (node.get("params") or {}).get("operation", "union")
        base = _emit_onto(children[0], operation, ctx, root, target)

        # The second child targets the first *feature*, which wraps the two in a
        # new boolean. Every child after that targets the boolean itself — hence
        # `$group` — so a third and fourth join that group instead of nesting a
        # fresh pair around the previous one. Same body either way; different
        # tree, and the tree is what gets compared.
        reference: dict = {"$step": base}
        for child in children[1:]:
            reference = {"$group": _emit_onto(child, inner, ctx, root, reference)}
        for child in node_children(node):
            if child.get("type") == _SKETCH:
                _emit_sketch(child, ctx)
        return base

    return _emit_leaf(node, operation, ctx, root, target)


def _emit_leaf(node: dict, operation: str, ctx: _Emit, root: dict,
               target: Optional[dict] = None) -> int:
    node_type = node.get("type")
    params = node.get("params") or {}

    if node_type in _PRIMITIVES:
        shape, keys = _PRIMITIVES[node_type]
        args: Dict[str, Any] = {"shape": shape, "params": _pick(params, keys)}
        if operation != "union":
            args["operation"] = operation
        if target is not None:
            args["target"] = target
        return ctx.push("add_primitive", args, node)

    if node_type in _BUILDERS:
        op_name, keys = _BUILDERS[node_type]
        args = _pick(params, keys)
        for ref_key, (arg_key, profile_key) in (_SKETCH_ARGS.get(node_type) or {}).items():
            sketch_id = params.get(ref_key)
            if sketch_id is None:
                continue
            sketch = _sketch_of(root, sketch_id)
            if sketch is None:
                raise Unsupported(
                    f"{node_type} reads sketch {sketch_id!r}, which is not in this tree",
                    node_type)
            args.pop(profile_key, None)
            args[arg_key] = {"$step": _emit_sketch(sketch, ctx)}
        if operation != "union":
            args["operation"] = operation
        if target is not None:
            args["target"] = target
        return ctx.push(op_name, args, node)

    raise Unsupported(f"no operation builds {node_type}", node_type)


def _topology_args(node: dict):
    """A fillet/chamfer/shell as the operation that would create it."""
    op_name, selection_key, size_key = _TOPOLOGY_OPS[node["type"]]
    params = node.get("params") or {}
    references = params.get(selection_key) or []
    ids = [r.get("id") for r in references if isinstance(r, dict) and r.get("id")]
    if not ids:
        raise Unsupported(f"{node['type']} has no resolvable selection to replay",
                          str(node["type"]))
    return op_name, {selection_key: ids, size_key: params.get(size_key)}


def _modifier_args(node: dict) -> dict:
    modifier, keys = _MODIFIERS[node["type"]]
    return {"modifier": modifier, "params": _pick(node.get("params") or {}, keys)}


# ------------------------------------------------------------------ replay --

def resolve_refs(args: dict, created: List[Optional[str]],
                 groups: Optional[List[Optional[str]]] = None) -> dict:
    """Replace ``{"$step": i}`` / ``{"$group": i}`` placeholders with real ids.

    ``$step`` is the feature step *i* created; ``$group`` is the boolean it
    created or joined. They are different nodes and the distinction matters: a
    third body joining an existing group has to target the group, while a
    modifier acting on one body has to target the body.
    """
    def substitute(value):
        if not isinstance(value, dict):
            return value
        if "$step" in value:
            resolved = created[value["$step"]]
            if resolved is None:
                raise Unsupported(f"step {value['$step']} created no feature to target")
            return resolved
        if "$group" in value:
            index = value["$group"]
            resolved = (groups or [])[index] if groups and index < len(groups) else None
            if resolved is None:
                raise Unsupported(f"step {index} created no group to target")
            return resolved
        # Nested: `on_face.feature_id` names a feature an earlier step created,
        # and a sketch is not always emitted after that step in tree order.
        return {k: substitute(v) for k, v in value.items()}

    return {key: substitute(value) for key, value in (args or {}).items()}


def replay(steps: List[dict], doc_id: str) -> List[dict]:
    """Run a decompiled sequence against a document. Returns the envelopes.

    Goes through ``cad_operations.execute``, so a replay inside a recording
    session produces a real trajectory — the ops are genuinely executed and the
    results genuinely observed, rather than a transcript being fabricated.
    """
    from app.services import cad_operations

    created: List[Optional[str]] = []
    groups: List[Optional[str]] = []
    envelopes: List[dict] = []
    for step in steps:
        args = resolve_refs(step["args"], created, groups)
        env = cad_operations.execute(step["op"], doc_id, args)
        envelopes.append(env)
        result = env.get("result") or {}
        created.append(result.get("feature_id"))
        groups.append(result.get("group_id"))
        if not env["ok"]:
            break
    return envelopes


# -------------------------------------------------------------- parameters --

def _shift_refs(value, by: int):
    """Offset every ``{"$step": i}`` / ``{"$group": i}`` in `value` by `by`.

    Needed because the parameter table has to be *prepended*: an expression is
    only meaningful once the name it uses exists, and every reference in the
    geometry steps counts from position zero.
    """
    if isinstance(value, dict):
        for tag in ("$step", "$group"):
            if tag in value:
                return {tag: value[tag] + by}
        return {k: _shift_refs(v, by) for k, v in value.items()}
    if isinstance(value, list):
        return [_shift_refs(v, by) for v in value]
    return value


def _expression_params(node: dict) -> dict:
    """The node's parameters that are expressions rather than numbers.

    Returned whole-key: a ``center`` with one bound component is re-set as a
    whole ``center``, because ``set_feature_params`` merges at the top level and
    a partial dict would drop the other two axes.
    """
    from app.services.parameters import _looks_expr

    def bound(value) -> bool:
        if isinstance(value, str):
            return value.startswith("=") or _looks_expr(value)
        if isinstance(value, dict):
            return any(bound(v) for v in value.values())
        if isinstance(value, list):
            return any(bound(v) for v in value)
        return False

    params = node.get("params") or {}
    return {k: v for k, v in params.items() if k != "children" and bound(v)}


def _parameter_steps(source_tree: dict, parameters: List[dict],
                     ctx: _Emit) -> Tuple[List[dict], List[dict]]:
    """The ``set_parameters`` prologue and the ``set_feature_params`` epilogue.

    A parameterised document could not be decompiled at all before this: the
    stored tree holds the *expression* (``"=length"``) — that is the whole point
    of a parameter — and the emitted ``add_primitive`` carried the string
    straight into ``params.size.x``, which the schema correctly refuses because
    a size is a number. So every document with design intent in it was recorded
    as ``replay_failed``, and templates make that the common case rather than a
    curiosity.

    The recovery is the sequence a person performs: declare the parameters,
    build with literals, then bind. Splitting it that way is not a workaround —
    ``add_primitive``'s schema is what a constrained decoder reads, and an
    expression string has no business in a numeric field there.
    """
    from app.cad_kernel.core.feature_tree import walk_nodes

    if not parameters:
        return [], []
    prologue = [{"op": "set_parameters", "args": {"parameters": [
        {"name": p["name"], "expr": str(p.get("expr", "0")),
         "unit": p.get("unit", "mm")} for p in parameters]}}]

    epilogue: List[dict] = []
    for node in walk_nodes(source_tree):
        step = ctx.made.get(node.get("id"))
        if step is None:
            continue
        bound = _expression_params(node)
        if bound:
            # +1 for the prologue, which lands in front of every geometry step.
            epilogue.append({"op": "set_feature_params",
                             "args": {"feature_id": {"$step": step + 1},
                                      "params": bound}})
    return prologue, epilogue


# ------------------------------------------------------------ verification --

def _hash_tree_geometry(tree: dict, resolution: int,
                        scope: Optional[Dict[str, float]] = None) -> str:
    from app.services import feature_ops
    from app.services import parameters as params

    # Parameters first, then sketches: an expression string is not a number any
    # evaluator can take, and a SketchFeature has no body and never reaches one
    # in the running system either.
    if scope:
        tree = params.resolve_tree(tree, scope)
    solid = Kernel.evaluate_tree(
        FeatureTree.from_dict(feature_ops.resolve_sketches(tree)), resolution=resolution)
    return mesh_hash(Kernel.tessellate(solid))


def _scope_of(doc) -> Dict[str, float]:
    from app.services import parameters as params

    if not getattr(doc, "parameters", None):
        return {}
    scope, _ = params.resolve_values(doc.parameters)
    return scope


def decompile_document(
    doc_id: str,
    *,
    resolution: int = 48,
    goal: str = "",
    record: bool = True,
) -> dict:
    """Decompile a document, replay it, and verify the geometry matches.

    On success with ``record=True`` the replay is captured as a trajectory whose
    goal is the document's own prompt, giving a (prompt -> operation sequence)
    training example for a document that was never built through the API.

    The replay happens in a scratch document that is deleted afterwards, so
    decompiling never touches the original.
    """
    from app.services import document_service as docs
    from app.services import trajectories

    source = docs.get_document(doc_id)
    if source is None:
        return {"ok": False, "reason": f"no document {doc_id!r}",
                "cause": "no_such_document", "steps": []}

    try:
        # The geometry is decompiled from the *resolved* tree, so every emitted
        # argument is the number the schema wants; the expressions come back
        # afterwards as bindings over the parameter table.
        scope = _scope_of(source)
        from app.services import parameters as _params

        build_tree = _params.resolve_tree(source.feature_tree, scope) if scope \
            else source.feature_tree
        steps, ctx = _decompile_with_context(build_tree)
        prologue, epilogue = _parameter_steps(
            source.feature_tree, [p.model_dump() for p in source.parameters], ctx)
        if prologue:
            steps = prologue + [{"op": s["op"], "args": _shift_refs(s["args"], 1)}
                                for s in steps] + epilogue
    except Unsupported as exc:
        return {"ok": False, "reason": str(exc), "unsupported": True,
                "cause": "unsupported", "failed_at": exc.feature, "steps": []}
    if not steps:
        return {"ok": False, "reason": "tree decompiled to no operations",
                "cause": "empty", "steps": []}

    scratch = docs.create_document(name=f"decompile:{source.name}"[:60],
                                   resolution=resolution, owner_id=source.owner_id)
    trajectory_id = None
    try:
        if record:
            trajectory_id = trajectories.start(
                doc_id, goal=goal or source.source_prompt or source.name,
                kind="decompiled", actor="system", owner_id=source.owner_id)
        try:
            envelopes = replay(steps, scratch.id)
        except Unsupported as exc:
            # `replay` raises this too, from `resolve_refs`, when a step targets
            # a group an earlier step did not create. Only the `decompile` call
            # above was guarded, so one document with an unresolvable reference
            # took down the whole of `corpus_report` — the metric that is
            # supposed to *count* those. A document that cannot be replayed is a
            # result, not a crash.
            return _reject(trajectory_id, record, steps, str(exc),
                           cause="unsupported", failed_at=exc.feature)
        if record:
            trajectories.finish(trajectory_id, outcome="done",
                                summary=f"rebuilt {source.name} in {len(steps)} operations")

        failed = next((e for e in envelopes if not e["ok"]), None)
        if failed is not None:
            return _reject(trajectory_id, record, steps,
                           f"replay failed at {failed['op']}: {failed['error']['message']}",
                           cause="replay_failed", failed_at=failed["op"])

        rebuilt = docs.get_document(scratch.id)
        # Each side is resolved against its *own* parameter table: the point of
        # the comparison is that the replay produced the same body, and it only
        # holds if the replayed document carries the parameters too.
        #
        # Broad catch, deliberately. A tree that does not evaluate is a
        # *supported* state of a document — `evaluate_document` records it in
        # `derived.error` and carries on, so the store legitimately contains
        # some — and the evaluator's failure modes are open-ended (a childless
        # `TransformFeature` raises `IndexError` from deep inside the SDF
        # builder). This is the measurement path: a document that cannot be
        # evaluated is a number in the report, not a traceback that takes the
        # whole report down with it.
        try:
            want = _hash_tree_geometry(source.feature_tree, resolution, scope)
            got = _hash_tree_geometry(rebuilt.feature_tree, resolution, _scope_of(rebuilt))
        except Exception as exc:
            return _reject(trajectory_id, record, steps,
                           f"the tree does not evaluate: {type(exc).__name__}: {exc}",
                           cause="eval_failed")
        if want != got:
            return _reject(trajectory_id, record, steps,
                           "replayed geometry differs from the original",
                           cause="geometry_mismatch")

        return {"ok": True, "steps": steps, "n_steps": len(steps),
                "trajectory_id": trajectory_id, "mesh_hash": want}
    finally:
        docs.delete_document(scratch.id)


def _reject(trajectory_id: Optional[str], record: bool, steps: List[dict], reason: str,
            *, cause: str = "rejected", failed_at: Optional[str] = None) -> dict:
    """Discard a trajectory that did not reproduce its source.

    A demonstration that does not rebuild the part teaches the wrong thing, so it
    is deleted rather than kept with a caveat nobody will filter on.
    """
    from app.services import trajectories

    if record and trajectory_id:
        trajectories.delete(trajectory_id)
    _LOGGER.info("decompile rejected: %s", reason)
    return {"ok": False, "reason": reason, "cause": cause, "failed_at": failed_at,
            "steps": steps, "n_steps": len(steps)}


def decompile_all(*, resolution: int = 48, limit: Optional[int] = None,
                  record: bool = True) -> dict:
    """Decompile every stored document. Returns per-document outcomes.

    The bulk path for turning an existing corpus into trajectories. The summary
    counts are `corpus_report`'s job — this stays the raw list so a caller that
    only wants the trajectories does not pay for the analysis.
    """
    from app.services import document_service as docs

    results = []
    for summary in docs.list_documents()[:limit]:
        outcome = decompile_document(summary.id, resolution=resolution, record=record)
        results.append({"doc_id": summary.id, "name": summary.name,
                        "ok": outcome["ok"], "reason": outcome.get("reason"),
                        "failed_at": outcome.get("failed_at"),
                        "cause": outcome.get("cause"),
                        "n_steps": outcome.get("n_steps", 0),
                        "trajectory_id": outcome.get("trajectory_id")})
    return {
        "total": len(results),
        "recovered": sum(1 for r in results if r["ok"]),
        "results": results,
    }


# ------------------------------------------------------------- corpus health --

def _feature_types(tree: Dict[str, Any]) -> List[str]:
    """Every feature type in a tree, deduplicated."""
    from app.cad_kernel.core.feature_tree import walk_nodes

    return sorted({str(n.get("type")) for n in walk_nodes(tree or {}) if n.get("type")})


def corpus_report(*, resolution: int = 48, limit: Optional[int] = None,
                  doc_ids: Optional[List[str]] = None, record: bool = False) -> dict:
    """The replay-verification hit rate over the whole corpus, and what fails.

    This is the health metric for the training-data half of the project and the
    one number nobody had looked at: *what fraction of the documents we already
    have can be turned into a verified demonstration?* Everything else here
    exists to make that number actionable rather than merely alarming —
    ``by_cause`` says whether the losses are unrecoverable geometry or a gap in
    the emitter, and ``by_feature`` says which feature type to fix first.

    ``by_feature`` counts a *document* against every type it contains, so a type
    with a 0% rate is either genuinely undecompilable or always keeps company
    with something that is. The ``blocking`` figure separates those: it counts
    only the documents where that type is the type the decompiler actually named
    as the reason.

    ``record`` is off here by default — the point of this call is to measure, and
    writing a few hundred trajectories as a side effect of asking a question is
    surprising. `decompile_all` is the one that exists to produce them.

    ``doc_ids`` narrows the population, for measuring one project rather than
    the whole datastore.
    """
    from app.services import document_service as docs

    summaries = [s for s in docs.list_documents()
                 if doc_ids is None or s.id in set(doc_ids)][:limit]
    rows: List[dict] = []
    by_cause: Dict[str, int] = {}
    by_feature: Dict[str, Dict[str, int]] = {}

    for summary in summaries:
        document = docs.get_document(summary.id)
        if document is None:
            continue
        types = _feature_types(document.feature_tree)
        outcome = decompile_document(summary.id, resolution=resolution, record=record)
        cause = "recovered" if outcome["ok"] else (outcome.get("cause") or "unknown")
        by_cause[cause] = by_cause.get(cause, 0) + 1

        blamed = outcome.get("failed_at")
        for feature_type in types:
            stats = by_feature.setdefault(feature_type, {"documents": 0, "recovered": 0,
                                                         "blocking": 0})
            stats["documents"] += 1
            stats["recovered"] += 1 if outcome["ok"] else 0
            if not outcome["ok"] and blamed == feature_type:
                stats["blocking"] += 1

        rows.append({"doc_id": summary.id, "name": summary.name, "ok": outcome["ok"],
                     "cause": cause, "reason": outcome.get("reason"),
                     "failed_at": blamed, "n_steps": outcome.get("n_steps", 0),
                     "features": types})

    total = len(rows)
    recovered = sum(1 for r in rows if r["ok"])
    features = [{"feature": name, **stats,
                 "rate": round(stats["recovered"] / stats["documents"], 4)}
                for name, stats in by_feature.items()]
    # Worst first, and within that the ones that block most often — the order you
    # would work down if you were fixing the emitter.
    features.sort(key=lambda f: (f["rate"], -f["blocking"], f["feature"]))

    return {
        "documents": total,
        "recovered": recovered,
        "hit_rate": round(recovered / total, 4) if total else 0.0,
        "by_cause": dict(sorted(by_cause.items(), key=lambda kv: -kv[1])),
        "by_feature": features,
        "results": rows,
    }

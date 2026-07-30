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

# Bodies whose geometry lives in a cached voxel grid, not in parameters.
_OPAQUE = {"ImportFeature", "OptimizeFeature"}

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
    """This tree cannot be expressed as a sequence of catalog operations."""


def _pick(params: Optional[dict], keys: Tuple[str, ...]) -> dict:
    """The subset of `params` an operation accepts, skipping absent keys.

    Passing only what is present lets each handler apply its own documented
    default, so a decompiled call looks like one a person would write.
    """
    p = params or {}
    return {k: p[k] for k in keys if p.get(k) is not None}


def decompile(tree: Dict[str, Any]) -> List[dict]:
    """The operation sequence that builds `tree`.

    Each step is ``{"op", "args"}``. A step that must refer to a feature created
    earlier carries ``args["target"] = {"$step": i}``; :func:`replay` substitutes
    the real id, because the id a feature gets is only known once it is created.
    """
    root = (tree or {}).get("root")
    if not root:
        raise Unsupported("tree has no root")

    steps: List[dict] = []
    _emit_body(root, steps)
    return steps


def _emit_body(node: dict, steps: List[dict]) -> int:
    """Emit ops building `node` as the whole body. Returns its step index."""
    node_type = node.get("type")

    if node_type in _OPAQUE:
        raise Unsupported(f"{node_type} holds a cached voxel grid no operation can rebuild")

    if node_type == "BooleanFeature":
        children = node_children(node)
        if not children:
            raise Unsupported("BooleanFeature with no children")
        operation = (node.get("params") or {}).get("operation", "union")
        index = _emit_body(children[0], steps)          # the base body
        for child in children[1:]:
            index = _emit_onto(child, operation, steps)  # combined onto it
        return index

    if node_type == "MirrorFeature":
        children = node_children(node)
        if not children:
            raise Unsupported("MirrorFeature with no children")
        _emit_body(children[0], steps)
        params = node.get("params") or {}
        return _push(steps, "add_mirror", _pick(params, ("plane", "offset")))

    if node_type in _TOPOLOGY_OPS:
        children = node_children(node)
        if not children:
            raise Unsupported(f"{node_type} with no children")
        _emit_body(children[0], steps)
        op_name, args = _topology_args(node)
        return _push(steps, op_name, args)

    if node_type in _BLENDS:
        passthrough = _blend_passthrough(node)
        if passthrough is None:
            raise Unsupported(
                f"{node_type} blends two bodies; no operation builds that")
        return _emit_body(passthrough, steps)

    if node_type in _MODIFIERS:
        children = node_children(node)
        if not children:
            raise Unsupported(f"{node_type} with no children")
        _emit_body(children[0], steps)
        # No target: wrapping the whole body, which is add_modifier's default.
        return _push(steps, "add_modifier", _modifier_args(node))

    return _emit_leaf(node, "union", steps)


def _emit_onto(node: dict, operation: str, steps: List[dict]) -> int:
    """Emit ops adding `node` onto the existing body with `operation`."""
    node_type = node.get("type")

    if node_type in _OPAQUE:
        raise Unsupported(f"{node_type} holds a cached voxel grid no operation can rebuild")

    if node_type in _BLENDS:
        passthrough = _blend_passthrough(node)
        if passthrough is None:
            raise Unsupported(
                f"{node_type} blends two bodies; no operation builds that")
        return _emit_onto(passthrough, operation, steps)

    if node_type in _MODIFIERS or node_type == "MirrorFeature":
        # A modifier sitting under a boolean was applied to that one feature, not
        # to the whole body — so it replays as a targeted add_modifier.
        children = node_children(node)
        if not children:
            raise Unsupported(f"{node_type} with no children")
        inner = _emit_onto(children[0], operation, steps)
        if node_type == "MirrorFeature":
            args = _pick(node.get("params") or {}, ("plane", "offset"))
            args["target"] = {"$step": inner}
            return _push(steps, "add_mirror", args)
        args = _modifier_args(node)
        args["target"] = {"$step": inner}
        return _push(steps, "add_modifier", args)

    if node_type == "BooleanFeature":
        # add_primitive appends one feature at a time, so a whole nested boolean
        # cannot be attached in a single call. Trees built through the operation
        # API never take this shape; hand-authored and LLM trees occasionally do.
        raise Unsupported("a nested BooleanFeature cannot be added in one operation")

    return _emit_leaf(node, operation, steps)


def _emit_leaf(node: dict, operation: str, steps: List[dict]) -> int:
    node_type = node.get("type")
    params = node.get("params") or {}

    if node_type in _PRIMITIVES:
        shape, keys = _PRIMITIVES[node_type]
        args: Dict[str, Any] = {"shape": shape, "params": _pick(params, keys)}
        if operation != "union":
            args["operation"] = operation
        return _push(steps, "add_primitive", args)

    if node_type in _BUILDERS:
        op_name, keys = _BUILDERS[node_type]
        args = _pick(params, keys)
        if operation != "union":
            args["operation"] = operation
        return _push(steps, op_name, args)

    raise Unsupported(f"no operation builds {node_type}")


def _topology_args(node: dict):
    """A fillet/chamfer/shell as the operation that would create it."""
    op_name, selection_key, size_key = _TOPOLOGY_OPS[node["type"]]
    params = node.get("params") or {}
    references = params.get(selection_key) or []
    ids = [r.get("id") for r in references if isinstance(r, dict) and r.get("id")]
    if not ids:
        raise Unsupported(f"{node['type']} has no resolvable selection to replay")
    return op_name, {selection_key: ids, size_key: params.get(size_key)}


def _modifier_args(node: dict) -> dict:
    modifier, keys = _MODIFIERS[node["type"]]
    return {"modifier": modifier, "params": _pick(node.get("params") or {}, keys)}


def _push(steps: List[dict], op: str, args: dict) -> int:
    steps.append({"op": op, "args": args})
    return len(steps) - 1


# ------------------------------------------------------------------ replay --

def resolve_refs(args: dict, created: List[Optional[str]]) -> dict:
    """Replace ``{"$step": i}`` placeholders with the id step *i* actually created."""
    out = {}
    for key, value in (args or {}).items():
        if isinstance(value, dict) and "$step" in value:
            resolved = created[value["$step"]]
            if resolved is None:
                raise Unsupported(f"step {value['$step']} created no feature to target")
            out[key] = resolved
        else:
            out[key] = value
    return out


def replay(steps: List[dict], doc_id: str) -> List[dict]:
    """Run a decompiled sequence against a document. Returns the envelopes.

    Goes through ``cad_operations.execute``, so a replay inside a recording
    session produces a real trajectory — the ops are genuinely executed and the
    results genuinely observed, rather than a transcript being fabricated.
    """
    from app.services import cad_operations

    created: List[Optional[str]] = []
    envelopes: List[dict] = []
    for step in steps:
        args = resolve_refs(step["args"], created)
        env = cad_operations.execute(step["op"], doc_id, args)
        envelopes.append(env)
        created.append((env.get("result") or {}).get("feature_id"))
        if not env["ok"]:
            break
    return envelopes


# ------------------------------------------------------------ verification --

def _hash_tree_geometry(tree: dict, resolution: int) -> str:
    solid = Kernel.evaluate_tree(FeatureTree.from_dict(tree), resolution=resolution)
    return mesh_hash(Kernel.tessellate(solid))


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
        return {"ok": False, "reason": f"no document {doc_id!r}", "steps": []}

    try:
        steps = decompile(source.feature_tree)
    except Unsupported as exc:
        return {"ok": False, "reason": str(exc), "unsupported": True, "steps": []}
    if not steps:
        return {"ok": False, "reason": "tree decompiled to no operations", "steps": []}

    scratch = docs.create_document(name=f"decompile:{source.name}"[:60],
                                   resolution=resolution, owner_id=source.owner_id)
    trajectory_id = None
    try:
        if record:
            trajectory_id = trajectories.start(
                doc_id, goal=goal or source.source_prompt or source.name,
                kind="decompiled", actor="system", owner_id=source.owner_id)
        envelopes = replay(steps, scratch.id)
        if record:
            trajectories.finish(trajectory_id, outcome="done",
                                summary=f"rebuilt {source.name} in {len(steps)} operations")

        failed = next((e for e in envelopes if not e["ok"]), None)
        if failed is not None:
            return _reject(trajectory_id, record, steps,
                           f"replay failed at {failed['op']}: {failed['error']['message']}")

        want = _hash_tree_geometry(source.feature_tree, resolution)
        got = _hash_tree_geometry(docs.get_document(scratch.id).feature_tree, resolution)
        if want != got:
            return _reject(trajectory_id, record, steps,
                           "replayed geometry differs from the original")

        return {"ok": True, "steps": steps, "n_steps": len(steps),
                "trajectory_id": trajectory_id, "mesh_hash": want}
    finally:
        docs.delete_document(scratch.id)


def _reject(trajectory_id: Optional[str], record: bool, steps: List[dict], reason: str) -> dict:
    """Discard a trajectory that did not reproduce its source.

    A demonstration that does not rebuild the part teaches the wrong thing, so it
    is deleted rather than kept with a caveat nobody will filter on.
    """
    from app.services import trajectories

    if record and trajectory_id:
        trajectories.delete(trajectory_id)
    _LOGGER.info("decompile rejected: %s", reason)
    return {"ok": False, "reason": reason, "steps": steps, "n_steps": len(steps)}


def decompile_all(*, resolution: int = 48, limit: Optional[int] = None) -> dict:
    """Decompile every stored document. Returns per-document outcomes.

    The bulk path for turning an existing corpus into trajectories.
    """
    from app.services import document_service as docs

    results = []
    for summary in docs.list_documents()[:limit]:
        outcome = decompile_document(summary.id, resolution=resolution)
        results.append({"doc_id": summary.id, "name": summary.name,
                        "ok": outcome["ok"], "reason": outcome.get("reason"),
                        "n_steps": outcome.get("n_steps", 0),
                        "trajectory_id": outcome.get("trajectory_id")})
    return {
        "total": len(results),
        "recovered": sum(1 for r in results if r["ok"]),
        "results": results,
    }

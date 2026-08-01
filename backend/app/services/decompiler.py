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
    sketches = _emit_sketches(root, steps)
    _emit_body(root, steps, sketches)
    return steps


def _emit_sketches(root: dict, steps: List[dict]) -> Dict[str, int]:
    """Every sketch in the tree, created up front. Returns ``{sketch id: step}``."""
    from app.cad_kernel.core.feature_tree import walk_nodes

    out: Dict[str, int] = {}
    for node in walk_nodes(root):
        if node.get("type") != _SKETCH or not node.get("id"):
            continue
        params = node.get("params") or {}
        if not params.get("sketch"):
            raise Unsupported("SketchFeature with no sketch spec to re-solve", _SKETCH)
        args = _pick(params, _CREATE_SKETCH_KEYS)
        if node.get("name"):
            args["name"] = node["name"]
        out[node["id"]] = _push(steps, "create_sketch", args)
    return out


def _body_children(node: dict) -> List[dict]:
    """A boolean's children that are actually bodies. Sketches are not."""
    return [c for c in node_children(node) if c.get("type") != _SKETCH]


def _emit_body(node: dict, steps: List[dict], sketches: Dict[str, int]) -> int:
    """Emit ops building `node` as the whole body. Returns its step index."""
    node_type = node.get("type")

    if node_type == _SKETCH:
        # A document that is only a sketch has no body yet — a legitimate state
        # halfway through drawing one, and `resolve_sketches` gives it the same
        # placeholder an all-suppressed tree gets.
        return sketches[node["id"]]

    if node_type in _OPAQUE:
        raise Unsupported(
            f"{node_type} carries {_OPAQUE[node_type]}, which no operation can rebuild "
            f"— it was imported or solved, not modelled", node_type)

    if node_type == "BooleanFeature":
        children = _body_children(node)
        if not children:
            raise Unsupported("BooleanFeature with no children", "BooleanFeature")
        operation = (node.get("params") or {}).get("operation", "union")
        index = _emit_body(children[0], steps, sketches)          # the base body
        for child in children[1:]:
            index = _emit_onto(child, operation, steps, sketches)  # combined onto it
        return index

    if node_type == "MirrorFeature":
        children = _body_children(node)
        if not children:
            raise Unsupported("MirrorFeature with no children", "MirrorFeature")
        _emit_body(children[0], steps, sketches)
        params = node.get("params") or {}
        return _push(steps, "add_mirror", _pick(params, ("plane", "offset")))

    if node_type in _TOPOLOGY_OPS:
        children = _body_children(node)
        if not children:
            raise Unsupported(f"{node_type} with no children", node_type)
        _emit_body(children[0], steps, sketches)
        op_name, args = _topology_args(node)
        return _push(steps, op_name, args)

    if node_type in _BLENDS:
        passthrough = _blend_passthrough(node)
        if passthrough is None:
            raise Unsupported(
                f"{node_type} blends two bodies; no operation builds that", node_type)
        return _emit_body(passthrough, steps, sketches)

    if node_type in _MODIFIERS:
        children = _body_children(node)
        if not children:
            raise Unsupported(f"{node_type} with no children", node_type)
        _emit_body(children[0], steps, sketches)
        # No target: wrapping the whole body, which is add_modifier's default.
        return _push(steps, "add_modifier", _modifier_args(node))

    return _emit_leaf(node, "union", steps, sketches)


def _emit_onto(node: dict, operation: str, steps: List[dict],
               sketches: Dict[str, int], target: Optional[dict] = None) -> int:
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
        return _emit_onto(passthrough, operation, steps, sketches, target)

    if node_type in _MODIFIERS or node_type == "MirrorFeature":
        # A modifier sitting under a boolean was applied to that one feature, not
        # to the whole body — so it replays as a targeted add_modifier.
        children = _body_children(node)
        if not children:
            raise Unsupported(f"{node_type} with no children", node_type)
        inner = _emit_onto(children[0], operation, steps, sketches, target)
        if node_type == "MirrorFeature":
            args = _pick(node.get("params") or {}, ("plane", "offset"))
            args["target"] = {"$step": inner}
            return _push(steps, "add_mirror", args)
        args = _modifier_args(node)
        args["target"] = {"$step": inner}
        return _push(steps, "add_modifier", args)

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
        base = _emit_onto(children[0], operation, steps, sketches, target)

        # The second child targets the first *feature*, which wraps the two in a
        # new boolean. Every child after that targets the boolean itself — hence
        # `$group` — so a third and fourth join that group instead of nesting a
        # fresh pair around the previous one. Same body either way; different
        # tree, and the tree is what gets compared.
        reference: dict = {"$step": base}
        for child in children[1:]:
            reference = {"$group": _emit_onto(child, inner, steps, sketches, reference)}
        return base

    return _emit_leaf(node, operation, steps, sketches, target)


def _emit_leaf(node: dict, operation: str, steps: List[dict],
               sketches: Dict[str, int], target: Optional[dict] = None) -> int:
    node_type = node.get("type")
    params = node.get("params") or {}

    if node_type in _PRIMITIVES:
        shape, keys = _PRIMITIVES[node_type]
        args: Dict[str, Any] = {"shape": shape, "params": _pick(params, keys)}
        if operation != "union":
            args["operation"] = operation
        if target is not None:
            args["target"] = target
        return _push(steps, "add_primitive", args)

    if node_type in _BUILDERS:
        op_name, keys = _BUILDERS[node_type]
        args = _pick(params, keys)
        for ref_key, (arg_key, profile_key) in (_SKETCH_ARGS.get(node_type) or {}).items():
            sketch_id = params.get(ref_key)
            if sketch_id is None:
                continue
            if sketch_id not in sketches:
                raise Unsupported(
                    f"{node_type} reads sketch {sketch_id!r}, which is not in this tree",
                    node_type)
            args.pop(profile_key, None)
            args[arg_key] = {"$step": sketches[sketch_id]}
        if operation != "union":
            args["operation"] = operation
        if target is not None:
            args["target"] = target
        return _push(steps, op_name, args)

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


def _push(steps: List[dict], op: str, args: dict) -> int:
    steps.append({"op": op, "args": args})
    return len(steps) - 1


# ------------------------------------------------------------------ replay --

def resolve_refs(args: dict, created: List[Optional[str]],
                 groups: Optional[List[Optional[str]]] = None) -> dict:
    """Replace ``{"$step": i}`` / ``{"$group": i}`` placeholders with real ids.

    ``$step`` is the feature step *i* created; ``$group`` is the boolean it
    created or joined. They are different nodes and the distinction matters: a
    third body joining an existing group has to target the group, while a
    modifier acting on one body has to target the body.
    """
    out = {}
    for key, value in (args or {}).items():
        if isinstance(value, dict) and "$step" in value:
            resolved = created[value["$step"]]
            if resolved is None:
                raise Unsupported(f"step {value['$step']} created no feature to target")
            out[key] = resolved
        elif isinstance(value, dict) and "$group" in value:
            index = value["$group"]
            resolved = (groups or [])[index] if groups and index < len(groups) else None
            if resolved is None:
                raise Unsupported(f"step {index} created no group to target")
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


# ------------------------------------------------------------ verification --

def _hash_tree_geometry(tree: dict, resolution: int) -> str:
    from app.services import feature_ops

    # Sketches are substituted first: a SketchFeature has no body and never
    # reaches an evaluator in the running system either.
    solid = Kernel.evaluate_tree(
        FeatureTree.from_dict(feature_ops.resolve_sketches(tree)), resolution=resolution)
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
        return {"ok": False, "reason": f"no document {doc_id!r}",
                "cause": "no_such_document", "steps": []}

    try:
        steps = decompile(source.feature_tree)
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
        envelopes = replay(steps, scratch.id)
        if record:
            trajectories.finish(trajectory_id, outcome="done",
                                summary=f"rebuilt {source.name} in {len(steps)} operations")

        failed = next((e for e in envelopes if not e["ok"]), None)
        if failed is not None:
            return _reject(trajectory_id, record, steps,
                           f"replay failed at {failed['op']}: {failed['error']['message']}",
                           cause="replay_failed", failed_at=failed["op"])

        want = _hash_tree_geometry(source.feature_tree, resolution)
        got = _hash_tree_geometry(docs.get_document(scratch.id).feature_tree, resolution)
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

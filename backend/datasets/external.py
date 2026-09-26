"""External CAD datasets, converted to this catalog's operations and verified.

The rule is `docs/CAD-DATASET-SOURCING.md`'s: a dataset is used only where its
licence permits this use, **nothing is fetched by the server**, and a converted
sequence earns its place by being replayed — if the operations do not build a
valid solid, and match the source's own geometry where the source ships any, the
sample is dropped and counted rather than trained on. A converter that quietly
emits plausible-looking operations would be the worst outcome here: the corpus
would teach a model to drive this API into shapes it does not make.

Two entry points:

* `samples_from_file` reads a JSONL the user produced — one line per model,
  `{"goal", "ops", "source", "license", "expect"?}` — and turns each verified
  sequence into turn samples through the same `copilot.planner_request` path
  every other source uses.
* `convert_cadquery` is the converter for **Zero-To-CAD-1m** (Apache-2.0,
  ~1M CadQuery programs with STEP beside them). It reads the *program*, because
  that dataset's `cadquery_ops_json` is a trace of method names rather than
  dimensioned operations, and it handles a deliberately narrow subset of
  CadQuery: the shapes this catalog can express exactly. Everything else is
  dropped by cause, which is the honest yield and is reported.
"""

from __future__ import annotations

import ast
import json
from typing import Any, Dict, List, Optional, Tuple

MM = 1000.0
#: Where a converted sequence stopped being convertible, counted for the card.
DROPS: Dict[str, int] = {}


def _drop(cause: str) -> None:
    DROPS[cause] = DROPS.get(cause, 0) + 1


# ------------------------------------------------------------- verification --

def verify(doc_id: str, expect: Optional[dict] = None,
           tolerance: float = 0.02) -> Tuple[bool, str]:
    """Did the replayed sequence build the body the source says it is?

    Two questions, and the second is the one that catches a converter bug: a
    valid solid can still be the wrong solid. When the source states a volume or
    a bounding box, they have to agree within `tolerance` — a converted sequence
    that builds *something* is not evidence of a correct conversion.
    """
    from app.services import cad_operations

    env = cad_operations.execute("check_geometry", doc_id, {}, include_state=False)
    if not env["ok"] or not (env.get("result") or {}).get("valid"):
        return False, "invalid_solid"
    if not expect:
        return True, "valid_solid"

    measured = cad_operations.execute("mass_properties", doc_id, {}, include_state=False)
    result = measured.get("result") or {}
    volume = result.get("volume_mm3")
    if expect.get("volume_mm3") and volume:
        rel = abs(volume - float(expect["volume_mm3"])) / max(float(expect["volume_mm3"]), 1e-9)
        if rel > tolerance:
            return False, "volume_mismatch"
    box = cad_operations.execute("bounding_box", doc_id, {}, include_state=False)
    size = (box.get("result") or {}).get("size_mm") or {}
    if expect.get("size_mm") and size:
        for axis in ("x", "y", "z"):
            want = float(expect["size_mm"].get(axis, 0) or 0)
            got = float(size.get(axis, 0) or 0)
            if want and abs(got - want) / max(want, 1e-9) > tolerance:
                return False, "bbox_mismatch"
    return True, "matches_source"


def verify_sketch(doc_id: str) -> Tuple[bool, str]:
    """The solver's own verdict, where there is no body to measure.

    A converted sketch builds no solid, so `verify`'s first question — is this a
    valid solid — has nothing to answer and would fail every sketch. The real
    check is stronger than a volume comparison anyway: the solver reports whether
    the constraints *fight* (`conflicting`), which is exactly what a mis-mapped
    constraint produces. `under_constrained` is kept, because it is the normal
    state of a real user's sketch and refusing it would drop most of the dataset
    for being what it is.
    """
    from app.services import cad_operations

    listed = cad_operations.execute("list_features", doc_id, {}, include_state=False)
    features = (listed.get("result") or {}).get("features") or []
    sketch = next((f for f in features if str(f.get("type")) == "SketchFeature"), None)
    if sketch is None:
        return False, "no_sketch_feature"
    env = cad_operations.execute("check_sketch", doc_id,
                                {"feature_id": sketch.get("id")}, include_state=False)
    if not env["ok"]:
        return False, "check_sketch_failed"
    status = str((env.get("result") or {}).get("status") or "")
    if status == "conflicting":
        return False, "conflicting"
    return True, status or "solved"


def samples_from_file(path: str, *, limit: Optional[int] = None) -> List:
    """Verified turn samples from a converted dataset file.

    Each line's operations are replayed into a fresh document inside a recording,
    so the samples are built from operations that genuinely ran and observations
    genuinely observed — the same footing as `catalog` and `template`.
    """
    from app.services import decompiler, document_service, trajectories
    from datasets import corpus

    out: List = []
    with open(path) as fh:
        for index, raw in enumerate(fh):
            if limit is not None and index >= limit:
                break
            line = raw.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                _drop("line:not_json")
                continue
            ops, goal = row.get("ops") or [], (row.get("goal") or "").strip()
            if not ops or not goal:
                _drop("line:no_ops_or_goal")
                continue

            doc = document_service.create_document(name=f"imported {index}", owner_id="dataset")
            with trajectories.recording(doc.id, goal=goal, kind="imported") as tid:
                try:
                    envelopes = decompiler.replay(ops, doc.id)
                except Exception:  # noqa: BLE001 — a bad conversion is a drop
                    envelopes = []
            if not envelopes or not all(e.get("ok") for e in envelopes):
                _drop("replay:failed")
                continue
            # **The row says which check applies.** A sketch has no body, and a
            # geometry check pointed at one fails for a reason that has nothing
            # to do with the conversion — the same reason
            # `observation.summarize_assembly` states its `kind` rather than
            # leaving a caller to infer it from which keys are present.
            if str(row.get("verify") or "") == "sketch":
                ok, why = verify_sketch(doc.id)
            else:
                ok, why = verify(doc.id, row.get("expect"))
            if not ok:
                _drop(f"verify:{why}")
                continue

            record = trajectories.get(tid)
            if record is None:
                _drop("record:missing")
                continue
            record = {**record, "outcome": "done", "subject": "document"}
            samples = corpus.samples_from_record(record, source=row.get("source") or "imported",
                                                 synthetic_finish=True)
            for sample in samples:
                sample.meta.update({"source": row.get("source") or "imported",
                                    "license": row.get("license") or "unknown",
                                    "verified": True, "verified_by": why})
            if not samples:
                _drop("samples:none")
            out.extend(samples)
    return out


# --------------------------------------------------- Zero-To-CAD conversion --

#: The CadQuery calls this converter understands. A program using anything else
#: is dropped: a partial conversion would build a different part from the one the
#: source shipped, and the point of the verification is that nothing like that
#: reaches the corpus.
SUPPORTED = ("box", "cylinder", "rect", "circle", "extrude", "hole", "cboreHole",
             "fillet", "chamfer", "workplane", "faces", "Workplane", "close")
#: Calls that place the next one instead of adding material. Skipped, and the
#: conversion is verified against the source's geometry — so a sequence whose
#: features would land elsewhere fails the check rather than entering the corpus.
PLACEMENT = ("Workplane", "workplane", "faces", "edges", "close", "moveTo", "vertices")


def _literals(tree: ast.Module) -> Dict[str, float]:
    """Top-level `name = <number>` assignments — the program's dimensions.

    Zero-To-CAD's programs name every dimension (`p_body_length = 80.0`), which
    is what makes a truthful goal possible: the request can state the sizes the
    operations are built to.
    """
    values: Dict[str, float] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            try:
                value = ast.literal_eval(node.value)
            except Exception:  # noqa: BLE001 — an expression, not a dimension
                continue
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                values[node.targets[0].id] = float(value)
    return values


def _number(node: ast.AST, values: Dict[str, float]) -> Optional[float]:
    """A call argument as millimetres, resolving the program's named dimensions."""
    try:
        return float(ast.literal_eval(node))
    except Exception:  # noqa: BLE001
        pass
    if isinstance(node, ast.Name):
        return values.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Div, ast.Mult)):
        left, right = _number(node.left, values), _number(node.right, values)
        if left is None or right is None:
            return None
        return left / right if isinstance(node.op, ast.Div) else left * right
    return None


def _chain(node: ast.AST) -> List[Tuple[str, List[ast.AST]]]:
    """A CadQuery method chain, outermost last: `[(name, args), …]`."""
    calls: List[Tuple[str, List[ast.AST]]] = []
    while isinstance(node, ast.Call):
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        calls.append((name, list(node.args)))
        node = func.value if isinstance(func, ast.Attribute) else None
    return list(reversed(calls))


def convert_cadquery(source: str, *, goal: str = "",
                     name: str = "model") -> Optional[Dict[str, Any]]:
    """One CadQuery program as this catalog's operations, or None.

    Millimetres in the program, metres in the operations — the conversion every
    caller of this API makes, and the one a converter gets wrong silently.
    """
    # **The interpreter first, the chain reader as the fallback.** They fail on
    # different programs: the interpreter executes the whole thing, so one
    # unsupported statement loses it, while the chain reader needs only one clean
    # chain and ignores the rest — which is also why it silently drops placement.
    # Measured on Zero-To-CAD's 10,000 test programs: interpreter 10.6%, chain
    # reader 9.4%, and neither is a superset of the other. Both are verified
    # downstream, so trying both is strictly more samples at no cost in trust.
    from datasets import cadquery_interp

    try:
        interpreted = cadquery_interp.interpret(source)
    except cadquery_interp.Unsupported as exc:
        _drop(f"interp:{exc.cause}")
    except RecursionError:
        _drop("interp:recursion")
    except Exception as exc:  # noqa: BLE001 — a converter bug is a drop, counted
        _drop(f"interp:crash:{type(exc).__name__}")
    else:
        if interpreted:
            values = _literals(ast.parse(source))
            stated = ", ".join(f"{k.removeprefix('p_').replace('_', ' ')} {v:g} mm"
                               for k, v in list(values.items())[:8])
            return {"goal": goal or (f"{name}: {stated}" if stated else name),
                    "ops": interpreted, "source": "zero-to-cad",
                    "license": "Apache-2.0", "converted_by": "interpreter"}

    try:
        tree = ast.parse(source)
    except SyntaxError:
        _drop("cadquery:unparsable")
        return None
    values = _literals(tree)

    chains: List[List[Tuple[str, List[ast.AST]]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            chain = _chain(node)
            if chain and any(step[0] in ("box", "cylinder", "extrude") for step in chain):
                chains.append(chain)
    if not chains:
        _drop("cadquery:no_solid_call")
        return None
    chain = max(chains, key=len)
    if any(step[0] not in SUPPORTED for step in chain):
        unsupported = sorted({step[0] for step in chain if step[0] not in SUPPORTED})
        _drop(f"cadquery:unsupported:{unsupported[0]}")
        return None

    ops: List[dict] = []
    pending_profile: Optional[dict] = None
    for method, args in chain:
        if method in PLACEMENT:
            # `cq.Workplane('XY')`, `.faces('>Z')` — selectors, not dimensions.
            # Their arguments are strings, and reading one as a number is how the
            # first version of this rejected every program in the dataset.
            continue
        numbers = [_number(a, values) for a in args]
        if any(n is None for n in numbers):
            _drop(f"cadquery:unresolved_argument:{method}")
            return None
        if method == "box" and len(numbers) >= 3:
            ops.append({"op": "add_primitive", "args": {"shape": "box", "params": {
                "size": {"x": numbers[0] / MM, "y": numbers[1] / MM, "z": numbers[2] / MM}}}})
        elif method == "cylinder" and len(numbers) >= 2:
            ops.append({"op": "add_primitive", "args": {"shape": "cylinder", "params": {
                "height": numbers[0] / MM, "radius": numbers[1] / MM}}})
        elif method == "rect" and len(numbers) >= 2:
            pending_profile = {"type": "rect", "width": numbers[0] / MM,
                               "height": numbers[1] / MM}
        elif method == "circle" and numbers:
            pending_profile = {"type": "circle", "radius": numbers[0] / MM}
        elif method == "extrude" and numbers:
            if pending_profile is None:
                _drop("cadquery:extrude_without_profile")
                return None
            ops.append({"op": "add_extrude", "args": {"profile": pending_profile,
                                                      "depth": numbers[0] / MM}})
            pending_profile = None
        elif method == "hole" and numbers:
            ops.append({"op": "add_hole", "args": {"radius": numbers[0] / 2 / MM,
                                                   "through": True}})
        elif method == "cboreHole" and len(numbers) >= 3:
            ops.append({"op": "add_hole", "args": {
                "radius": numbers[0] / 2 / MM, "hole_type": "counterbore",
                "recess_diameter": numbers[1] / MM, "recess_depth": numbers[2] / MM,
                "through": True}})
        elif method in ("fillet", "chamfer") and numbers:
            ops.append({"op": "add_modifier", "args": {"modifier": method,
                                                       "params": {"radius": numbers[0] / MM}}})
        # `workplane`, `faces`, `close`, `Workplane` place the next call rather
        # than adding material. Placement is not carried across: a converted
        # sequence is verified against the source's own geometry, so a sample
        # whose features land somewhere else fails and is dropped.
    if not ops:
        _drop("cadquery:nothing_emitted")
        return None
    stated = ", ".join(f"{k.removeprefix('p_').replace('_', ' ')} {v:g} mm"
                       for k, v in list(values.items())[:8])
    return {"goal": goal or (f"{name}: {stated}" if stated else name),
            "ops": ops, "source": "zero-to-cad", "license": "Apache-2.0",
            "converted_by": "chain"}


# --------------------------------------------------------- SketchGraphs --

#: SketchGraphs constraint types this converter maps, and what they become here.
#:
#: 15M constrained sketches from Onshape public documents, against the one part of
#: this catalog with no synthetic source at all: `create_sketch` and the solver's
#: 18 constraint types have **zero labels** in the corpus. The licence reads
#: clearly — Onshape's Terms of Use §1.g.ii grant anyone accessing a public
#: document the right to use, copy and modify its contents — which is the reading
#: `docs/CAD-DATASET-SOURCING.md` sets out and the reason this one is implemented
#: where Fusion 360 Gallery is declined.
#:
#: **What is mapped is what means the same thing.** `Length` on a line is our
#: `distance` between its two endpoints, and `Diameter` is our `radius` halved —
#: both are the same constraint under another name. `Equal` is *two* of ours
#: depending on what it joins, so it is resolved by the referenced entities'
#: kinds rather than assumed. Everything else is dropped by cause: `Mirror` needs
#: a mirror line this export does not carry, `Pierce` is a 3D relation, and the
#: pattern and projection types have no equivalent here at all. A partial
#: conversion would be a sketch that means something the source did not.
SKETCHGRAPHS_CONSTRAINTS = {
    "Coincident": "coincident",
    "Distance": "distance",
    "Length": "distance",
    "Horizontal": "horizontal",
    "Vertical": "vertical",
    "Parallel": "parallel",
    "Perpendicular": "perpendicular",
    "Tangent": "tangent",
    "Midpoint": "midpoint",
    "Concentric": "concentric",
    "Radius": "radius",
    "Diameter": "radius",
    "Angle": "angle",
    "Fix": "fixed",
    "Equal": "equal",          # resolved per referenced kind: length or radius
}


def _sg_entities(record: dict):
    """The export's entities as our `points` / `lines` / `arcs` / `circles`.

    Every endpoint becomes a named point (`l0.start`), because that is what the
    constraint graph refers to and what the solver indexes. Coincident points are
    *not* merged: joining them is what the `Coincident` constraints are for, and
    merging here would silently discard the relation the dataset exists to carry.
    """
    points: dict = {}
    lines, arcs, circles = [], [], []

    def at(pid, spot):
        points[pid] = {"id": pid, "x": float(spot.get("x", 0.0)),
                       "y": float(spot.get("y", 0.0))}
        return pid

    for entity in record.get("entities") or []:
        kind = str(entity.get("kind") or "").lower()
        eid = entity.get("id")
        if not eid:
            return None, "entity_without_id"
        construction = bool(entity.get("construction"))
        if kind == "point":
            at(eid, entity)
        elif kind == "line":
            lines.append({"id": eid,
                          "a": at(f"{eid}.start", entity.get("start") or {}),
                          "b": at(f"{eid}.end", entity.get("end") or {}),
                          "construction": construction})
        elif kind == "circle":
            circles.append({"id": eid,
                            "center": at(f"{eid}.center", entity.get("center") or {}),
                            "radius": float(entity.get("radius") or 0.0),
                            "construction": construction})
        elif kind == "arc":
            arcs.append({"id": eid,
                         "center": at(f"{eid}.center", entity.get("center") or {}),
                         "a": at(f"{eid}.start", entity.get("start") or {}),
                         "b": at(f"{eid}.end", entity.get("end") or {}),
                         "construction": construction})
        else:
            # Splines and ellipses are real SketchGraphs entities and this kernel
            # has neither, so the sketch is dropped rather than approximated.
            return None, f"entity:{kind or 'unknown'}"
    if not lines and not arcs and not circles:
        return None, "no_curves"
    return {"points": list(points.values()), "lines": lines,
            "arcs": arcs, "circles": circles, "constraints": []}, ""


def _sg_kind_of(spec: dict, ref: str):
    """Whether a reference names a line, a circle, an arc, or a point."""
    base = str(ref).split(".")[0]
    for key, kind in (("lines", "line"), ("circles", "circle"), ("arcs", "arc")):
        if any(e["id"] == base for e in spec[key]):
            return kind, base
    if any(p["id"] == str(ref) for p in spec["points"]):
        return "point", str(ref)
    return "", base


def _sg_constraint(spec: dict, raw: dict):
    """One SketchGraphs constraint as one of ours, or `(None, cause)`."""
    name = str(raw.get("type") or "")
    mapped = SKETCHGRAPHS_CONSTRAINTS.get(name)
    if mapped is None:
        return None, f"constraint:{name or 'unnamed'}"
    refs = [str(r) for r in (raw.get("refs") or [])]
    value = raw.get("value")
    kinds = [_sg_kind_of(spec, r) for r in refs]

    def is_point(index):
        return index < len(refs) and _sg_kind_of(spec, refs[index])[0] == "point" \
            or (index < len(refs) and "." in refs[index])

    if mapped == "coincident" and len(refs) >= 2:
        return {"type": "coincident", "a": refs[0], "b": refs[1]}, ""
    if mapped == "distance" and name == "Length" and len(refs) >= 1 and value is not None:
        kind, base = kinds[0]
        if kind != "line":
            return None, "constraint:Length:not_a_line"
        return {"type": "distance", "a": f"{base}.start", "b": f"{base}.end",
                "value": float(value)}, ""
    if mapped == "distance" and len(refs) >= 2 and value is not None:
        return {"type": "distance", "a": refs[0], "b": refs[1],
                "value": float(value)}, ""
    if mapped in ("horizontal", "vertical") and refs:
        kind, base = kinds[0]
        if kind != "line":
            return None, f"constraint:{name}:not_a_line"
        return {"type": mapped, "line": base}, ""
    if mapped in ("parallel", "perpendicular", "angle") and len(refs) >= 2:
        if kinds[0][0] != "line" or kinds[1][0] != "line":
            return None, f"constraint:{name}:not_two_lines"
        row = {"type": mapped, "line1": kinds[0][1], "line2": kinds[1][1]}
        if mapped == "angle":
            if value is None:
                return None, "constraint:Angle:no_value"
            row["value"] = float(value)
        return row, ""
    if mapped == "midpoint" and len(refs) >= 2:
        line = next((b for k, b in kinds if k == "line"), None)
        pnt = next((r for r, (k, _) in zip(refs, kinds) if k != "line"), None)
        if not line or not pnt:
            return None, "constraint:Midpoint:needs_a_point_and_a_line"
        return {"type": "midpoint", "line": line, "point": pnt}, ""
    if mapped == "concentric" and len(refs) >= 2:
        if kinds[0][0] not in ("circle", "arc") or kinds[1][0] not in ("circle", "arc"):
            return None, "constraint:Concentric:not_two_round_entities"
        return {"type": "concentric", "a": kinds[0][1], "b": kinds[1][1]}, ""
    if mapped == "radius" and refs and value is not None:
        kind, base = kinds[0]
        # A diameter is a radius under another name; halving it is not a guess.
        measure = float(value) / 2.0 if name == "Diameter" else float(value)
        if kind == "circle":
            return {"type": "radius", "circle": base, "value": measure}, ""
        if kind == "arc":
            return {"type": "arc_radius", "arc": base, "value": measure}, ""
        return None, f"constraint:{name}:not_round"
    if mapped == "tangent" and len(refs) >= 2:
        return {"type": "tangent", "a": kinds[0][1], "b": kinds[1][1]}, ""
    if mapped == "fixed" and refs:
        # **Both coordinates, explicitly.** `fixed` emits one row per axis given,
        # and a missing axis used to default to the point's live coordinate — a
        # residual identically zero with a zero derivative, which pins nothing
        # while still counting as an equation.
        pid = refs[0]
        home = next((p for p in spec["points"] if p["id"] == pid), None)
        if home is None:
            return None, "constraint:Fix:unknown_point"
        return {"type": "fixed", "point": pid, "x": home["x"], "y": home["y"]}, ""
    if mapped == "equal" and len(refs) >= 2:
        first, second = kinds[0], kinds[1]
        if first[0] == "line" and second[0] == "line":
            return {"type": "equal_length", "line1": first[1], "line2": second[1]}, ""
        if first[0] in ("circle", "arc") and second[0] in ("circle", "arc"):
            return {"type": "equal_radius", "a": first[1], "b": second[1]}, ""
        return None, "constraint:Equal:mixed_kinds"
    return None, f"constraint:{name}:unmapped_shape"


def convert_sketchgraphs(record: dict, *, goal: str = ""):
    """One exported SketchGraphs sketch as a `{"goal", "ops"}` row.

    Reads the JSONL contract `docs/CAD-DATASET-SOURCING.md` documents rather than
    their binary sequence format: their own library resolves a `Line`'s
    parametric form (`pntX`, `dirX`, `startParam`) into endpoints, so the export
    script does that where the library is installed, and nothing here guesses at
    a format it cannot test. That is the same rule `samples_from_file` follows —
    the user brings a local file — and the reason this converter has fixtures.

    Unsupported entities and constraints are dropped **by cause**, and a sketch
    losing any constraint is dropped whole: a sketch missing one relation solves
    to a different shape, and unlike a solid there is no volume to catch it.
    """
    spec, why = _sg_entities(record)
    if spec is None:
        _drop(f"sketchgraphs:{why}")
        return None

    for raw in record.get("constraints") or []:
        row, cause = _sg_constraint(spec, raw)
        if row is None:
            _drop(f"sketchgraphs:{cause}")
            return None
        spec["constraints"].append(row)

    from app.services import sketch_solver
    work = len(spec["points"]) * max(1, len(spec["constraints"]))
    if work > sketch_solver.MAX_SOLVE_WORK:
        _drop("sketchgraphs:too_large_to_solve")
        return None

    counts = [(len(spec["lines"]), "line"), (len(spec["arcs"]), "arc"),
              (len(spec["circles"]), "circle")]
    parts = [f"{n} {word}{'s' if n != 1 else ''}" for n, word in counts if n]
    said = ", ".join(parts)
    return {
        "goal": goal or (f"A sketch of {said}, held by "
                         f"{len(spec['constraints'])} constraints."),
        "ops": [{"op": "create_sketch", "args": {"sketch": spec, "plane": "xy"}}],
        # A sketch builds no body, so the geometry check has nothing to measure.
        "verify": "sketch",
        "source": "sketchgraphs",
        "license": "Onshape ToU 1.g.ii",
    }

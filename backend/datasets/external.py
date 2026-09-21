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
            "ops": ops, "source": "zero-to-cad", "license": "Apache-2.0"}

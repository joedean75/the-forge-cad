"""Starter parts — a feature tree with its driving parameters already named.

A new document is three datum planes and no body, which is correct and leaves a
beginner with nothing to edit. A template fills that: not a file to import, but
the tree somebody would have built, with the numbers that drive it already
lifted out into named parameters. The first thing a user does is change
``length`` from 100 to 140, not construct a box.

**A template is an operation sequence, not a serialized tree.** Each one is a
list of ``{op, args}`` run through :func:`decompiler.replay`, which is the same
path a recorded trajectory replays through, with the same ``{"$step": i}``
references. That is not a stylistic choice — three properties fall out of it and
none of them survive a hand-written tree:

- **It can only build what the catalog can build.** A template that reached past
  the operation surface would be content a model could not reproduce, and it
  would show up in ``corpus_report`` as an undecompilable document dragging
  every feature type in it to 0%.
- **It is a demonstration.** Applied inside a recording session it writes a real
  trajectory, because the operations genuinely run.
- **There is one implementation.** The gallery button and an agent calling
  ``apply_template`` execute the identical sequence.

**Parameters are bound in a second step, deliberately.** ``add_primitive`` takes
``params.size.x`` as a *number* and ``op_schema`` enforces it, so the expression
string ``"=length"`` cannot be smuggled in there — nor should it be, since the
schema is what a constrained decoder reads. The literal is built first and
``set_feature_params`` — whose ``params`` is an opaque object the feature owns —
rebinds it to the expression. That is exactly the sequence a person performs.

The literals and the expressions must agree: the template is applied by running
the steps, so a mismatch is a body that jumps the first time a parameter is
touched. ``test_templates`` evaluates each one and checks the bound tree against
the declared parameter values.
"""

from __future__ import annotations

from typing import Dict, List, NamedTuple, Optional


class Template(NamedTuple):
    key: str
    name: str
    summary: str
    #: What the finished part is for, in the words someone searching would use.
    tags: tuple
    #: ``[{name, expr, unit}]`` — the driving parameters, in dependency order.
    parameters: List[dict]
    #: ``[{op, args}]``, ``{"$step": i}`` referring to the feature step *i* made.
    steps: List[dict]


def _mm(**kw) -> List[dict]:
    return [{"name": n, "expr": str(v), "unit": "mm"} for n, v in kw.items()]


def _box(size, center, operation: str = "union") -> dict:
    return {"op": "add_primitive",
            "args": {"shape": "box", "operation": operation,
                     "params": {"size": dict(zip("xyz", size)),
                                "center": dict(zip("xyz", center))}}}


def _cyl(radius, height, center, axis=(0.0, 0.0, 1.0), operation: str = "union") -> dict:
    return {"op": "add_primitive",
            "args": {"shape": "cylinder", "operation": operation,
                     "params": {"radius": radius, "height": height,
                                "center": dict(zip("xyz", center)),
                                "axis": dict(zip("xyz", axis))}}}


def _bind(step: int, params: dict) -> dict:
    return {"op": "set_feature_params",
            "args": {"feature_id": {"$step": step}, "params": params}}


def _hole(screw: str, center, axis=(0.0, 0.0, 1.0)) -> dict:
    return {"op": "clearance_hole",
            "args": {"screw": screw, "center": dict(zip("xyz", center)),
                     "axis": dict(zip("xyz", axis))}}


# ---------------------------------------------------------------- the parts --

_PLATE = Template(
    key="plate",
    name="Mounting plate",
    summary="A rectangular plate with an M4 clearance hole inset from each corner.",
    tags=("plate", "bracket", "mount", "base"),
    parameters=_mm(length=100, width=60, thickness=8, hole_inset=10),
    steps=[
        {"op": "set_parameters", "args": {"parameters": _mm(
            length=100, width=60, thickness=8, hole_inset=10)}},
        _box((0.100, 0.060, 0.008), (0.0, 0.0, 0.004)),                        # 1
        _bind(1, {"size": {"x": "=length", "y": "=width", "z": "=thickness"},
                  "center": {"x": 0.0, "y": 0.0, "z": "=thickness/2"}}),
        _hole("M4", (0.040, 0.020, 0.0)),                                      # 3
        _hole("M4", (-0.040, 0.020, 0.0)),                                     # 4
        _hole("M4", (0.040, -0.020, 0.0)),                                     # 5
        _hole("M4", (-0.040, -0.020, 0.0)),                                    # 6
        # The hole depth follows the plate: a bore sized against the old
        # thickness stops inside a plate somebody thickened.
        _bind(3, {"height": "=thickness*4",
                  "center": {"x": "=length/2-hole_inset", "y": "=width/2-hole_inset", "z": 0.0}}),
        _bind(4, {"height": "=thickness*4",
                  "center": {"x": "=-(length/2-hole_inset)", "y": "=width/2-hole_inset", "z": 0.0}}),
        _bind(5, {"height": "=thickness*4",
                  "center": {"x": "=length/2-hole_inset", "y": "=-(width/2-hole_inset)", "z": 0.0}}),
        _bind(6, {"height": "=thickness*4",
                  "center": {"x": "=-(length/2-hole_inset)", "y": "=-(width/2-hole_inset)", "z": 0.0}}),
    ],
)

_BRACKET = Template(
    key="bracket",
    name="L-bracket",
    summary="Two legs at a right angle, one M5 clearance hole through each.",
    tags=("bracket", "angle", "L", "corner", "mount"),
    parameters=_mm(leg_a=80, leg_b=60, width=40, thickness=6, hole_inset=15),
    steps=[
        {"op": "set_parameters", "args": {"parameters": _mm(
            leg_a=80, leg_b=60, width=40, thickness=6, hole_inset=15)}},
        _box((0.080, 0.040, 0.006), (0.040, 0.0, 0.003)),                      # 1  foot
        _bind(1, {"size": {"x": "=leg_a", "y": "=width", "z": "=thickness"},
                  "center": {"x": "=leg_a/2", "y": 0.0, "z": "=thickness/2"}}),
        _box((0.006, 0.040, 0.060), (0.003, 0.0, 0.030)),                      # 3  upright
        _bind(3, {"size": {"x": "=thickness", "y": "=width", "z": "=leg_b"},
                  "center": {"x": "=thickness/2", "y": 0.0, "z": "=leg_b/2"}}),
        _hole("M5", (0.065, 0.0, 0.0)),                                        # 5
        _bind(5, {"height": "=thickness*4",
                  "center": {"x": "=leg_a-hole_inset", "y": 0.0, "z": 0.0}}),
        # Through the upright, so the axis is X and the depth follows the wall.
        _hole("M5", (0.0, 0.0, 0.045), axis=(1.0, 0.0, 0.0)),                  # 7
        _bind(7, {"height": "=thickness*4",
                  "center": {"x": "=thickness/2", "y": 0.0, "z": "=leg_b-hole_inset"}}),
    ],
)

_ENCLOSURE = Template(
    key="enclosure",
    name="Open-top enclosure",
    summary="A walled box with a floor and an open top, cut as an inner cavity.",
    tags=("enclosure", "box", "case", "housing", "shell"),
    parameters=_mm(length=120, width=80, height=40, wall=3),
    steps=[
        {"op": "set_parameters", "args": {"parameters": _mm(
            length=120, width=80, height=40, wall=3)}},
        _box((0.120, 0.080, 0.040), (0.0, 0.0, 0.020)),                        # 1
        _bind(1, {"size": {"x": "=length", "y": "=width", "z": "=height"},
                  "center": {"x": 0.0, "y": 0.0, "z": "=height/2"}}),
        # The cavity runs past the top face, which is what opens it. A
        # ShellFeature would hollow uniformly and leave the lid on — that is
        # `abs(d) - t/2`, not a face removal.
        _box((0.114, 0.074, 0.040), (0.0, 0.0, 0.023), operation="subtract"),  # 3
        _bind(3, {"size": {"x": "=length-2*wall", "y": "=width-2*wall", "z": "=height"},
                  "center": {"x": 0.0, "y": 0.0, "z": "=wall+height/2"}}),
    ],
)

_SHAFT = Template(
    key="shaft",
    name="Stepped shaft",
    summary="A round shaft with a smaller journal on one end for a bearing.",
    tags=("shaft", "axle", "spindle", "pin", "round"),
    parameters=_mm(shaft_dia=20, shaft_length=120, journal_dia=12, journal_length=25),
    steps=[
        {"op": "set_parameters", "args": {"parameters": _mm(
            shaft_dia=20, shaft_length=120, journal_dia=12, journal_length=25)}},
        _cyl(0.010, 0.120, (0.0, 0.0, 0.060)),                                 # 1
        _bind(1, {"radius": "=shaft_dia/2", "height": "=shaft_length",
                  "center": {"x": 0.0, "y": 0.0, "z": "=shaft_length/2"}}),
        _cyl(0.006, 0.025, (0.0, 0.0, 0.1325)),                                # 3
        _bind(3, {"radius": "=journal_dia/2", "height": "=journal_length",
                  "center": {"x": 0.0, "y": 0.0, "z": "=shaft_length+journal_length/2"}}),
    ],
)

TEMPLATES: Dict[str, Template] = {
    t.key: t for t in (_PLATE, _BRACKET, _ENCLOSURE, _SHAFT)
}


def by_key(key: str) -> Optional[Template]:
    return TEMPLATES.get(str(key).strip().lower())


def describe() -> List[dict]:
    """The gallery listing: what each one is, and what it will let you change."""
    return [{"key": t.key, "name": t.name, "summary": t.summary,
             "tags": list(t.tags), "steps": len(t.steps),
             "parameters": [{"name": p["name"], "value": float(p["expr"]), "unit": p["unit"]}
                            for p in t.parameters]}
            for t in TEMPLATES.values()]


def apply(key: str, doc_id: str) -> dict:
    """Run a template's operation sequence into a document.

    Returns what was built. A step that fails stops the sequence — `replay`
    breaks on the first non-ok envelope — and the failure is reported with the
    op that produced it rather than as a generic "template failed", because the
    only useful thing to say about a broken template is which step broke.
    """
    from app.services import decompiler

    template = TEMPLATES[str(key).strip().lower()]
    envelopes = decompiler.replay(template.steps, doc_id)
    failed = next((i for i, e in enumerate(envelopes) if not e.get("ok")), None)
    if failed is not None:
        error = envelopes[failed].get("error") or {}
        return {"ok": False, "failed_step": failed,
                "failed_op": template.steps[failed]["op"],
                "error": error.get("message", "the step did not succeed")}
    created = [e.get("result", {}).get("feature_id") for e in envelopes]
    return {"ok": True, "template": template.key, "name": template.name,
            "steps": len(envelopes),
            "feature_ids": [c for c in created if c],
            "parameters": [p["name"] for p in template.parameters]}

"""The frozen task set.

Two kinds live here and they answer different questions.

**Goal tasks** are what a benchmark normally means: a sentence, and a spec that
says what satisfying it would measure. They need a planner — an LLM — and they
are the number that A2 and A3 are trying to move.

**Adversarial tasks** are the same harness pointed at the *surface* rather than
at a model. Each replays a specific mistake a model is known to make against
this catalog, and asserts what the API does about it. They need no API key,
which is what lets them run in CI on every commit, and they are how a
"silent success" regression is caught: every one of them once returned
``ok: true`` and a body nobody asked for.

Nothing here may leak into a prompt, a default or a parser shortcut. A task set
you can pass by memorising is worse than no task set — the old text-to-CAD
parser's ``"rc" in prompt`` shortcut is what that looks like when it happens.
"""

from __future__ import annotations

from typing import Dict, Sequence

from evals.agent import Task

MM = 0.001


def _bbox(axis: str, value: float, tolerance: float = 1.0) -> dict:
    return {"kind": "bounding_box", "axis": axis, "value": value, "tolerance": tolerance}


# --------------------------------------------------------------- goal tasks --
#
# Deliberately spread across the feature space rather than clustered on the easy
# corner. A suite of plain boxes scores well and says nothing — the same lesson
# `decompiler.corpus_report` learned about its own corpus.

GOALS: Sequence[Task] = (
    Task(
        id="plate",
        goal="A flat rectangular plate 100 mm long, 60 mm wide and 8 mm thick.",
        requirements=[_bbox("x", 100), _bbox("y", 60), _bbox("z", 8),
                      {"kind": "watertight"}],
        tags=("primitive",),
    ),
    Task(
        id="plate-4-holes",
        goal="A 100 x 60 x 8 mm plate with four 5 mm diameter holes through it, "
             "one near each corner.",
        requirements=[_bbox("x", 100), _bbox("y", 60), _bbox("z", 8),
                      {"kind": "hole", "diameter": 5, "count": 4},
                      {"kind": "watertight"}],
        max_steps=10,
        tags=("holes",),
    ),
    Task(
        id="boss-on-plate",
        goal="An 80 x 80 x 6 mm plate with a 20 mm diameter boss standing 12 mm "
             "proud of its top face, in the middle.",
        requirements=[_bbox("x", 80), _bbox("y", 80), _bbox("z", 18),
                      {"kind": "bodies", "value": 1, "tolerance": 0},
                      {"kind": "watertight"}],
        tags=("boolean",),
    ),
    Task(
        id="bushing",
        goal="A cylindrical bushing: 30 mm outside diameter, 16 mm bore, 25 mm long, "
             "with the bore running all the way through.",
        requirements=[_bbox("x", 30), _bbox("y", 30), _bbox("z", 25),
                      {"kind": "hole", "diameter": 16, "count": 1},
                      {"kind": "watertight"}],
        tags=("holes", "boolean"),
    ),
    Task(
        id="steel-plate",
        goal="A 50 x 50 x 10 mm plate in 1018 steel.",
        requirements=[_bbox("x", 50), _bbox("y", 50), _bbox("z", 10),
                      {"kind": "material", "value": "steel-1018"},
                      # 50*50*10 mm^3 of steel at 7870 kg/m^3 is 196.75 g.
                      {"kind": "mass", "value": 196.75, "tolerance": 5}],
        tags=("material",),
    ),
    Task(
        id="filleted-block",
        goal="A 40 x 40 x 20 mm block with every edge rounded by 3 mm.",
        requirements=[_bbox("x", 40), _bbox("y", 40), _bbox("z", 20),
                      {"kind": "feature", "value": "EdgeFilletFeature"},
                      {"kind": "watertight"}],
        tags=("exact-kernel",),
    ),
)


# -------------------------------------------------------- adversarial tasks --

def _cube_spec() -> list:
    """The plate every adversarial script is *asked* for: 100 x 60 x 8 mm."""
    return [_bbox("x", 100), _bbox("y", 60), _bbox("z", 8)]


ADVERSARIAL: Sequence[Task] = (
    Task(
        id="adv:millimetres-for-metres",
        expect="warn",
        goal="A plate 100 mm by 60 mm by 8 mm.",
        requirements=_cube_spec(),
        # The single likeliest error against this catalog: the observation comes
        # back in millimetres (`bbox_size_mm`) and the arguments are in metres,
        # so a model reads 100.0 and must write 0.1 on every call.
        script=[{"op": "add_primitive",
                 "args": {"shape": "box", "params": {"size": {"x": 100, "y": 60, "z": 8}}}}],
        max_steps=2,
        tags=("units",),
    ),
    Task(
        id="adv:invented-parameter-names",
        expect="refuse",
        goal="A plate 100 mm by 60 mm by 8 mm.",
        requirements=_cube_spec(),
        # `_PRIMITIVE_PARAMS` declares `additionalProperties: false` and the
        # validator never descended into it, so these three invented keys were
        # dropped on the floor and a default 50 mm cube came back as `ok: true`.
        script=[{"op": "add_primitive",
                 "args": {"shape": "box",
                          "params": {"width": 0.1, "depth": 0.06, "thickness": 0.008}}}],
        max_steps=2,
        tags=("schema",),
    ),
    Task(
        id="adv:wrong-nested-type",
        expect="refuse",
        goal="A plate 100 mm by 60 mm by 8 mm.",
        requirements=_cube_spec(),
        # A vector as a list rather than {x, y, z}. Declared as an object;
        # reached the kernel and failed there, as an evaluation warning about
        # `Vector3() argument after ** must be a mapping`.
        script=[{"op": "add_primitive",
                 "args": {"shape": "box", "params": {"size": [0.1, 0.06, 0.008]}}}],
        max_steps=2,
        tags=("schema",),
    ),
    Task(
        id="adv:omitted-profile",
        expect="refuse",
        goal="Extrude a 100 x 60 mm rectangle to 8 mm.",
        requirements=_cube_spec(),
        # No profile and no sketch. This built a 50 x 30 mm rectangle from a
        # default and reported success — an invention, not a default.
        script=[{"op": "add_extrude", "args": {"depth": 0.008}}],
        max_steps=2,
        tags=("required",),
    ),
    Task(
        id="adv:empty-revolve",
        expect="refuse",
        goal="Revolve a profile into a 100 x 60 x 8 mm part.",
        requirements=_cube_spec(),
        script=[{"op": "add_revolve", "args": {}}],
        max_steps=2,
        tags=("required",),
    ),
    Task(
        id="adv:correct-plate",
        goal="A plate 100 mm by 60 mm by 8 mm.",
        requirements=_cube_spec(),
        # The control. If tightening the surface ever breaks *this*, the
        # tightening went too far, and every other row in the suite would still
        # look like an improvement.
        script=[{"op": "add_primitive",
                 "args": {"shape": "box",
                          "params": {"size": {"x": 0.1, "y": 0.06, "z": 0.008}}}}],
        max_steps=2,
        tags=("control",),
    ),
    Task(
        id="adv:recovers-after-a-refusal",
        goal="A plate 100 mm by 60 mm by 8 mm.",
        requirements=_cube_spec(),
        # A refusal is only useful if the next call can be right. This is the
        # shape of every repair: bad call, typed error, corrected call.
        script=[
            {"op": "add_primitive", "args": {"shape": "cube"}},
            {"op": "add_primitive",
             "args": {"shape": "box",
                      "params": {"size": {"x": 0.1, "y": 0.06, "z": 0.008}}}},
        ],
        max_steps=3,
        tags=("control", "recovery"),
    ),
)


SUITES: Dict[str, Sequence[Task]] = {
    "scripted": ADVERSARIAL,
    "goals": GOALS,
    "all": tuple(ADVERSARIAL) + tuple(GOALS),
}

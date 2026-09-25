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

    # ---------------------------------------------------------------------
    # Six tasks could tell 0.533 from 0.900 and could not tell 0.900 from
    # 0.867: one task is 0.167 of the headline, the planner samples at
    # temperature 0.2, and three checkpoints of one model ranked differently
    # while each stayed within a task of the others. These widen the base so a
    # single flip cannot move the number, and they are chosen for *operations*
    # rather than shapes — a suite of six plates measured one op six times.
    #
    # Every requirement below was verified reachable by building the part the
    # goal describes through the catalog and scoring it: a task nothing can
    # satisfy is a permanent zero dragging the mean, which looks exactly like a
    # model that cannot do it. `test_eval_tasks.py` keeps that true.

    Task(
        id="chamfered-block",
        goal="A 40 x 40 x 20 mm block with every edge chamfered by 2 mm.",
        requirements=[_bbox("x", 40), _bbox("y", 40), _bbox("z", 20),
                      {"kind": "feature", "value": "EdgeChamferFeature"},
                      {"kind": "watertight"}],
        tags=("exact-kernel",),
    ),
    Task(
        id="shelled-tray",
        goal="A 60 x 40 x 30 mm box hollowed out to a 3 mm wall thickness, "
             "open at the top.",
        requirements=[_bbox("x", 60), _bbox("y", 40), _bbox("z", 30),
                      {"kind": "feature", "value": "FaceShellFeature"},
                      {"kind": "watertight"}],
        tags=("exact-kernel",),
    ),
    Task(
        id="stepped-shaft",
        goal="A stepped shaft: a 24 mm diameter section 30 mm long, and a 14 mm "
             "diameter section 25 mm long, coaxial and end to end.",
        requirements=[_bbox("x", 24), _bbox("y", 24), _bbox("z", 55),
                      {"kind": "bodies", "value": 1, "tolerance": 0},
                      {"kind": "watertight"}],
        tags=("boolean",),
    ),
    Task(
        id="bolt-circle",
        goal="An 80 x 80 x 6 mm plate with six 4 mm diameter holes through it, "
             "equally spaced around a 60 mm diameter circle at its centre.",
        requirements=[_bbox("x", 80), _bbox("y", 80), _bbox("z", 6),
                      {"kind": "hole", "diameter": 4, "count": 6},
                      {"kind": "watertight"}],
        max_steps=12,
        tags=("holes", "pattern"),
    ),
    Task(
        id="two-cubes",
        # The one task that is *supposed* to be two solids: `bodies` exists to
        # tell "the part is two parts by design" from "the part fell in half",
        # and nothing measured the first reading.
        goal="One part made of two separate 10 mm cubes, their centres 30 mm "
             "apart along X, not touching.",
        requirements=[_bbox("x", 40), _bbox("y", 10), _bbox("z", 10),
                      {"kind": "bodies", "value": 2, "tolerance": 0}],
        tags=("boolean", "bodies"),
    ),
    Task(
        id="aluminium-bar",
        # 100 x 20 x 10 mm of 6061 at 2700 kg/m^3 is 54.0 g.
        goal="A 100 x 20 x 10 mm bar in 6061 aluminium.",
        requirements=[_bbox("x", 100), _bbox("y", 20), _bbox("z", 10),
                      {"kind": "material", "value": "al-6061"},
                      {"kind": "mass", "value": 54.0, "tolerance": 2}],
        tags=("material",),
    ),
    Task(
        id="titanium-disc",
        # pi * 25^2 * 8 = 15,708 mm^3 of Ti-6Al-4V at 4430 kg/m^3 is 69.6 g.
        goal="A 50 mm diameter, 8 mm thick disc in Ti-6Al-4V titanium.",
        requirements=[_bbox("x", 50), _bbox("y", 50), _bbox("z", 8),
                      {"kind": "material", "value": "ti-6al4v"},
                      {"kind": "mass", "value": 69.6, "tolerance": 3}],
        tags=("material",),
    ),
    Task(
        id="volume-block",
        # The only task on `volume`, which is a different measurement from mass
        # (no material in it) and was scored by nothing.
        goal="A solid rectangular block 50 mm by 40 mm by 30 mm.",
        requirements=[_bbox("x", 50), _bbox("y", 40), _bbox("z", 30),
                      {"kind": "volume", "value": 60.0, "tolerance": 1},
                      {"kind": "watertight"}],
        tags=("primitive",),
    ),
    Task(
        id="counterbored-plate",
        # Two diameters on one hole: the counterbore's own wall is a cylindrical
        # face, so "Ø5 through, Ø10 recess" is two countable requirements and a
        # plain bore fails the second without failing the first.
        goal="A 60 x 60 x 10 mm plate with four 5 mm diameter holes through it, "
             "each counterbored to 10 mm diameter and 4 mm deep from the top.",
        requirements=[_bbox("x", 60), _bbox("y", 60), _bbox("z", 10),
                      {"kind": "hole", "diameter": 5, "count": 4},
                      {"kind": "hole", "diameter": 10, "count": 4},
                      {"kind": "watertight"}],
        max_steps=12,
        tags=("holes",),
    ),
    Task(
        id="cone",
        goal="A cone 40 mm in diameter at its base and 30 mm tall.",
        requirements=[_bbox("x", 40), _bbox("y", 40), _bbox("z", 30),
                      {"kind": "bodies", "value": 1, "tolerance": 0},
                      {"kind": "watertight"}],
        tags=("primitive",),
    ),
    Task(
        id="rib-plate",
        goal="An 80 x 40 x 5 mm plate with a rib along its centreline: 3 mm "
             "thick at the tip and standing 10 mm above the plate.",
        requirements=[_bbox("x", 80), _bbox("y", 40), _bbox("z", 15),
                      {"kind": "feature", "value": "RibFeature"},
                      {"kind": "watertight"}],
        tags=("rib",),
    ),
    Task(
        id="sheet-bracket",
        # The bent bbox depends on the bend radius and the K-factor, so only the
        # flat direction is pinned: a figure this task cannot predict is a
        # requirement that measures the bend allowance rather than the model.
        goal="A 1.5 mm sheet metal bracket: a 60 x 40 mm base flange with one "
             "20 mm tall flange bent up 90 degrees along a short edge.",
        # Y, not X: the flange is on a short edge, so it grows in Z and pushes X
        # out by the bend's outside radius — measured 63.5 for a 60 mm flange,
        # which is the bend allowance rather than anything the goal states. Y is
        # the width the goal gives and the bend does not touch it.
        requirements=[_bbox("y", 40),
                      {"kind": "feature", "value": "SheetMetalFeature"},
                      {"kind": "watertight"}],
        tags=("sheet-metal",),
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
        id="adv:another-shapes-parameter",
        expect="refuse",
        goal="A cone 40 mm across its base and 30 mm tall.",
        requirements=[_bbox("x", 40), _bbox("y", 40), _bbox("z", 30)],
        # `radius` is a *declared* key — the schema lists the union of every
        # shape's, which is what a constrained decoder needs — and a cone reads
        # `base_radius`. So this passed validation, the handler defaulted the
        # radius to 30 mm, honoured the height, and returned `ok: true` for a
        # body half again as wide as the one asked for. Half the call obeyed is
        # what makes it hard to see: the height is right.
        script=[{"op": "add_primitive",
                 "args": {"shape": "cone",
                          "params": {"radius": 0.02, "height": 0.03}}}],
        max_steps=2,
        tags=("schema",),
    ),
    Task(
        id="adv:another-modifiers-parameter",
        expect="refuse",
        goal="Hollow the block out to a 2 mm wall.",
        requirements=_cube_spec(),
        # The same union in `_MODIFIER_PARAMS`: `radius` belongs to `hole`, a
        # shell reads `thickness`, and the 2 mm asked for became the default
        # 4 mm with nothing said.
        script=[{"op": "add_primitive",
                 "args": {"shape": "box",
                          "params": {"size": {"x": 0.1, "y": 0.06, "z": 0.008}}}},
                {"op": "add_modifier",
                 "args": {"modifier": "shell", "params": {"radius": 0.002}}}],
        max_steps=3,
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

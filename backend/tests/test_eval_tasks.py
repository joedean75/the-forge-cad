"""A goal task nothing can satisfy is a permanent zero that reads like a model
that cannot do it.

Two of the twelve tasks added to widen the suite were wrong when written — a
cone asked for `radius` where the feature reads `base_radius`, and a sheet metal
bracket pinned the axis the bend allowance extends — and both looked exactly
like a capable model failing. So each goal task is built here, through the
catalog, and scored against its own spec: the requirement values (a mass in
grams, a volume in cm³, a hole count) are checked to be the ones a correct build
actually produces.

This is not a test of the model. It is a test of the *ruler*.
"""

import math

import pytest

MM = 0.001


def _run(doc, op, **args):
    from app.services import cad_operations
    result = cad_operations.execute(op, doc, args)
    assert result["ok"], f"{op} failed: {result.get('error')}"
    return result


def _box(doc, x, y, z, cx=0.0, cy=0.0, cz=0.0, **kw):
    return _run(doc, "add_primitive", shape="box",
                params={"size": {"x": x * MM, "y": y * MM, "z": z * MM},
                        "center": {"x": cx * MM, "y": cy * MM, "z": cz * MM}}, **kw)


def _cyl(doc, r, h, cz=0.0, **kw):
    return _run(doc, "add_primitive", shape="cylinder",
                params={"radius": r * MM, "height": h * MM,
                        "center": {"x": 0, "y": 0, "z": cz * MM}}, **kw)


def _chamfered_block(doc):
    _box(doc, 40, 40, 20)
    _run(doc, "chamfer_edges", edges="all", distance=2 * MM)


def _shelled_tray(doc):
    _box(doc, 60, 40, 30)
    faces = _run(doc, "list_faces")["result"]["faces"]
    top = max((f for f in faces if f.get("kind") == "plane"),
              key=lambda f: f.get("position_mm", {}).get("z", -1e9))
    _run(doc, "shell_faces", faces=[top["id"]], thickness=3 * MM)


def _stepped_shaft(doc):
    _cyl(doc, 12, 30, cz=15)
    _cyl(doc, 7, 25, cz=42.5, operation="union")


def _bolt_circle(doc):
    _box(doc, 80, 80, 6)
    for i in range(6):
        angle = 2 * math.pi * i / 6
        _run(doc, "add_hole", radius=2 * MM, through=True,
             center={"x": 30 * MM * math.cos(angle),
                     "y": 30 * MM * math.sin(angle), "z": 0})


def _two_cubes(doc):
    _box(doc, 10, 10, 10, cx=-15)
    _box(doc, 10, 10, 10, cx=15, operation="union")


def _aluminium_bar(doc):
    _box(doc, 100, 20, 10)
    _run(doc, "set_material", material_id="al-6061")


def _titanium_disc(doc):
    _cyl(doc, 25, 8)
    _run(doc, "set_material", material_id="ti-6al4v")


def _volume_block(doc):
    _box(doc, 50, 40, 30)


def _counterbored_plate(doc):
    _box(doc, 60, 60, 10)
    for cx, cy in ((-20, -20), (20, -20), (-20, 20), (20, 20)):
        _run(doc, "add_hole", radius=2.5 * MM, through=True, hole_type="counterbore",
             recess_radius=5 * MM, recess_depth=4 * MM,
             center={"x": cx * MM, "y": cy * MM, "z": 0})


def _cone(doc):
    # `base_radius`, not `radius`: see `adv:another-shapes-parameter`.
    _run(doc, "add_primitive", shape="cone",
         params={"base_radius": 20 * MM, "height": 30 * MM,
                 "center": {"x": 0, "y": 0, "z": 0}})


def _rib_plate(doc):
    _box(doc, 80, 40, 5)
    _run(doc, "add_rib", start={"x": -35 * MM, "y": 0, "z": 2.5 * MM},
         end={"x": 35 * MM, "y": 0, "z": 2.5 * MM},
         thickness=3 * MM, height=10 * MM)


def _sheet_bracket(doc):
    _run(doc, "add_sheet_metal", thickness=1.5 * MM, length=60 * MM, width=40 * MM,
         flanges=[{"edge": "x+", "length": 20 * MM, "angle": 90}])


def _plate(doc):
    _box(doc, 100, 60, 8)


def _plate_4_holes(doc):
    _box(doc, 100, 60, 8)
    for cx, cy in ((-40, -20), (40, -20), (-40, 20), (40, 20)):
        _run(doc, "add_hole", radius=2.5 * MM, through=True,
             center={"x": cx * MM, "y": cy * MM, "z": 0})


def _boss_on_plate(doc):
    _box(doc, 80, 80, 6)
    _cyl(doc, 10, 12, cz=9, operation="union")


def _bushing(doc):
    _cyl(doc, 15, 25)
    _run(doc, "add_hole", radius=8 * MM, through=True)


def _steel_plate(doc):
    _box(doc, 50, 50, 10)
    _run(doc, "set_material", material_id="steel-1018")


def _filleted_block(doc):
    _box(doc, 40, 40, 20)
    _run(doc, "fillet_edges", edges="all", radius=3 * MM)


#: One builder per goal task. A task with no builder here is a task nobody has
#: shown to be satisfiable, which is the state this file exists to prevent.
BUILDERS = {
    "plate": _plate,
    "plate-4-holes": _plate_4_holes,
    "boss-on-plate": _boss_on_plate,
    "bushing": _bushing,
    "steel-plate": _steel_plate,
    "filleted-block": _filleted_block,
    "chamfered-block": _chamfered_block,
    "shelled-tray": _shelled_tray,
    "stepped-shaft": _stepped_shaft,
    "bolt-circle": _bolt_circle,
    "two-cubes": _two_cubes,
    "aluminium-bar": _aluminium_bar,
    "titanium-disc": _titanium_disc,
    "volume-block": _volume_block,
    "counterbored-plate": _counterbored_plate,
    "cone": _cone,
    "rib-plate": _rib_plate,
    "sheet-bracket": _sheet_bracket,
}


def _goal_ids():
    from evals import tasks
    return [t.id for t in tasks.SUITES["goals"]]


def test_every_goal_task_has_a_builder():
    """Added without one, a task's requirement values are nobody's measurement."""
    assert sorted(BUILDERS) == sorted(_goal_ids())


@pytest.mark.parametrize("task_id", _goal_ids())
def test_the_task_is_satisfiable(task_id):
    from app.services import cad_operations, document_service, rollout
    from evals import tasks

    task = next(t for t in tasks.SUITES["goals"] if t.id == task_id)
    with rollout.rollout(resolution=96):
        doc = document_service.create_document(name=f"satisfiable-{task_id}").id
        BUILDERS[task_id](doc)
        _run(doc, "set_spec", requirements=list(task.requirements))
        report = cad_operations.execute("verify_against_spec", doc, {})["result"]
    unmet = [c for c in report.get("checks", []) if c.get("status") != "pass"]
    assert report["score"] == 1.0, (
        f"{task_id} cannot be satisfied by a correct build: "
        + "; ".join(f"{c.get('id')}: {c.get('detail')}" for c in unmet))

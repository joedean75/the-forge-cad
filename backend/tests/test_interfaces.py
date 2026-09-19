"""Reading a purchased part's mounting interfaces off its exact body.

The ground truth is the catalog's own 2207 motor: its bell, shaft and bolt
pattern are table values, so the reading can be checked against numbers nobody
measured. It goes out as STEP and comes back in as an import — the path the
user's A2212 took — so the reader is tested on a `BrepBodyFeature` with no
feature history, not on the tree it was built from.
"""

import math

import pytest

from app.cad_kernel.io import step_import
from app.services import (artifacts, assembly_service, cad_operations, import_service,
                          interfaces, parts_catalog)
from app.models.schemas import ComponentTransform, Vector3
from conftest import whole_tree_document

pytestmark = pytest.mark.skipif(
    not step_import.available(), reason="interfaces are read from the exact kernel")


def _imported_motor():
    source = whole_tree_document(parts_catalog.brushless_motor("2207"), name="2207 source")
    data = artifacts._render(source.id, "step", resolution=64, spec=None)
    doc = import_service.import_document("motor-2207.step", data, name="2207 motor")
    assert "BrepBody" in str(doc.feature_tree.get("root", doc.feature_tree).get("type"))
    return doc


def _read(doc_id):
    env = cad_operations.execute("inspect_interfaces", doc_id, {}, include_state=False)
    assert env["ok"], env.get("error")
    return env["result"]


def test_the_catalog_motor_reads_back_its_table_values():
    reading = _read(_imported_motor().id)
    table = parts_catalog.MOTORS["2207"]

    assert reading["axis"]["direction"] == pytest.approx([0, 0, 1], abs=1e-6)
    assert reading["can"]["diameter_mm"] == pytest.approx(table.bell_dia, abs=0.01)

    shaft = reading["shaft"]
    assert shaft["diameter_mm"] == pytest.approx(table.shaft, abs=0.01)
    assert "5 mm" in shaft["fits"]
    top = next(e for e in shaft["ends"] if e["end"] == "+axis")
    assert top["projection_mm"] == pytest.approx(parts_catalog._MOTOR_SHAFT_STICKOUT, abs=0.05)

    mount = reading["mount_patterns"][0]
    clearance = parts_catalog.METRIC["M3"].clearance
    assert mount["count"] == 4 and mount["end"] == "-axis"
    assert mount["hole_diameter_mm"] == pytest.approx(clearance, abs=0.01)
    circle = mount["circles"][0]
    assert circle["holes"] == 4
    # A 16 x 16 square is a bolt circle of 16·√2.
    assert circle["diameter_mm"] == pytest.approx(16 * math.sqrt(2), abs=0.01)
    assert {"screw": "M3", "as": "clearance"}.items() <= mount["screw_candidates"][0].items()


def test_what_the_geometry_cannot_say_is_named_not_guessed():
    reading = _read(_imported_motor().id)
    assert "KV rating" in reading["not_in_geometry"]
    assert not any(k in reading for k in ("kv", "turns", "manufacturer"))


def test_a_placed_motor_reports_its_shaft_where_the_assembly_puts_it():
    motor = _imported_motor()
    asm = assembly_service.create_assembly("mount")
    asm = assembly_service.add_component(asm.id, motor.id)
    comp = asm.components[0]
    assembly_service.update_component(asm.id, comp.id, ComponentTransform(
        translate=Vector3(x=0.1, y=0.0, z=0.0), rotate_deg=Vector3(x=90.0, y=0.0, z=0.0)))

    env = cad_operations.execute("component_interfaces", asm.id,
                                 {"component_id": comp.id}, include_state=False)
    assert env["ok"], env.get("error")
    placed = env["result"]["in_assembly"]
    # Turned 90° about x, the motor's +z axis points along world -y.
    assert placed["axis"]["direction"] == pytest.approx([0, -1, 0], abs=1e-6)
    tip = placed["shaft_tips_mm"]["+axis"]
    assert tip[0] == pytest.approx(100.0, abs=0.01)
    assert tip[1] == pytest.approx(-(20.0 + parts_catalog._MOTOR_SHAFT_STICKOUT), abs=0.05)


def test_a_body_with_no_cylinders_says_so():
    from app.cad_kernel.evaluator import brep  # noqa: F401 — exact kernel present
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    import numpy as np

    box = BRepPrimAPI_MakeBox(0.01, 0.01, 0.01).Shape()
    reading = interfaces.read_interfaces(box, np.zeros((1, 3)))
    assert reading["axis"] is None and "no cylindrical faces" in reading["note"]


def test_a_tap_drill_is_offered_as_both_screws_it_could_be():
    """2.5 mm is an M3 tap drill and an M2.5 thread modelled at nominal; the
    reader does not pick between them."""
    found = {(c["screw"], c["as"]) for c in interfaces.screw_candidates(2.5)}
    assert {("M3", "tapped"), ("M2.5", "nominal")} <= found

"""Purchased components carry their real mass (TOOL-01..05).

Every mass in this system was volume times density. That is right for a bracket
somebody modelled and wrong for everything they bought — a NEMA 17's envelope is
a solid steel block where the real motor is laminations, windings and air, so a
robot with four of them came out more than a kilogram heavy. And these are
exactly the components a vehicle's CG hangs on, so the budget was wrong in the
place it matters most with nothing on the report saying so.

The property under test throughout: **the number and the reason for it travel
together**, and `unknown` is a real answer rather than a confident zero.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import http_part
from app.services import (
    assembly_mass, assembly_service, bom, cad_operations,
    document_service as docs, part_mass, parts_catalog, standard_parts,
)
from main import app

client = TestClient(app)


def _part(size=(0.06, 0.04, 0.01)) -> str:
    return http_part(client, size, resolution=32)


# ---------------------------------------------------------------------------
# Which figure wins, and why
# ---------------------------------------------------------------------------

def test_a_modelled_part_reports_a_geometric_mass():
    doc_id = _part()
    cad_operations.execute("set_material", doc_id, {"material_id": "al-6061"})
    fact = part_mass.for_document(doc_id)
    assert fact.source == part_mass.GEOMETRIC
    assert "Aluminum" in fact.basis or "6061" in fact.basis
    assert fact.kg > 0


def test_a_catalog_part_reports_the_published_mass_not_its_envelope():
    """The finding this sprint exists for: a stepper's envelope is a solid steel
    block and the catalog has carried the real figure all along, unread."""
    entry = parts_catalog.by_key("stepper-nema17")
    assert entry is not None and entry.typical_mass_g

    doc_id = standard_parts.document_for_key("stepper-nema17", owner_id=None)
    fact = part_mass.for_document(doc_id)

    assert fact.source == part_mass.CATALOG
    assert fact.grams == pytest.approx(entry.typical_mass_g, rel=1e-6)
    # And it says how far from the geometry it is, which is the useful number:
    # the solid envelope is far heavier than the motor.
    assert fact.geometric_kg is not None
    assert fact.ratio_to_geometric < 1.0


def test_a_recorded_mass_beats_both():
    """Nothing beats a measurement of the actual object."""
    doc_id = standard_parts.document_for_key("stepper-nema17", owner_id=None)
    env = cad_operations.execute("set_part_mass", doc_id, {"grams": 287.5})
    assert env["ok"] is True, env.get("error")

    fact = part_mass.for_document(doc_id)
    assert fact.source == part_mass.OVERRIDE
    assert fact.grams == pytest.approx(287.5)


def test_clearing_a_recorded_mass_falls_back_to_the_next_source():
    doc_id = standard_parts.document_for_key("stepper-nema17", owner_id=None)
    cad_operations.execute("set_part_mass", doc_id, {"grams": 287.5})
    cad_operations.execute("set_part_mass", doc_id, {"grams": 0})
    assert part_mass.for_document(doc_id).source == part_mass.CATALOG


def test_a_recorded_mass_survives_a_reload():
    """`set_material` did not, because it left the persisting to the evaluate
    path and that path only writes when the mass moved."""
    doc_id = _part()
    cad_operations.execute("set_part_mass", doc_id, {"grams": 42.0})
    docs.DOCUMENTS.pop(doc_id, None)
    assert docs.get_document(doc_id).mass_override_g == 42.0


def test_a_part_with_no_material_and_no_body_is_unknown_not_zero():
    """A confident 0.0 disappears into a total; a stated reason does not."""
    resp = client.post("/api/documents", json={"name": "empty", "resolution": 32})
    fact = part_mass.for_document(resp.json()["id"])
    assert fact.source == part_mass.UNKNOWN
    assert fact.kg == 0.0
    assert "no solid geometry" in fact.basis


def test_a_supplier_row_whose_mass_came_from_its_own_geometry_is_not_catalog():
    """The import path fills a supplier row's `mass_g` from the body it just
    read. Reading that back as published data would report a correction of a
    figure against itself."""
    from app.services import supplier_parts

    row = supplier_parts.record(document_id="doc-x", owner_id=None,
                                designation="bracket", mass_g=12.0)
    assert row["mass_source"] == "geometric"

    declared = supplier_parts.record(document_id="doc-y", owner_id=None,
                                     designation="motor", mass_g=31.0,
                                     mass_source="declared")
    assert declared["mass_source"] == "declared"


# ---------------------------------------------------------------------------
# The BOM
# ---------------------------------------------------------------------------

def _assembly_with(part_ids) -> str:
    asm = assembly_service.create_assembly("mass test", owner_id=None)
    for part_id in part_ids:
        assembly_service.add_component(asm.id, part_id)
    return asm.id


def test_a_bom_line_says_where_its_unit_mass_came_from():
    printed = _part()
    cad_operations.execute("set_material", printed, {"material_id": "pla"})
    motor = standard_parts.document_for_key("stepper-nema17", owner_id=None)

    data = bom.bill_of_materials(_assembly_with([printed, motor]))
    by_source = {line["part_id"]: line["mass_source"] for line in data["lines"]}
    assert by_source[printed] == part_mass.GEOMETRIC
    assert by_source[motor] == part_mass.CATALOG
    # And a rollup, so one envelope-derived motor among forty printed parts is
    # not invisible in the total.
    assert data["mass"]["by_source"] == {"catalog": 1, "geometric": 1}


def test_the_bom_total_uses_the_published_mass():
    motor = standard_parts.document_for_key("stepper-nema17", owner_id=None)
    published = parts_catalog.by_key("stepper-nema17").typical_mass_g

    data = bom.bill_of_materials(_assembly_with([motor, motor]))
    assert data["lines"][0]["quantity"] == 2
    assert data["total_mass_g"] == pytest.approx(published * 2, rel=1e-4)


def test_material_cost_still_keys_off_the_geometry():
    """A purchased component's price has nothing to do with its material, and
    the stock a machined part is cut from is its geometric volume."""
    printed = _part()
    cad_operations.execute("set_material", printed, {"material_id": "al-6061"})
    line = bom.bill_of_materials(_assembly_with([printed]))["lines"][0]
    assert line["ext_material_cost_usd"] > 0


def test_a_bom_warns_when_a_part_contributes_no_known_mass():
    blank = client.post("/api/documents",
                        json={"name": "no body", "resolution": 32}).json()["id"]
    data = bom.bill_of_materials(_assembly_with([blank]))
    assert data["mass"]["by_source"] == {"unknown": 1}
    assert "lower bound" in data["mass"]["warning"]


# ---------------------------------------------------------------------------
# Assembly mass, CG and inertia (TOOL-05)
# ---------------------------------------------------------------------------

def test_assembly_mass_uses_the_published_figure_for_a_purchased_part():
    motor = standard_parts.document_for_key("stepper-nema17", owner_id=None)
    published = parts_catalog.by_key("stepper-nema17").typical_mass_g

    report = assembly_mass.assembly_mass_properties(_assembly_with([motor]))
    assert report["mass_g"] == pytest.approx(published, rel=1e-4)
    assert report["components"][0]["mass_source"] == part_mass.CATALOG
    assert report["mass"]["by_source"] == {"catalog": 1}


def test_the_inertia_tensor_is_scaled_with_the_mass_not_left_at_the_envelope():
    """The incoherent middle this avoids: a 350 g motor carrying a 669 g motor's
    rotational inertia. For a rigid body of fixed geometry the tensor is linear
    in density, so the correction is exactly the mass ratio.
    """
    motor = standard_parts.document_for_key("stepper-nema17", owner_id=None)
    asm = _assembly_with([motor])

    corrected = assembly_mass.assembly_mass_properties(asm)
    fact = part_mass.for_document(motor)
    ratio = fact.ratio_to_geometric
    assert ratio is not None and ratio < 1.0

    # Against the same assembly weighed geometrically: both the mass and the
    # tensor move by the ratio, together.
    from app.services import mass_properties

    geometric = mass_properties.mass_properties(motor)
    envelope_izz = geometric["inertia_kg_mm2"][2][2]
    # The component sits at the origin, so the assembly tensor about its CG is
    # the part's own, scaled.
    assert corrected["inertia_kg_mm2"][2][2] == pytest.approx(
        envelope_izz * ratio, rel=0.02)


def test_a_corrected_component_says_so_in_the_notes_and_on_its_row():
    motor = standard_parts.document_for_key("stepper-nema17", owner_id=None)
    report = assembly_mass.assembly_mass_properties(_assembly_with([motor]))
    row = report["components"][0]
    assert row["inertia_scale"] < 1.0
    assert row["geometric_mass_g"] > row["mass_g"]
    assert any("inertia tensor is scaled" in note for note in report["notes"])


def test_the_cg_moves_when_a_heavy_component_is_weighed_correctly():
    """The whole reason this matters for an RC or drone build.

    A printed plate at the origin and a motor out on an arm. Weighing the motor
    as a solid steel block puts the CG much further toward it than it belongs.
    """
    plate = _part((0.12, 0.04, 0.004))
    cad_operations.execute("set_material", plate, {"material_id": "pla"})
    motor = standard_parts.document_for_key("stepper-nema17", owner_id=None)

    from app.models.schemas import ComponentTransform, Vector3

    asm = assembly_service.create_assembly("cg test", owner_id=None)
    assembly_service.add_component(asm.id, plate)
    placed = assembly_service.add_component(asm.id, motor)
    assembly_service.update_component(
        asm.id, placed.components[-1].id,
        ComponentTransform(translate=Vector3(x=0.08, y=0.0, z=0.0)))

    corrected = assembly_mass.assembly_mass_properties(asm.id)["center_of_mass_mm"][0]

    # Now weigh the motor as its envelope, by recording that mass as an
    # override — the same arithmetic the old code did for every part.
    envelope_g = part_mass.for_document(motor).geometric_kg * 1000.0
    cad_operations.execute("set_part_mass", motor, {"grams": envelope_g})
    as_envelope = assembly_mass.assembly_mass_properties(asm.id)["center_of_mass_mm"][0]

    assert as_envelope > corrected, (
        "weighing the motor as a solid block should drag the CG toward it")


# ---------------------------------------------------------------------------
# TOOL-03 / TOOL-04: the RC components
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", [
    "servo-micro-9g", "servo-standard", "bldc-2205", "bldc-2807",
    "board-30x30-flight", "board-20x20-esc", "shaft-3-50",
])
def test_every_new_rc_entry_builds_a_body(key):
    entry = parts_catalog.by_key(key)
    assert entry is not None, key
    doc_id = standard_parts.document_for_key(key, owner_id=None)
    doc = docs.get_document(doc_id)
    assert doc.derived.error is None
    assert doc.derived.volume > 0


def test_a_form_factor_carries_a_mass_only_where_its_name_states_one():
    """A "9 g servo" is called that because of what it weighs. A 2205 motor's
    mass is set by the winding, and two of them differ by 40% — a figure there
    would be a guess with a decimal point on it."""
    assert parts_catalog.by_key("servo-micro-9g").typical_mass_g == 9.0
    assert parts_catalog.by_key("bldc-2205").typical_mass_g is None

    motor = standard_parts.document_for_key("bldc-2205", owner_id=None)
    fact = part_mass.for_document(motor)
    # It has a body, so the geometric figure is what there is — and the report
    # says that is what it is rather than implying it is the motor's mass.
    assert fact.source == part_mass.GEOMETRIC
    assert "record" in parts_catalog.by_key("bldc-2205").notes


def test_the_servo_case_matches_the_form_factor_it_names():
    size = parts_catalog.envelope_mm(parts_catalog.by_key("servo-micro-9g"))
    v = parts_catalog.SERVOS["micro-9g"]
    # The tabs are the widest thing on the case, so they set the long extent.
    assert size["x"] == pytest.approx(v.tab_span + 8.0, abs=0.1)
    assert size["y"] == pytest.approx(v.body_w, abs=0.1)


def test_a_bldc_envelope_is_built_from_its_stator_code():
    assert parts_catalog.BLDC["2205"].stator_dia == 22.0
    assert parts_catalog.BLDC["2205"].stator_h == 5.0
    assert parts_catalog.BLDC["2807"].stator_dia == 28.0


def test_a_battery_is_a_declared_envelope_with_its_electrical_record():
    """No capacity-to-size guess: pack dimensions are not standardised, so the
    dimensions and the mass are both stated by whoever has the pack."""
    doc_id = client.post("/api/documents",
                         json={"name": "pack", "resolution": 32}).json()["id"]
    env = cad_operations.execute("add_battery", doc_id, {
        "length": 0.105, "width": 0.034, "height": 0.024,
        "grams": 189.0, "cells": 3, "capacity_mah": 2200})
    assert env["ok"] is True, env.get("error")

    assert env["result"]["battery"]["nominal_v"] == pytest.approx(11.1)
    assert env["result"]["battery"]["capacity_mah"] == 2200
    # The declared mass is what every report uses, not the box of whatever.
    assert env["result"]["mass_source"] == part_mass.OVERRIDE
    assert part_mass.for_document(doc_id).grams == pytest.approx(189.0)


def test_a_battery_pack_keeps_its_record_across_a_reload():
    doc_id = client.post("/api/documents",
                         json={"name": "pack", "resolution": 32}).json()["id"]
    cad_operations.execute("add_battery", doc_id, {
        "length": 0.07, "width": 0.035, "height": 0.019,
        "grams": 96.0, "cells": 2, "chemistry": "lipo"})
    docs.DOCUMENTS.pop(doc_id, None)
    reloaded = docs.get_document(doc_id)
    assert reloaded.battery["cells"] == 2
    assert reloaded.battery["nominal_v"] == pytest.approx(7.4)
    assert reloaded.mass_override_g == 96.0


def test_a_battery_needs_its_dimensions_and_its_mass():
    doc_id = client.post("/api/documents",
                         json={"name": "pack", "resolution": 32}).json()["id"]
    env = cad_operations.execute("add_battery", doc_id,
                                 {"length": 0.07, "width": 0.035, "cells": 2})
    assert env["ok"] is False
    assert env["error"]["code"] == "MISSING_ARG"

"""A purchased part weighs what the catalog publishes, not what its solid does.

`parts_catalog.Entry.typical_mass_g` was declared, populated for the NEMA
steppers, documented as being there "so a BOM total is not quietly doubled" —
and read by nothing. Both roll-ups took the geometric mass of a solid envelope
instead, so a NEMA 17 reported 672 g against a real 350 g and every mass budget
containing a motor was nearly double.

Four things have to hold, and each is a different way this goes wrong again:

* **The roll-ups substitute.** BOM and `mass_budget` both, because a build is
  checked against one and flown against the other.
* **The part does not.** `mass_properties` measures that document's geometry;
  the moment it starts reporting a figure the body does not have, nothing in
  the system can be checked against anything else.
* **The substitution is visible.** `mass_source` on every row, with the
  modelled figure beside it. A number whose provenance is invisible is one
  nobody can check.
* **A plain modelled part is untouched.** The whole scheme keys off
  `catalog_ref`, so a part somebody drew has no published figure to prefer.
"""

import pytest
from fastapi.testclient import TestClient

from conftest import base_box
from main import app
from app.services import assembly_mass, assembly_service, bom, mass_properties
from app.services import cad_operations as ops
from app.services import document_service as docs
from app.services import parts_catalog as pc
from app.services import standard_parts

client = TestClient(app)

#: The NEMA table's published figure — the one the whole task is about.
NEMA17_G = 350.0


def _assembly(name="rig"):
    return assembly_service.create_assembly(name=name, owner_id="dev").id


def _stepper(asm_id, quantity=1):
    return ops.dispatch("insert_standard_part", asm_id,
                        {"key": "stepper-nema17", "quantity": quantity})


def _modelled_part(name="plate"):
    doc = docs.create_document(name=name, owner_id="dev")
    ops.dispatch("add_primitive", doc.id, {
        "shape": "box",
        "params": {"size": {"x": .1, "y": .1, "z": .01},
                   "center": {"x": 0, "y": 0, "z": 0}}})
    ops.dispatch("set_material", doc.id, {"material_id": "al-6061"})
    return doc.id


# --- the bug itself ----------------------------------------------------------

def test_the_bom_reports_the_published_mass_of_a_stepper():
    asm_id = _assembly()
    _stepper(asm_id)
    line = bom.bill_of_materials(asm_id)["lines"][0]
    assert line["unit_mass_g"] == pytest.approx(NEMA17_G, abs=0.01)
    assert line["mass_source"] == "published"
    # The solid envelope it replaced: steel through the frame, the pilot and
    # the shaft, which is where the 672 g came from.
    assert line["modelled_mass_g"] > 600


def test_the_mass_budget_reports_the_published_mass_of_a_stepper():
    asm_id = _assembly()
    _stepper(asm_id)
    report = assembly_mass.assembly_mass_properties(asm_id)
    assert report["mass_g"] == pytest.approx(NEMA17_G, abs=0.01)
    assert report["components"][0]["mass_source"] == "published"


def test_the_total_scales_with_quantity_not_with_the_envelope():
    """Four steppers is 1.4 kg, not 2.7 kg. The magnitude is the whole point."""
    asm_id = _assembly()
    _stepper(asm_id, quantity=4)
    data = bom.bill_of_materials(asm_id)
    assert data["line_count"] == 1 and data["lines"][0]["quantity"] == 4
    assert data["total_mass_g"] == pytest.approx(4 * NEMA17_G, abs=0.05)
    assert assembly_mass.assembly_mass_properties(asm_id)["mass_g"] == \
        pytest.approx(4 * NEMA17_G, abs=0.05)


# --- and what it must not touch ---------------------------------------------

def test_the_part_document_still_reports_its_own_geometry():
    """The substitution is a roll-up decision. `mass_properties` is a
    measurement of a body, and a measurement that starts answering a different
    question is worse than no measurement."""
    asm_id = _assembly()
    part_id = _stepper(asm_id)["part_id"]
    measured = mass_properties.mass_properties(part_id)["mass_kg"]
    assert measured > 0.6, measured


def test_a_modelled_part_is_unaffected_and_says_so():
    asm_id = _assembly()
    part_id = _modelled_part()
    ops.dispatch("insert_part", asm_id, {"part_id": part_id})
    line = bom.bill_of_materials(asm_id)["lines"][0]
    # 100x100x10 of 6061 — the mass is the geometry's and nothing swapped it.
    assert line["mass_source"] == "geometry"
    assert "modelled_mass_g" not in line
    assert line["unit_mass_g"] == pytest.approx(
        mass_properties.mass_properties(part_id)["mass_kg"] * 1000, rel=1e-6)
    row = assembly_mass.assembly_mass_properties(asm_id)["components"][0]
    assert row["mass_source"] == "geometry"


def test_a_screw_has_no_published_figure_and_keeps_its_own():
    """Only entries that declare one are substituted. A cap screw is solid
    steel and its geometry is the answer — substituting everything would need a
    mass for every one of 210 parts, which is 210 numbers to get wrong."""
    assert pc.by_key("iso4762-m4-16").typical_mass_g is None
    asm_id = _assembly()
    ops.dispatch("insert_standard_part", asm_id, {"key": "iso4762-m4-16"})
    line = bom.bill_of_materials(asm_id)["lines"][0]
    assert line["mass_source"] == "geometry"
    assert 2.0 < line["unit_mass_g"] < 3.2


# --- the tensor, and why it is left alone -----------------------------------

def test_the_inertia_tensor_is_not_scaled_and_the_report_says_so():
    """Scaling the tensor by published/geometric is the tempting correction and
    it is wrong: a motor's mass is in the stator and magnets, not spread through
    the bell, so the ratio is right for the mass and wrong for the second
    moment. It is reported as modelled, with a note."""
    asm_id = _assembly()
    _stepper(asm_id)
    report = assembly_mass.assembly_mass_properties(asm_id)
    ratio = NEMA17_G / report["components"][0]["modelled_mass_g"]

    # A single part at the origin: the assembly tensor is that part's own,
    # unscaled. Compare against the part's measured tensor directly.
    row = report["components"][0]
    part_id = bom.bill_of_materials(asm_id)["lines"][0]["part_id"]
    measured = mass_properties.mass_properties(part_id)["inertia_kg_mm2"]
    reported = report["inertia_kg_mm2"]
    assert reported[0][0] == pytest.approx(measured[0][0], rel=1e-3)
    # Which is emphatically *not* the scaled figure.
    assert reported[0][0] != pytest.approx(measured[0][0] * ratio, rel=1e-2)

    assert any("not scaled" in n for n in report["notes"]), report["notes"]
    assert row["mass_source"] == "published"


# --- the resolver itself -----------------------------------------------------

def test_a_supplier_import_is_never_substituted():
    """A supplier part's mass came from the body its manufacturer published, so
    the geometric figure *is* the published one and there is no second number
    to prefer. Substituting would be reading a mass off a row that got it from
    the geometry in the first place."""
    from app.services import supplier_parts

    assert standard_parts.published_mass_kg(f"{supplier_parts.PREFIX}whatever") is None


def test_an_unknown_or_absent_ref_resolves_to_nothing():
    assert standard_parts.published_mass_kg(None) is None
    assert standard_parts.published_mass_kg("") is None
    assert standard_parts.published_mass_kg("not-a-catalog-key") is None
    assert standard_parts.published_mass_kg("stepper-nema17") == pytest.approx(0.350)


def test_the_csv_carries_the_provenance():
    """The CSV is read by somebody who cannot ask where a figure came from."""
    asm_id = _assembly()
    _stepper(asm_id)
    text = bom.bom_csv(asm_id)
    assert "Mass Source" in text
    assert "published" in text

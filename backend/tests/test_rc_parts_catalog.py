"""The RC, drone and robotics half of the purchased-parts catalog.

Most of the time on a build of this kind goes on modelling *around* parts
somebody else made, and until now the catalog had a screw, a nut and a stepper.
The difficulty in extending it is that this domain has far fewer published
standards than the fastener one, so the rule the whole half runs on is:

> Table what is genuinely standardised or a settled de-facto convention.
> Parameterise what varies by manufacturer. Never invent a number to fill a
> table.

These tests are that rule, made checkable. The tabled dimensions are asserted
against the convention they claim (a 30.5 stack really is 30.5 between hole
centres; a 2207 really is a 22 mm stator); the parameterised ones are asserted
to *be* parameters rather than to have a particular value; and the last test is
the one that matters, because it is the only one that runs the whole chain — a
quadcopter's all-up weight, which is wrong by a factor of two the moment any
link in it breaks.
"""

import numpy as np
import pytest

from app.cad_kernel.core.feature_tree import FeatureTree, walk_nodes
from app.cad_kernel.geom.meshing import mesh_volume
from app.cad_kernel.kernel import Kernel
from app.services import assembly_mass, assembly_service, bom, materials
from app.services import cad_operations as ops
from app.services import parts_catalog as pc
from app.services import standard_parts


def _measure(key, params=None):
    """(bbox mm, volume mm³) of a configuration, actually evaluated."""
    entry = pc.by_key(key)
    solid = Kernel.evaluate_tree(
        FeatureTree.from_dict(pc.build_tree(entry, params)), resolution=96)
    mesh = Kernel.tessellate(solid)
    size = (mesh.vertices.max(axis=0) - mesh.vertices.min(axis=0)) * 1000
    return size, abs(mesh_volume(mesh.vertices, mesh.faces)) * 1e9


def _hole_centres(key, name="Mount hole", params=None):
    return {(round(n["params"]["center"]["x"] * 1000, 3),
             round(n["params"]["center"]["y"] * 1000, 3))
            for n in walk_nodes(pc.build_tree(pc.by_key(key), params))
            if n.get("name") == name}


# ============================================ what is tabled, and why it is ==

def test_the_stack_patterns_are_the_three_the_hobby_is_built_around():
    """30.5, 20 and 16 are conventions rather than standards with a number, but
    they are conventions with teeth: a 30.5 board does not fit a 20 stack."""
    assert pc.MOUNT_PATTERNS["fc-30.5"][:3] == (30.5, 30.5, "M3")
    assert pc.MOUNT_PATTERNS["fc-20"][:3] == (20.0, 20.0, "M2")
    assert pc.MOUNT_PATTERNS["fc-16"][:3] == (16.0, 16.0, "M2")


def test_a_flight_controller_is_drilled_on_its_pattern_at_the_iso_273_clearance():
    """The pattern is the tabled part and the part that has to be right. The
    hole size is not a second opinion — it comes from `clearance_hole`, which
    is the ISO 273 table the rest of the system drills to."""
    assert _hole_centres("fc-30.5") == {(15.25, 15.25), (15.25, -15.25),
                                        (-15.25, 15.25), (-15.25, -15.25)}
    assert _hole_centres("fc-16") == {(8.0, 8.0), (8.0, -8.0),
                                      (-8.0, 8.0), (-8.0, -8.0)}
    hole = next(n for n in walk_nodes(pc.build_tree(pc.by_key("fc-30.5")))
                if n.get("name") == "Mount hole")
    assert hole["params"]["radius"] * 2000 == pytest.approx(
        pc.clearance_hole("M3")["diameter_mm"], abs=1e-9)


def test_a_brushless_motor_is_sized_by_its_four_digit_stator_code():
    """`2207` is a 22 mm stator diameter and a 7 mm stator height. That is the
    one fixed thing about a brushless motor and it is what the row is keyed on;
    it is also why a 2207 from one maker replaces a 2207 from another."""
    entry = pc.by_key("bldc-2207")
    assert "22 mm stator diameter x 7 mm stator height" in entry.notes
    assert _hole_centres("bldc-2207") == {(8.0, 8.0), (8.0, -8.0),
                                          (-8.0, 8.0), (-8.0, -8.0)}


def test_the_servo_classes_are_the_dimensions_a_servo_tray_is_cut_to():
    """A standard servo has fitted the same pocket since the 1970s. The overall
    length across the ears is the figure a tray is cut to, so it is the one
    that has to come out of the body rather than out of an offset."""
    size, _ = _measure("servo-standard")
    assert size == pytest.approx([54.0, 20.0, 36.5], abs=0.1)
    assert _hole_centres("servo-standard", "Mounting hole") == {
        (24.75, 5.0), (24.75, -5.0), (-24.75, 5.0), (-24.75, -5.0)}
    # A micro servo has one hole per ear, not two. Drilling four would put a
    # hole where the plastic is.
    assert len(_hole_centres("servo-micro", "Mounting hole")) == 2
    micro, _ = _measure("servo-micro")
    assert micro == pytest.approx([32.2, 12.2, 28.5], abs=0.1)


def test_a_standoff_takes_its_hex_from_the_nut_table_and_not_a_second_one():
    """An M3 standoff is 5.5 mm across flats because an M3 nut is. Two tables
    of one number are two chances to be wrong about it."""
    size, _ = _measure("standoff-m3-20")
    assert size[1] == pytest.approx(pc.METRIC["M3"].nut_flats, abs=0.02)
    assert size[0] == pytest.approx(pc.METRIC["M3"].nut_flats * 2 / np.sqrt(3),
                                    abs=0.05)
    assert size[2] == pytest.approx(20.0, abs=0.02)


def test_carbon_stock_and_lipo_packs_are_parameterised_rather_than_tabled():
    """A LiPo is the one part here with nothing to table: two 4S 1300 packs
    from different makers differ by ten millimetres and forty grams, so a table
    keyed on capacity would be a table of invented numbers."""
    for key in ("lipo", "carbon-plate", "carbon-tube"):
        entry = pc.by_key(key)
        assert entry.params, key
    assert "not standardised" in pc.by_key("lipo").standard
    # And no row anywhere claims a cell count or a capacity, which is what a
    # tabled pack would have had to be keyed on.
    assert not [e for e in pc.catalog()
                if "mah" in e.key.lower() or "mAh" in e.designation]


# ================================== what is a parameter, and that it works ==

def test_a_parameter_actually_changes_the_body():
    size, _ = _measure("lipo", {"length_mm": 105, "width_mm": 34,
                                "height_mm": 25})
    assert size == pytest.approx([105.0, 34.0, 25.0], abs=0.2)
    tube, _ = _measure("carbon-tube", {"od_mm": 20, "id_mm": 18,
                                       "length_mm": 200})
    assert tube == pytest.approx([20.0, 200.0, 20.0], abs=0.3)


def test_a_parameter_the_row_does_not_declare_is_refused_not_dropped():
    """The same rule `additionalProperties: false` enforces everywhere else on
    this surface: a parameter silently ignored is a part built to a size nobody
    asked for and reported as a success."""
    with pytest.raises(ValueError, match="not among them"):
        pc.resolve_params(pc.by_key("lipo"), {"capacity_mah": 1300})


def test_an_explicit_zero_is_refused_rather_than_replaced_by_the_default():
    """`x or default` swallows a caller's zero. A zero-thickness plate is not a
    plate, and it must fail by name rather than quietly become a 2 mm one."""
    with pytest.raises(ValueError, match="thickness_mm"):
        pc.resolve_params(pc.by_key("carbon-plate"), {"thickness_mm": 0})
    assert pc.resolve_params(
        pc.by_key("carbon-plate"), {"thickness_mm": 3})["thickness_mm"] == 3.0


def test_a_pair_that_cannot_both_be_true_is_refused_by_the_builder():
    """A range check cannot see two parameters at once; a tube bored wider than
    itself is not a tube."""
    with pytest.raises(ValueError, match="smaller than"):
        pc.build_tree(pc.by_key("carbon-tube"), {"od_mm": 10, "id_mm": 12})


def test_the_default_configuration_keeps_the_bare_key_as_its_reference():
    """Which is what keeps every document stored before parameters existed
    resolvable, and what keeps eight identical packs one BOM line."""
    assert pc.make_ref("lipo") == "lipo"
    assert pc.make_ref("lipo", {"length_mm": 72}) == "lipo"      # the default
    assert pc.make_ref("lipo", {"length_mm": 90}) == "lipo?length_mm=90"
    assert pc.parse_ref("lipo?length_mm=90&mass_g=210") == (
        "lipo", {"length_mm": 90.0, "mass_g": 210.0})


def test_the_listing_publishes_the_parameters_a_row_takes():
    row = pc.describe(pc.by_key("bldc-2207"))
    assert set(row["params"]) == {"bell_dia_mm", "height_mm", "shaft_dia_mm",
                                  "mass_g"}
    assert row["params"]["bell_dia_mm"]["unit"] == "mm"
    assert row["params"]["mass_g"]["unit"] == "g"
    # A row with one configuration says nothing rather than an empty object.
    assert "params" not in pc.describe(pc.by_key("iso4762-m4-16"))


# ============================================= mass, which is the whole point ==

def test_every_powered_or_electronic_part_declares_a_published_mass():
    """A solid envelope's mass is meaningless for all of them, and the roll-ups
    read `typical_mass_g`. A row without one reports the envelope's weight."""
    powered = [e for e in pc.catalog()
               if e.category in ("motor", "electronics", "battery", "propeller")]
    assert powered
    for entry in powered:
        assert entry.typical_mass_g, entry.key
        assert entry.fidelity == "envelope", entry.key


def test_the_published_mass_is_a_parameter_where_it_can_be_known_exactly():
    """A typical mass for the class is not the mass of the part in your hand,
    and a LiPo has its weight printed on the side."""
    assert pc.published_mass_g("lipo") == 165.0
    assert pc.published_mass_g(pc.make_ref("lipo", {"mass_g": 210})) == 210.0
    assert standard_parts.published_mass_kg(
        pc.make_ref("lipo", {"mass_g": 210})) == pytest.approx(0.210)


def test_a_prop_is_the_disc_it_sweeps_and_therefore_needs_its_mass_declared():
    """Two blades frozen at one azimuth answer the clearance question only for
    that azimuth. The disc is the honest body; it also weighs ten times the
    propeller, which is why the mass is published rather than measured."""
    size, volume = _measure("prop-5in")
    assert size[0] == pytest.approx(5 * 25.4, abs=0.2)
    assert size[0] == pytest.approx(size[1], abs=0.05)   # a disc, not blades
    entry = pc.by_key("prop-5in")
    geometric = volume * 1e-9 * materials.density_of(entry.material) * 1000
    assert geometric > 10 * entry.typical_mass_g, geometric


# ========================================= the whole chain, end to end ======

def _quad():
    """4 motors, 4 props, an FC, a 4-in-1 ESC, a pack and a frame plate."""
    asm = assembly_service.create_assembly(name="5-inch quad", owner_id="dev")
    arm = 0.1145 * 0.7071   # a 5-inch quad's motor-to-centre, on the diagonal

    def place(key, quantity=1, params=None):
        return ops.dispatch("insert_standard_part", asm.id,
                            {"key": key, "quantity": quantity,
                             **({"params": params} if params else {})})

    for key, z in (("bldc-2207", 0.010), ("prop-5in", 0.045)):
        for i, cid in enumerate(place(key, 4)["component_ids"]):
            ops.dispatch("place_component", asm.id, {
                "component_id": cid,
                "translate": {"x": arm * (1 if i < 2 else -1),
                              "y": arm * (1 if i % 3 == 0 else -1), "z": z}})
    place("fc-30.5")
    place("esc-4in1-30.5")
    place("lipo")
    place("carbon-plate", 1, {"width_mm": 110, "length_mm": 130,
                              "thickness_mm": 4})
    return asm.id


def test_a_quadcopters_all_up_weight_lands_in_a_real_band():
    """The test worth having, because it is the only one that runs the whole
    chain: catalog row -> published mass -> part document -> BOM -> mass
    budget. Modelled as solid envelopes this build weighs 780 g; the parts on
    the bench weigh about 420 g, and a 5-inch quad with a 4S pack is a
    600-gramme aircraft once its arms, standoffs and camera are on it.
    """
    asm_id = _quad()
    budget = assembly_mass.assembly_mass_properties(asm_id)
    assert budget["counted"] == 12
    assert 350.0 < budget["mass_g"] < 550.0, budget["mass_g"]

    # The BOM agrees with it, line for line — two roll-ups of one number.
    data = bom.bill_of_materials(asm_id)
    assert data["total_mass_g"] == pytest.approx(budget["mass_g"], abs=0.5)

    # And the envelope masses it replaced really were about double, which is
    # what makes this a check rather than a coincidence.
    modelled = sum(ln.get("modelled_mass_g", ln["unit_mass_g"]) * ln["quantity"]
                   for ln in data["lines"])
    assert modelled > 1.7 * data["total_mass_g"], modelled


def test_four_identical_motors_are_one_bom_line_of_quantity_four():
    """`document_for` reuses one document per (reference, owner), and the
    reference is the bare key for a default configuration — so adding
    parameters did not turn four motors into four lines."""
    asm_id = _quad()
    data = bom.bill_of_materials(asm_id)
    motors = [ln for ln in data["lines"] if "2207" in ln["name"]]
    assert len(motors) == 1 and motors[0]["quantity"] == 4


def test_two_differently_configured_packs_are_two_parts():
    """And the converse: a pack of a different size is a different part, with
    its own line and its own name. One line reading 'LiPo pack' twice would be
    a purchasing document that cannot be ordered from."""
    asm = assembly_service.create_assembly(name="two packs", owner_id="dev")
    ops.dispatch("insert_standard_part", asm.id, {"key": "lipo"})
    ops.dispatch("insert_standard_part", asm.id,
                 {"key": "lipo", "params": {"length_mm": 105, "mass_g": 320}})
    lines = bom.bill_of_materials(asm.id)["lines"]
    assert len(lines) == 2
    assert {ln["unit_mass_g"] for ln in lines} == {165.0, 320.0}
    assert any("105" in ln["name"] for ln in lines)


def test_the_operation_refuses_a_parameter_the_row_does_not_take():
    asm = assembly_service.create_assembly(name="rig", owner_id="dev")
    result = ops.execute("insert_standard_part", asm.id,
                         {"key": "lipo", "params": {"capacity_mah": 1300}})
    assert not result["ok"]
    assert result["error"]["code"] == "BAD_ARG"
    assert result["error"]["arg"] == "params"


def test_the_listing_offers_the_new_families():
    rows = standard_parts.listing(owner_id="dev", limit=250)
    keys = {r["key"] for r in rows["parts"]}
    for expected in ("bldc-2207", "fc-30.5", "esc-4in1-30.5", "servo-standard",
                     "standoff-m3-20", "prop-5in", "carbon-plate",
                     "carbon-tube", "lipo"):
        assert expected in keys or expected in {
            r["key"] for r in standard_parts.listing(
                owner_id="dev", search=expected, limit=250)["parts"]}, expected
    assert {"motor", "electronics", "battery", "propeller", "stock",
            "hardware"} <= set(rows["categories"])

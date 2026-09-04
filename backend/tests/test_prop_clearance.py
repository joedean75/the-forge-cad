"""Rotor clearance — the check this domain has that general CAD does not.

A propeller's useful geometry is not its shape. Two blades frozen at one
azimuth clash or clear depending on where the modeller left them, which is not
a property of the aircraft, so `check_interference` answers the wrong question
about a rotor. `prop_clearance` reduces every rotor to the **disc it sweeps** —
tip radius measured across the body, whatever the body is — and then asks the
three questions a multirotor is built against.

The load-bearing test is `test_a_bladed_model_reads_the_same_as_a_swept_disc`:
two bladed props that miss each other at their modelled azimuth and whose discs
plainly overlap. General interference says clear. If this check ever agrees with
it, the operation has stopped being worth having.
"""

import math

import pytest

from app.services import assembly_service, interference
from app.services import cad_operations as ops
from app.services import document_service as docs

V = lambda x, y, z: {"x": x, "y": y, "z": z}  # noqa: E731

PROP_MM = 5 * 25.4  # a 5-inch prop, 127 mm across


def _assembly(name="quad"):
    return assembly_service.create_assembly(name=name, owner_id="dev").id


def _place(asm_id, component_id, at, rotate=None):
    ops.dispatch("place_component", asm_id, {
        "component_id": component_id, "translate": V(*at),
        **({"rotate": V(*rotate)} if rotate else {})})


def _quad(arm_mm, z=0.045, key="prop-5in"):
    """Four rotors on a square, evenly spaced, at `arm_mm` from the centre."""
    asm_id = _assembly()
    arm = arm_mm / 1000.0 * math.sin(math.pi / 4)
    ids = ops.dispatch("insert_standard_part", asm_id,
                       {"key": key, "quantity": 4})["component_ids"]
    for i, cid in enumerate(ids):
        _place(asm_id, cid, (arm * (1 if i < 2 else -1),
                             arm * (1 if i % 3 == 0 else -1), z))
    return asm_id, ids


def _bar(length_mm=PROP_MM, width_mm=12.0, thick_mm=4.0, name="bladed prop"):
    """A prop somebody modelled as blades: a bar as long as the disc is wide."""
    doc = docs.create_document(name=name, owner_id="dev", resolution=48)
    ops.dispatch("add_primitive", doc.id, {
        "shape": "box", "params": {
            "size": V(length_mm / 1000, width_mm / 1000, thick_mm / 1000),
            "center": V(0, 0, 0)}})
    return doc.id


def _box(size_mm, name="part"):
    doc = docs.create_document(name=name, owner_id="dev", resolution=48)
    ops.dispatch("add_primitive", doc.id, {
        "shape": "box", "params": {
            "size": V(*[s / 1000 for s in size_mm]), "center": V(0, 0, 0)}})
    return doc.id


# ================================================ the disc, and where it is ==

def test_a_rotor_is_measured_as_the_disc_it_sweeps():
    asm_id, _ = _quad(114.5)
    report = ops.dispatch("prop_clearance", asm_id, {})
    assert len(report["props"]) == 4
    for prop in report["props"]:
        assert prop["diameter_mm"] == pytest.approx(PROP_MM, abs=0.2)
        assert prop["axis"] == pytest.approx([0.0, 0.0, 1.0], abs=1e-6)


def test_the_tip_gap_is_the_closed_form_for_four_evenly_spaced_rotors():
    """`2 R sin(pi/N) - D`, and nothing about the check may disagree with it."""
    arm = 114.5
    asm_id, _ = _quad(arm)
    report = ops.dispatch("prop_clearance", asm_id, {})
    adjacent = 2 * arm * math.sin(math.pi / 4) - PROP_MM
    across = 2 * arm - PROP_MM
    gaps = sorted(round(p["gap_mm"], 2) for p in report["pairs"])
    assert gaps[:4] == [pytest.approx(adjacent, abs=0.2)] * 4
    assert gaps[4:] == [pytest.approx(across, abs=0.2)] * 2
    assert report["clear"]


def test_short_arms_are_a_strike_and_the_overlap_is_the_dimension_to_change():
    """The true signed distance between two overlapping coplanar discs is to
    the nearest *flat face* — 4 mm, which is the blade's own thickness and a
    number about nothing. What gets reported is the radial overlap, because the
    arm is what you would change."""
    arm = 80.0
    asm_id, _ = _quad(arm)
    report = ops.dispatch("prop_clearance", asm_id, {})
    assert not report["clear"] and report["interferences"] == 4
    worst = report["pairs"][0]
    assert worst["status"] == "interference"
    assert worst["gap_mm"] == pytest.approx(
        2 * arm * math.sin(math.pi / 4) - PROP_MM, abs=0.2)
    assert worst["gap_mm"] == worst["plan_gap_mm"]
    assert not worst["stacked"]


def test_the_arm_the_layout_asks_for_is_the_arm_that_clears_it():
    """The design aid has to agree with the measurement, or one of them is
    decoration. Re-place at the arm it names and the strike is gone."""
    asm_id, ids = _quad(80.0)
    needed = ops.dispatch("prop_clearance", asm_id, {})["layout"]["min_arm_mm"]
    arm = needed / 1000.0 * math.sin(math.pi / 4)
    for i, cid in enumerate(ids):
        _place(asm_id, cid, (arm * (1 if i < 2 else -1),
                             arm * (1 if i % 3 == 0 else -1), 0.045))
    after = ops.dispatch("prop_clearance", asm_id, {})
    assert after["interferences"] == 0
    # And it is *exactly* the arm — the tip gap lands on the clearance asked for.
    assert min(p["gap_mm"] for p in after["pairs"]) == pytest.approx(5.0, abs=0.1)


def test_the_closed_form_and_the_measurement_agree_about_the_biggest_prop():
    asm_id, _ = _quad(114.5)
    layout = ops.dispatch("prop_clearance", asm_id, {})["layout"]
    assert layout["rotors"] == 4
    assert layout["mean_arm_mm"] == pytest.approx(114.5, abs=0.1)
    assert layout["max_diameter_mm"] == pytest.approx(
        2 * 114.5 * math.sin(math.pi / 4) - 5.0, abs=0.2)
    assert "evenly spaced" in layout["assumes"]


# ============================================ the reason it is not check_interference ==

def test_a_bladed_model_reads_the_same_as_a_swept_disc():
    """**The test the operation exists for.** Two bladed props, one turned a
    quarter turn, whose bodies miss each other entirely at the azimuth they
    happen to be modelled at — and whose discs overlap by 17 mm. General
    interference says clear, because at that azimuth they are. The aircraft
    still eats its own propellers."""
    asm_id = _assembly("bladed")
    part = _bar()
    a = ops.dispatch("insert_part", asm_id, {"part_id": part})["component_id"]
    b = ops.dispatch("insert_part", asm_id, {"part_id": part})["component_id"]
    _place(asm_id, a, (0.0, 0.0, 0.0))
    _place(asm_id, b, (0.110, 0.0, 0.0), rotate=(0, 0, 90))

    assert ops.dispatch("check_interference", asm_id, {})["clear"], \
        "the modelled bodies really do miss each other at this azimuth"

    report = ops.dispatch("prop_clearance", asm_id, {"props": [a, b]})
    assert report["declared"]
    assert report["props"][0]["diameter_mm"] == pytest.approx(PROP_MM, abs=0.1)
    assert report["interferences"] == 1
    assert report["pairs"][0]["gap_mm"] == pytest.approx(110 - PROP_MM, abs=0.2)


def test_stacked_coaxial_rotors_are_not_a_clash():
    """An X8 has its discs fully overlapped in plan by design. Reporting that
    as a failure trains a reader to ignore the check."""
    asm_id = _assembly("x8")
    ids = ops.dispatch("insert_standard_part", asm_id,
                       {"key": "prop-5in", "quantity": 2})["component_ids"]
    _place(asm_id, ids[0], (0.0, 0.0, 0.060))
    _place(asm_id, ids[1], (0.0, 0.0, 0.0))
    report = ops.dispatch("prop_clearance", asm_id, {})
    pair = report["pairs"][0]
    assert pair["coaxial"] and pair["stacked"]
    assert pair["plan_gap_mm"] == pytest.approx(-PROP_MM, abs=0.2)
    assert pair["axial_gap_mm"] == pytest.approx(60 - 8, abs=0.3)
    assert pair["status"] == "clear" and report["clear"]


def test_a_stack_too_tight_is_still_reported():
    """Not flagging an X8 is not the same as never flagging one."""
    asm_id = _assembly("tight x8")
    ids = ops.dispatch("insert_standard_part", asm_id,
                       {"key": "prop-5in", "quantity": 2})["component_ids"]
    _place(asm_id, ids[0], (0.0, 0.0, 0.010))
    _place(asm_id, ids[1], (0.0, 0.0, 0.0))
    report = ops.dispatch("prop_clearance", asm_id, {})
    assert report["pairs"][0]["stacked"]
    assert report["pairs"][0]["status"] == "clearance"
    assert not report["clear"]


# ================================================= anything standing in one ==

def test_a_mast_in_the_disc_is_found_and_an_arm_under_it_is_not():
    """The swept volume is the disc, not a column. Something under the rotor is
    not in its way, and saying it is would make the report unreadable."""
    asm_id = _assembly("frame")
    prop = ops.dispatch("insert_standard_part", asm_id,
                        {"key": "prop-5in"})["component_ids"][0]
    _place(asm_id, prop, (0.0, 0.0, 0.045))

    mast = ops.dispatch("insert_part", asm_id,
                        {"part_id": _box((6, 6, 60), "antenna")})["component_id"]
    _place(asm_id, mast, (0.040, 0.0, 0.030))     # reaches up through the disc
    arm = ops.dispatch("insert_part", asm_id,
                       {"part_id": _box((120, 12, 4), "arm")})["component_id"]
    _place(asm_id, arm, (0.0, 0.0, 0.010))        # passes well under it

    report = ops.dispatch("prop_clearance", asm_id, {})
    hits = {o["obstruction"] for o in report["obstructions"]}
    assert "antenna" in hits and "arm" not in hits
    struck = next(o for o in report["obstructions"] if o["obstruction"] == "antenna")
    assert struck["status"] == "interference"
    # Where on the disc it reaches — a hit at the hub is the shaft it is bolted
    # to and one at the tip is a strike, and a depth cannot tell them apart.
    assert struck["at_radius_mm"] == pytest.approx(40, abs=5)


def test_a_rotor_on_its_own_motor_is_not_four_warnings():
    """The hub does not sweep: it is bolted to a shaft, and that shaft reaching
    into it is what a shaft is. Left as a full disc, every correctly built
    quadcopter reported four obstructions reading "your propeller is mounted on
    its motor" — and a check that flags the normal case is one people learn to
    ignore."""
    asm_id = _assembly("motor and prop")
    arm = 0.1145 * math.sin(math.pi / 4)
    motors = ops.dispatch("insert_standard_part", asm_id,
                          {"key": "bldc-2207", "quantity": 4})["component_ids"]
    props = ops.dispatch("insert_standard_part", asm_id,
                         {"key": "prop-5in", "quantity": 4})["component_ids"]
    for i in range(4):
        at = (arm * (1 if i < 2 else -1), arm * (1 if i % 3 == 0 else -1))
        _place(asm_id, motors[i], (*at, 0.010))
        _place(asm_id, props[i], (*at, 0.045))

    report = ops.dispatch("prop_clearance", asm_id, {})
    assert report["obstructions"] == []
    assert report["clear"]
    # The hub it used is the catalog's, and it says so rather than leaving a
    # suppressed reading invisible.
    assert report["props"][0]["hub_mm"] == pytest.approx(10.0)


def test_something_out_on_the_disc_is_still_a_warning():
    """Not flagging the mount is not the same as never flagging anything: the
    band is dropped where the mount has to be, and nowhere else."""
    asm_id = _assembly("camera")
    prop = ops.dispatch("insert_standard_part", asm_id,
                        {"key": "prop-5in"})["component_ids"][0]
    _place(asm_id, prop, (0.0, 0.0, 0.045))
    cam = ops.dispatch("insert_part", asm_id,
                       {"part_id": _box((14, 14, 14), "camera")})["component_id"]
    _place(asm_id, cam, (0.030, 0.0, 0.030))       # under the disc, out at r=30
    report = ops.dispatch("prop_clearance", asm_id, {})
    hit = next(o for o in report["obstructions"] if o["obstruction"] == "camera")
    assert hit["status"] == "clearance"
    assert hit["at_radius_mm"] > 10.0


def test_a_declared_rotor_takes_the_hub_it_is_given():
    """A rotor somebody modelled has no catalog row to declare a hub, so the
    argument is the only way to say where the mount stops."""
    asm_id = _assembly("declared hub")
    part = _bar()
    a = ops.dispatch("insert_part", asm_id, {"part_id": part})["component_id"]
    _place(asm_id, a, (0.0, 0.0, 0.0))
    assert ops.dispatch("prop_clearance", asm_id,
                        {"props": [a]})["props"][0]["hub_mm"] == 0.0
    assert ops.dispatch("prop_clearance", asm_id,
                        {"props": [a], "hub_mm": 12.0})["props"][0]["hub_mm"] == 12.0


# ================================================================= the surface ==

def test_an_assembly_with_no_rotor_refuses_rather_than_reporting_clear():
    """A clearance report that means "I found nothing to check" is the one
    thing this must never be mistaken for."""
    asm_id = _assembly("no rotors")
    ops.dispatch("insert_part", asm_id, {"part_id": _box((50, 50, 5))})
    result = ops.execute("prop_clearance", asm_id, {})
    assert not result["ok"]
    assert result["error"]["code"] == "PRECONDITION"
    assert "props" in result["error"]["message"]


def test_rotors_are_found_by_catalog_category_without_being_named():
    asm_id, _ = _quad(114.5)
    ops.dispatch("insert_part", asm_id, {"part_id": _box((50, 50, 5), "plate")})
    report = ops.dispatch("prop_clearance", asm_id, {})
    assert len(report["props"]) == 4 and not report["declared"]
    assert all("propeller" in p["name"] for p in report["props"])


def test_props_must_be_a_list():
    asm_id, _ = _quad(114.5)
    result = ops.execute("prop_clearance", asm_id, {"props": "c1"})
    assert not result["ok"]


def test_the_threshold_is_a_warning_band_not_a_rule():
    asm_id, _ = _quad(95.0)          # 7.4 mm of tip gap
    assert ops.dispatch("prop_clearance", asm_id,
                        {"clearance_mm": 5.0})["clear"]
    tight = ops.dispatch("prop_clearance", asm_id, {"clearance_mm": 15.0})
    assert not tight["clear"] and tight["interferences"] == 0
    assert tight["clearance_warnings"] == 4


def test_a_tilted_rotor_falls_back_to_the_true_distance():
    """No single dimension is the one to change when the axes are not parallel,
    so the pair is measured in 3D rather than resolved into plan and axis."""
    asm_id = _assembly("tiltrotor")
    ids = ops.dispatch("insert_standard_part", asm_id,
                       {"key": "prop-5in", "quantity": 2})["component_ids"]
    _place(asm_id, ids[0], (0.0, 0.0, 0.0))
    _place(asm_id, ids[1], (0.200, 0.0, 0.0), rotate=(0, 45, 0))
    pair = ops.dispatch("prop_clearance", asm_id, {})["pairs"][0]
    assert not pair["parallel"]
    assert "plan_gap_mm" not in pair
    assert pair["gap_mm"] > 0 and pair["status"] == "clear"


def test_the_service_reports_the_same_thing_the_operation_does():
    asm_id, _ = _quad(114.5)
    direct = interference.check_prop_clearance(asm_id)
    assert direct["clear"] and len(direct["props"]) == 4

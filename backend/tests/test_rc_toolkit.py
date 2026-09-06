"""The 1/16 RC toolkit: templates, presets, and the numbers behind them.

TOOL-06/07/09/10. The templates are covered generically by `test_templates`
(they build, their parameters drive, the literal and the expression agree) —
what is here is what is specific to *these* parts:

* the presets say whether a number is a standard or a band, because those are
  different kinds of wrong to change;
* the geometry helpers are arithmetic somebody would otherwise do by eye, so
  they are checked against the closed forms rather than against themselves;
* a knuckle re-aims its own steering arm when the wheelbase moves, which is the
  entire reason the Ackermann relation lives in the parameter binding;
* every template exports and draws, because a template nobody can hand to a shop
  is a picture.
"""

import math

import pytest
from fastapi.testclient import TestClient

from main import app
from app.services import drawing, rc_presets, templates

client = TestClient(app)

RC_KEYS = sorted(k for k in templates.TEMPLATES if k.startswith("rc_"))


def _new(name="RC test") -> str:
    return client.post("/api/documents", json={"name": name}).json()["id"]


def _op(doc_id, op, **args):
    resp = client.post(f"/api/documents/{doc_id}/op?envelope=1",
                       json={"op": op, "args": args})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _built(key) -> str:
    doc_id = _new(key)
    env = _op(doc_id, "apply_template", template=key)
    assert env["ok"], env
    return doc_id


def _bbox_mm(doc_id):
    size = _op(doc_id, "bounding_box")["result"]["size_mm"]
    return (size["x"], size["y"], size["z"])


def _params(key, **overrides):
    out = []
    for p in templates.TEMPLATES[key].parameters:
        p = dict(p)
        if p["name"] in overrides:
            p["expr"] = str(overrides[p["name"]])
        out.append(p)
    return out


# ------------------------------------------------------------------ presets --

def test_every_preset_row_says_whether_it_is_a_standard_or_a_band():
    """The distinction is the point. Changing a band value is a design decision;
    changing a standard value is a mistake, and a caller cannot tell which it is
    doing unless the row says."""
    for group, rows in rc_presets.GROUPS.items():
        for row in rows:
            assert row["kind"] in (rc_presets.STANDARD, rc_presets.BAND), (group, row)
            assert row["note"], f"{group}.{row['name']} says nothing about itself"


def test_a_band_states_its_range_and_a_standard_states_its_source():
    for rows in rc_presets.GROUPS.values():
        for row in rows:
            if row["kind"] == rc_presets.BAND:
                assert row["low"] < row["typical"] < row["high"], row
            else:
                assert row["standard"], row


def test_the_bearings_the_presets_name_are_parts_the_catalog_can_build():
    """A preset pointing at a part that does not exist is a dead end at the one
    moment somebody is trying to place it."""
    from app.services import parts_catalog

    for row in rc_presets.GROUPS["bearing"]:
        entry = parts_catalog.by_key(f"bearing-{str(row['value']).lower()}")
        assert entry is not None, row["value"]
        envelope = parts_catalog.envelope_mm(entry)
        assert envelope["z"] > 0


def test_the_servo_forms_the_presets_name_are_forms_the_catalog_has():
    from app.services import parts_catalog

    for row in rc_presets.GROUPS["servo"]:
        assert row["value"] in parts_catalog.SERVOS, row


def test_the_presets_are_reachable_as_an_operation_and_can_be_narrowed():
    doc_id = _new()
    every = _op(doc_id, "list_rc_presets")["result"]
    assert set(every["groups"]) == set(rc_presets.groups())

    one = _op(doc_id, "list_rc_presets", group="gear")["result"]
    assert set(one["groups"]) == {"gear"}


def test_an_unknown_preset_group_is_named_and_the_real_ones_listed():
    env = _op(_new(), "list_rc_presets", group="suspension")
    assert not env["ok"]
    assert env["error"]["code"] == "BAD_ARG"
    assert "chassis" in env["error"]["message"]


# ------------------------------------------------------------ the arithmetic --

def test_the_ackermann_arm_points_at_the_centre_of_the_rear_axle():
    """The guide construction, checked against its own definition rather than
    against a remembered number: the arm's angle is the angle of the line from
    the kingpin to the rear axle centre."""
    out = rc_presets.ackermann(250.0, 180.0, 22.0)
    expected = math.degrees(math.atan2(90.0, 250.0))
    assert out["arm_angle_deg"] == pytest.approx(expected, abs=1e-3)
    # And the components are that angle resolved onto the arm.
    assert out["arm_back_mm"] == pytest.approx(22.0 * math.cos(math.radians(expected)), abs=1e-3)
    assert out["arm_in_mm"] == pytest.approx(22.0 * math.sin(math.radians(expected)), abs=1e-3)
    assert out["tie_rod_mm"] == pytest.approx(180.0 - 2 * out["arm_in_mm"], abs=1e-3)


def test_a_longer_wheelbase_straightens_the_steering_arm():
    """The relation, not a number: a car with the axles further apart wants less
    difference between the inner and outer wheel, so the arm swings less inboard."""
    short = rc_presets.ackermann(200.0, 180.0, 22.0)
    long = rc_presets.ackermann(320.0, 180.0, 22.0)
    assert long["arm_angle_deg"] < short["arm_angle_deg"]
    assert long["tie_rod_mm"] > short["tie_rod_mm"]


def test_an_arm_that_reaches_past_the_centreline_is_refused():
    # A 50 mm arm on a 60 mm kingpin track with the axles 30 mm apart: the two
    # arms cross the centreline and each other, so there is no tie rod to build.
    with pytest.raises(ValueError) as exc:
        rc_presets.ackermann(30.0, 60.0, 50.0)
    assert "shorten the arm" in str(exc.value)


def test_the_centre_distance_is_the_one_the_tooth_counts_demand():
    out = rc_presets.spur_pinion(0.6, 16, 64)
    assert out["center_distance_mm"] == pytest.approx(0.6 * (16 + 64) / 2)
    assert out["pinion_pitch_dia_mm"] == pytest.approx(9.6)
    assert out["spur_pitch_dia_mm"] == pytest.approx(38.4)
    assert out["reduction"] == pytest.approx(4.0)


def test_a_small_pinion_is_warned_about_and_a_large_one_is_not():
    """17 teeth is where a standard 20-degree involute starts undercutting. RC
    pinions are routinely below it, which is a reason to say so, not to be quiet."""
    assert rc_presets.spur_pinion(0.6, 16, 64)["warnings"]
    assert not rc_presets.spur_pinion(0.6, 20, 64)["warnings"]


def test_the_geometry_helpers_are_reachable_as_operations():
    doc_id = _new()
    steering = _op(doc_id, "steering_geometry",
                   wheelbase=250.0, kingpin_track=180.0, arm=22.0)
    assert steering["ok"], steering
    assert steering["result"]["tie_rod_mm"] > 0

    gears = _op(doc_id, "gear_layout", module=0.6, pinion_teeth=16, spur_teeth=64)
    assert gears["ok"], gears
    assert gears["result"]["center_distance_mm"] == pytest.approx(24.0)


def test_an_impossible_steering_layout_is_a_typed_refusal_not_a_traceback():
    env = _op(_new(), "steering_geometry",
              wheelbase=30.0, kingpin_track=60.0, arm=50.0)
    assert not env["ok"]
    assert env["error"]["code"] == "BAD_ARG"


# -------------------------------------------------------------- the templates --

@pytest.mark.parametrize("key", RC_KEYS)
def test_every_rc_template_records_how_it_moves(key):
    """The drivetrain templates are operation sequences, so 'this shaft turns
    clockwise about X' is a step like any other — which is why a drawing of one
    comes out annotated without anybody typing a note."""
    doc_id = _built(key)
    notes = _op(doc_id, "list_mechanism")["result"]["notes"]
    assert notes, f"{key} says nothing about how it moves"
    assert all(n["prints_as"] for n in notes)


def test_the_axle_is_bored_through_and_pocketed_at_each_end():
    doc_id = _built("rc_axle")
    x, y, z = _bbox_mm(doc_id)
    assert (round(x), round(y), round(z)) == (120, 14, 14)
    # A tube, not a bar: a solid 14 mm rod of this length would be ~18.5 cm³.
    volume_cm3 = _op(doc_id, "mass_properties")["result"]["volume_cm3"]
    assert volume_cm3 < 0.75 * math.pi * 0.7 ** 2 * 12.0


def test_lengthening_the_axle_carries_its_bearing_pockets_with_it():
    """The failure a template exists to prevent: a pocket bound to a literal
    stays where it was while the tube grows past it."""
    doc_id = _built("rc_axle")
    assert _op(doc_id, "set_parameters",
               parameters=_params("rc_axle", axle_span=160))["ok"]
    assert round(_bbox_mm(doc_id)[0]) == 160

    from app.cad_kernel.core.feature_tree import walk_nodes
    from app.services import document_service

    resolved = document_service.resolved_tree(document_service.get_document(doc_id))
    pockets = [n for n in walk_nodes(resolved)
               if n.get("type") == "CylinderFeature"
               and abs(n["params"].get("height", 0) - 0.004) < 1e-9]
    xs = sorted(round(n["params"]["center"]["x"] * 1000, 3) for n in pockets)
    assert xs == [-78.0, 78.0]          # 160/2 - 4/2 at each end


def test_the_gearbox_plate_follows_its_tooth_counts():
    """The plate spans from outside the pinion's tips to outside the spur's, so
    a bigger spur moves the plate edge rather than hanging over air."""
    doc_id = _built("rc_spur_pinion")
    before = _bbox_mm(doc_id)
    layout = rc_presets.spur_pinion(0.6, 16, 64)
    # length = pinion tip radius + centre distance + spur tip radius + 2 margins
    expected = (layout["pinion_outside_dia_mm"] / 2 + layout["center_distance_mm"]
                + layout["spur_outside_dia_mm"] / 2 + 2 * 8.0)
    assert before[0] == pytest.approx(expected, abs=1e-3)

    assert _op(doc_id, "set_parameters",
               parameters=_params("rc_spur_pinion", spur_teeth=80))["ok"]
    grown = rc_presets.spur_pinion(0.6, 16, 80)
    after = _bbox_mm(doc_id)
    assert after[0] == pytest.approx(
        grown["pinion_outside_dia_mm"] / 2 + grown["center_distance_mm"]
        + grown["spur_outside_dia_mm"] / 2 + 16.0, abs=1e-3)


def test_the_layshaft_bore_sits_at_the_centre_distance():
    doc_id = _built("rc_spur_pinion")
    from app.cad_kernel.core.feature_tree import walk_nodes
    from app.services import document_service

    resolved = document_service.resolved_tree(document_service.get_document(doc_id))
    bores = [n for n in walk_nodes(resolved) if n.get("type") == "CylinderFeature"]
    centres = sorted(round(n["params"]["center"]["x"] * 1000, 4) for n in bores)
    assert centres == [0.0, 24.0]


def test_the_knuckle_re_aims_its_steering_arm_when_the_wheelbase_moves():
    """The Ackermann relation is in the parameter binding, not baked into a
    number, so the arm follows the car it is on. An arm that kept the angle it
    was born with is the silent failure this is guarding."""
    doc_id = _built("rc_knuckle")

    from app.cad_kernel.core.feature_tree import walk_nodes
    from app.services import document_service

    def ball():
        resolved = document_service.resolved_tree(
            document_service.get_document(doc_id))
        sweep = next(n for n in walk_nodes(resolved) if n.get("type") == "SweepFeature")
        return [v * 1000.0 for v in sweep["params"]["path"][1][:2]]

    guide = rc_presets.ackermann(250.0, 180.0, 22.0)
    back, inward = ball()
    assert back == pytest.approx(-guide["arm_back_mm"], abs=1e-3)
    assert inward == pytest.approx(guide["arm_in_mm"], abs=1e-3)

    assert _op(doc_id, "set_parameters",
               parameters=_params("rc_knuckle", wheelbase=320))["ok"]
    longer = rc_presets.ackermann(320.0, 180.0, 22.0)
    back, inward = ball()
    assert back == pytest.approx(-longer["arm_back_mm"], abs=1e-3)
    assert inward == pytest.approx(longer["arm_in_mm"], abs=1e-3)
    assert inward < guide["arm_in_mm"], "the arm did not straighten"


def test_the_servo_tray_is_cut_for_the_case_and_relieved_for_the_horn():
    doc_id = _built("rc_servo_tray")
    x, y, z = _bbox_mm(doc_id)
    assert (round(x), round(y), round(z)) == (40, 24, 3)
    volume_mm3 = _op(doc_id, "mass_properties")["result"]["volume_cm3"] * 1000.0
    assert volume_mm3 < 0.7 * 40 * 24 * 3, "nothing was cut out"
    assert volume_mm3 > 0.2 * 40 * 24 * 3, "there is no tray left"


def test_the_envelopes_are_envelopes_and_say_so():
    """A dogbone and a u-joint are modelled as the volume the real part sweeps,
    which is a different claim from 'this is the part' and is made in the
    summary rather than left for somebody to infer from the shape."""
    for key in ("rc_dogbone", "rc_ujoint"):
        assert "envelope" in templates.TEMPLATES[key].summary.lower()
        assert "envelope" in templates.TEMPLATES[key].tags


# ---------------------------------------------------------- the handoff (TOOL-10) --

@pytest.mark.parametrize("key", RC_KEYS)
def test_every_rc_template_exports_a_solid_and_draws_a_sheet(key):
    """A template nobody can hand to a shop is a picture. STEP is the body the
    next tool opens, STL is what a printer slices, and the drawing is what a
    person reads — and each one is a resource keyed by its content hash, so
    fetching it back is part of the check."""
    doc_id = _built(key)

    for fmt in ("step", "stl"):
        env = _op(doc_id, "export", format=fmt)
        assert env["ok"], (key, fmt, env.get("error"))
        fetched = client.get(env["result"]["url"])
        assert fetched.status_code == 200
        assert len(fetched.content) == env["result"]["bytes"] > 0

    notes = _op(doc_id, "list_mechanism")["result"]["notes"]
    sheet = drawing.generate_drawing(doc_id, section=False, spec={
        "views": ["front", "top"],
        "annotations": [{"view": "top", "at": [0, 0], "mechanism": n["id"],
                         "dy": -10 - 6 * i}
                        for i, n in enumerate(notes)]})
    assert sheet["annotations"] == len(notes)
    for note in notes:
        assert note["label"] in sheet["svg"] or "&" in note["label"]

"""Driving a mate through the travel it leaves free.

A connector mate said what could move and then held it at zero: `revolute` was
a hinge that always landed at the connectors' own alignment, so a door could be
mated and never opened, a gear never indexed and a servo horn always pointed the
same way. `angle_deg` is which value the solver picks inside that free rotation,
and `drive_mate` changes it on a mate that already exists — the one mate
parameter anybody changes repeatedly, and until now the only way to change it
was to delete the mate and build another one with a different id.

Three things here are the whole point. **Driving does not consume the degree of
freedom** — a door held open 30 degrees is still a door — so `dof` reads the
same before and after. **An angle is refused where no rotation about the mate
axis is free**, because there it would break the constraint rather than position
the part inside it; the legacy bounding-box four are refused for the harder
reason that an extreme has no frame for an angle to be measured against at all.
And **URDF puts it in one of two places**: an articulated joint's `<origin>` is
its zero pose so the angle is reported as state, while a mate emitted `fixed`
has nowhere to carry state and the angle has to go into the geometry.
"""

import math

import numpy as np
import pytest

from app.services import assembly_mates
from app.services import assembly_service
from app.services import cad_operations as ops
from app.services import document_service as docs

V = lambda x, y, z: {"x": x, "y": y, "z": z}  # noqa: E731


def _part(size=(.04, .04, .01), name="part"):
    doc = docs.create_document(name=name, owner_id="dev")
    ops.dispatch("add_primitive", doc.id,
                 {"shape": "box", "params": {"size": V(*size), "center": V(0, 0, 0)}})
    return doc.id


def _assembly(*part_ids):
    asm = assembly_service.create_assembly(name="rig", owner_id="dev")
    ids = []
    for part_id in part_ids:
        ids.append(ops.dispatch("insert_part", asm.id, {"part_id": part_id})["component_id"])
    return asm.id, ids


def _hinge(angle_deg=0.0, mate_type="revolute", moved_origin=(.020, 0, 0)):
    """A hinge whose pin is on world +Z and whose leaf reaches out along +X.

    The moved connector sits 20 mm off its part's origin, so the part origin
    swings on a 20 mm radius and the angle is readable straight off the
    placement rather than inferred from a rotation matrix.
    """
    base, moved = _part(name="frame"), _part(name="door")
    b = ops.dispatch("add_mate_connector", base, {"origin": V(0, 0, .005)})["connector_id"]
    m = ops.dispatch("add_mate_connector", moved,
                     {"origin": V(*moved_origin)})["connector_id"]
    asm_id, (base_c, moved_c) = _assembly(base, moved)
    result = ops.dispatch("add_mate", asm_id,
                          {"type": mate_type, "base": base_c, "moved": moved_c,
                           "base_connector": b, "moved_connector": m,
                           "angle_deg": angle_deg})
    return asm_id, moved_c, result


def _translate(asm_id, component_id):
    t = next(c for c in assembly_service.get_assembly(asm_id).components
             if c.id == component_id).transform.translate
    return np.array([t.x, t.y, t.z])


# ------------------------------------------------------------ the rotation --

def test_a_revolute_at_zero_is_exactly_where_it_always_was():
    """Every stored assembly has `angle_deg` defaulted to zero, so the day this
    landed nothing may have moved."""
    asm_id, moved_c, _ = _hinge()
    # The moved connector is 20 mm out along +X; landing it on the base's
    # (0, 0, 5) puts the part origin 20 mm back along -X.
    assert _translate(asm_id, moved_c) == pytest.approx([-.020, 0.0, .005])


def test_driving_a_revolute_swings_the_part_about_the_pin():
    asm_id, moved_c, _ = _hinge(angle_deg=90.0)
    # Rotating the -X arm by +90 about Z puts the part origin on -Y.
    assert _translate(asm_id, moved_c) == pytest.approx([0.0, -.020, .005], abs=1e-9)


def test_the_swing_radius_is_the_connector_offset_at_every_angle():
    """A hinge is a circle. If the angle were composed on the wrong side of the
    frame the part would still move, and it would move off the arc."""
    for angle in (0.0, 30.0, 45.0, 137.0, -60.0, 180.0):
        asm_id, moved_c, _ = _hinge(angle_deg=angle)
        placed = _translate(asm_id, moved_c)
        assert placed[2] == pytest.approx(.005)
        assert math.hypot(placed[0], placed[1]) == pytest.approx(.020, abs=1e-9)
        # The arm starts pointing at -X and turns with the angle. Compared as
        # a direction rather than a number, because 180 and -180 are the same
        # place and only one of them is what atan2 returns.
        turned = math.radians(180.0 + angle)
        assert placed[:2] / .020 == pytest.approx(
            [math.cos(turned), math.sin(turned)], abs=1e-9)


def test_the_part_is_turned_as_well_as_carried():
    """A door on a hinge does not translate around the arc facing the same way.
    The body's own orientation has to follow the angle too."""
    asm_id, moved_c, _ = _hinge(angle_deg=90.0)
    rotate = next(c for c in assembly_service.get_assembly(asm_id).components
                  if c.id == moved_c).transform.rotate_deg
    assert rotate.z == pytest.approx(90.0)


def test_a_full_turn_lands_where_zero_does():
    zero = _translate(*_hinge(angle_deg=0.0)[:2])
    full = _translate(*_hinge(angle_deg=360.0)[:2])
    assert zero == pytest.approx(full, abs=1e-9)


def test_the_angle_turns_about_the_base_connectors_axis_not_the_world():
    """A hinge whose pin lies along world +Y must swing in the XZ plane."""
    base, moved = _part(name="frame"), _part(name="door")
    b = ops.dispatch("add_mate_connector", base,
                     {"origin": V(0, 0, 0), "z_axis": V(0, 1, 0)})["connector_id"]
    m = ops.dispatch("add_mate_connector", moved, {"origin": V(.020, 0, 0)})["connector_id"]
    asm_id, (base_c, moved_c) = _assembly(base, moved)
    ops.dispatch("add_mate", asm_id,
                 {"type": "revolute", "base": base_c, "moved": moved_c,
                  "base_connector": b, "moved_connector": m, "angle_deg": 90.0})
    placed = _translate(asm_id, moved_c)
    assert placed[1] == pytest.approx(0.0, abs=1e-9)
    assert math.hypot(placed[0], placed[2]) == pytest.approx(.020, abs=1e-9)


def test_the_offset_still_runs_along_the_axis_the_angle_turns_about():
    """Rotating about Z cannot move the Z offset, and a driven hinge that
    slid along its own pin would be a hinge with a lot more travel than it has."""
    asm_id, moved_c, _ = _hinge()
    base_mate = assembly_service.get_assembly(asm_id).mates[0]
    ops.dispatch("drive_mate", asm_id, {"mate_id": base_mate.id, "offset_mm": 6.0})
    flat = _translate(asm_id, moved_c)
    ops.dispatch("drive_mate", asm_id, {"mate_id": base_mate.id, "angle_deg": 75.0})
    turned = _translate(asm_id, moved_c)
    assert turned[2] == pytest.approx(flat[2])


# ------------------------------------------------------- what may be driven --

@pytest.mark.parametrize("kind", sorted(assembly_mates.DRIVEN_ROTATION))
def test_every_type_that_leaves_a_rotation_about_its_axis_takes_an_angle(kind):
    asm_id, moved_c, result = _hinge(angle_deg=45.0, mate_type=kind)
    assert result["angle_deg"] == 45.0
    assert result["drivable"] is True
    assert math.hypot(*_translate(asm_id, moved_c)[:2]) == pytest.approx(.020, abs=1e-9)


@pytest.mark.parametrize("kind", ["fasten", "slider"])
def test_a_type_with_no_free_rotation_refuses_an_angle(kind):
    """It would break the constraint rather than position the part inside it."""
    base, moved = _part(name="frame"), _part(name="door")
    b = ops.dispatch("add_mate_connector", base, {"origin": V(0, 0, 0)})["connector_id"]
    m = ops.dispatch("add_mate_connector", moved, {"origin": V(.020, 0, 0)})["connector_id"]
    asm_id, (base_c, moved_c) = _assembly(base, moved)
    envelope = ops.execute("add_mate", asm_id,
                           {"type": kind, "base": base_c, "moved": moved_c,
                            "base_connector": b, "moved_connector": m,
                            "angle_deg": 30.0})
    assert not envelope["ok"]
    assert envelope["error"]["code"] == "BAD_ARG"
    assert envelope["error"]["arg"] == "angle_deg"
    assert "revolute" in envelope["error"]["message"]


@pytest.mark.parametrize("kind", ["fasten", "coincident", "concentric", "distance"])
def test_a_bounding_box_mate_refuses_an_angle(kind):
    """An extreme cannot express a rotation, so there is no frame for an angle
    to be measured against and driving one would place the part somewhere
    invented — which is harder to notice than a refusal."""
    base, moved = _part(name="frame"), _part(name="door")
    asm_id, (base_c, moved_c) = _assembly(base, moved)
    envelope = ops.execute("add_mate", asm_id,
                           {"type": kind, "base": base_c, "moved": moved_c,
                            "angle_deg": 30.0})
    assert not envelope["ok"]
    assert envelope["error"]["arg"] == "angle_deg"


def test_zero_is_not_a_drive_so_a_fasten_still_takes_the_default():
    """The refusal is on driving, not on the field existing. Every legacy mate
    carries `angle_deg: 0` and must keep being buildable."""
    base, moved = _part(name="frame"), _part(name="door")
    asm_id, (base_c, moved_c) = _assembly(base, moved)
    envelope = ops.execute("add_mate", asm_id,
                           {"type": "fasten", "base": base_c, "moved": moved_c,
                            "angle_deg": 0.0})
    assert envelope["ok"], envelope.get("error")


# --------------------------------------------------------------- drive_mate --

def test_drive_mate_moves_the_part_without_rebuilding_the_mate():
    asm_id, moved_c, _ = _hinge()
    mate_id = assembly_service.get_assembly(asm_id).mates[0].id
    result = ops.dispatch("drive_mate", asm_id, {"mate_id": mate_id, "angle_deg": 90.0})
    assert result["angle_deg"] == 90.0
    assert result["type"] == "revolute"
    after = assembly_service.get_assembly(asm_id)
    assert [m.id for m in after.mates] == [mate_id], "the mate keeps its identity"
    assert _translate(asm_id, moved_c) == pytest.approx([0.0, -.020, .005], abs=1e-9)


def test_the_angle_is_absolute_rather_than_an_increment():
    """Two drives to the same angle land in the same place. An increment would
    make a retried call a different assembly."""
    asm_id, moved_c, _ = _hinge()
    mate_id = assembly_service.get_assembly(asm_id).mates[0].id
    ops.dispatch("drive_mate", asm_id, {"mate_id": mate_id, "angle_deg": 40.0})
    once = _translate(asm_id, moved_c)
    ops.dispatch("drive_mate", asm_id, {"mate_id": mate_id, "angle_deg": 40.0})
    assert _translate(asm_id, moved_c) == pytest.approx(once)


def test_driving_does_not_consume_the_degree_of_freedom():
    """A door held open 30 degrees is still a door. Reporting it as constrained
    would tell an agent it had fixed something it had only posed."""
    asm_id, moved_c, _ = _hinge()
    mate_id = assembly_service.get_assembly(asm_id).mates[0].id
    before = ops.dispatch("dof", asm_id, {})
    result = ops.dispatch("drive_mate", asm_id, {"mate_id": mate_id, "angle_deg": 30.0})
    assert result["removes_dof"] == 0
    assert ops.dispatch("dof", asm_id, {}) == before


def test_drive_mate_refuses_an_angle_on_a_mate_that_leaves_none_free():
    base, moved = _part(name="frame"), _part(name="door")
    asm_id, (base_c, moved_c) = _assembly(base, moved)
    ops.dispatch("add_mate", asm_id, {"type": "coincident", "base": base_c,
                                      "moved": moved_c})
    mate_id = assembly_service.get_assembly(asm_id).mates[0].id
    envelope = ops.execute("drive_mate", asm_id,
                           {"mate_id": mate_id, "angle_deg": 30.0})
    assert not envelope["ok"]
    assert envelope["error"]["arg"] == "angle_deg"


def test_drive_mate_names_a_mate_that_does_not_exist():
    asm_id, _, _ = _hinge()
    envelope = ops.execute("drive_mate", asm_id, {"mate_id": "nope", "angle_deg": 10.0})
    assert not envelope["ok"]
    assert envelope["error"]["code"] == "BAD_ARG"
    assert envelope["error"]["arg"] == "mate_id"


def test_drive_mate_with_nothing_to_drive_is_refused():
    """An operation that reports success and changes nothing is the worst thing
    this action space can contain."""
    asm_id, _, _ = _hinge()
    mate_id = assembly_service.get_assembly(asm_id).mates[0].id
    envelope = ops.execute("drive_mate", asm_id, {"mate_id": mate_id})
    assert not envelope["ok"]
    assert "angle_deg" in envelope["error"]["message"]


def test_drive_mate_slides_a_slider_along_its_rail():
    """`offset_mm` is the translational twin and is drivable on every type."""
    asm_id, moved_c, _ = _hinge(mate_type="slider")
    mate_id = assembly_service.get_assembly(asm_id).mates[0].id
    ops.dispatch("drive_mate", asm_id, {"mate_id": mate_id, "offset_mm": 12.0})
    assert _translate(asm_id, moved_c)[2] == pytest.approx(.005 + .012)


def test_a_driven_pose_survives_a_reload():
    """It is stored on the mate, so it is the assembly rather than a view of it."""
    asm_id, moved_c, _ = _hinge()
    mate_id = assembly_service.get_assembly(asm_id).mates[0].id
    ops.dispatch("drive_mate", asm_id, {"mate_id": mate_id, "angle_deg": 55.0})
    from app.services import persistence

    stored = persistence.load_assembly_doc(asm_id)
    assert stored["mates"][0]["angle_deg"] == pytest.approx(55.0)
    # And re-solved from that row rather than from anything held in memory:
    # `get_assembly` loads and places every time.
    turned = math.radians(180.0 + 55.0)
    assert _translate(asm_id, moved_c)[:2] / .020 == pytest.approx(
        [math.cos(turned), math.sin(turned)], abs=1e-9)


def test_the_observation_shows_the_angle():
    """A state an operation moves and the observation does not show is one
    nothing can be trained against."""
    from app.services import observation

    asm_id, _, _ = _hinge(angle_deg=25.0)
    state = observation.summarize_assembly(assembly_service.get_assembly(asm_id))
    assert state["mates"][0]["angle_deg"] == pytest.approx(25.0)


# --------------------------------------------------------------------- URDF --

def test_an_articulated_joint_keeps_its_zero_pose_and_reports_the_angle():
    """URDF's `<origin>` is the joint's zero configuration. Baking the current
    hinge angle into it would move the machine's zero every time somebody swung
    the door, and a simulator commanding q=0 would get the door open."""
    from app.services import urdf

    shut_id, _, _ = _hinge(angle_deg=0.0)
    open_id, _, _ = _hinge(angle_deg=90.0)
    shut = urdf.export_urdf(shut_id)
    swung = urdf.export_urdf(open_id)

    def origin(result):
        line = next(l for l in result["urdf"].splitlines()
                    if "<origin" in l and "rpy" in l and "joint" not in l
                    and "inertial" not in l)
        return line

    joint_shut = [l for l in shut["urdf"].splitlines() if "<origin" in l]
    joint_open = [l for l in swung["urdf"].splitlines() if "<origin" in l]
    assert joint_shut == joint_open, "the zero pose must not move with the angle"
    assert shut["driven"] == []
    assert swung["driven"][0]["angle_deg"] == pytest.approx(90.0)
    assert swung["driven"][0]["urdf_type"] == "continuous"


def test_a_fixed_joint_carries_the_angle_in_its_geometry():
    """A cylindrical mate has no URDF equivalent and is emitted `fixed`. A fixed
    joint has no state to carry a pose, so dropping the angle would put the part
    somewhere the assembly says it is not, permanently."""
    from app.services import urdf

    shut = urdf.export_urdf(_hinge(angle_deg=0.0, mate_type="cylindrical")[0])
    swung = urdf.export_urdf(_hinge(angle_deg=90.0, mate_type="cylindrical")[0])
    assert [l for l in shut["urdf"].splitlines() if "<origin" in l] != \
        [l for l in swung["urdf"].splitlines() if "<origin" in l]
    assert swung["driven"] == [], "there is no joint state to report"
    assert any(u["type"] == "cylindrical" for u in swung["unsupported"])

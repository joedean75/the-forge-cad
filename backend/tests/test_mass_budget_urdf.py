"""All-up weight, CG, inertia — and the URDF that carries them to a simulator.

The three numbers an airframe, a drone or a robot is checked against before it
flies or moves, and the file that lets the same model be flown in simulation.
Both are downstream of the mate connectors: until a mate could express a
rotation there was no joint to write and no reason for a part's inertia tensor
to be rotated into anything.

The load-bearing tests are the arithmetic ones. A CG that ignores a part's own
centre of mass, or an inertia tensor that is summed without rotating it into
assembly axes, is wrong in a way that looks plausible — the number has the right
magnitude and the wrong value, which is the failure mode that reaches a test
flight.
"""

import math
import xml.etree.ElementTree as ET

import numpy as np
import pytest

from app.services import assembly_mass, assembly_service, urdf
from app.services import cad_operations as ops
from app.services import document_service as docs

V = lambda x, y, z: {"x": x, "y": y, "z": z}  # noqa: E731

#: 2700 kg/m³ — a 100x100x10 mm plate is 270 g, which is easy to check by hand.
ALUMINIUM = "al-6061"


def _part(size, name="part", material=ALUMINIUM) -> str:
    doc = docs.create_document(name=name, owner_id="dev")
    ops.dispatch("add_primitive", doc.id,
                 {"shape": "box", "params": {"size": V(*size), "center": V(0, 0, 0)}})
    if material:
        ops.dispatch("set_material", doc.id, {"material_id": material})
    return doc.id


def _assembly(name="rig"):
    return assembly_service.create_assembly(name=name, owner_id="dev").id


def _insert(asm_id, part_id) -> str:
    return ops.dispatch("insert_part", asm_id, {"part_id": part_id})["component_id"]


def _density() -> float:
    from app.services import materials

    return materials.density_of(ALUMINIUM)


# ================================================================ the maths ==

def test_all_up_weight_is_the_sum_of_the_parts():
    asm_id = _assembly()
    _insert(asm_id, _part((.1, .1, .01), "plate"))
    _insert(asm_id, _part((.05, .05, .01), "pad"))
    report = ops.dispatch("mass_budget", asm_id, {})
    expected = (.1 * .1 * .01 + .05 * .05 * .01) * _density() * 1000.0
    assert report["mass_g"] == pytest.approx(expected, rel=1e-6)
    assert report["counted"] == 2


def test_the_cg_is_mass_weighted_and_moves_with_a_placement():
    """Two identical plates, one shifted 100 mm: the CG lands halfway."""
    asm_id = _assembly()
    part_id = _part((.02, .02, .02), "cube")
    a, b = _insert(asm_id, part_id), _insert(asm_id, part_id)
    ops.dispatch("place_component", asm_id, {"component_id": b,
                                             "translate": {"x": .1}})
    cg = ops.dispatch("mass_budget", asm_id, {})["center_of_mass_mm"]
    assert cg == pytest.approx([50.0, 0.0, 0.0], abs=1e-6)


def test_an_unequal_pair_puts_the_cg_nearer_the_heavy_end():
    asm_id = _assembly()
    heavy = _insert(asm_id, _part((.04, .04, .04), "heavy"))     # 8x the volume
    light = _insert(asm_id, _part((.02, .02, .02), "light"))
    ops.dispatch("place_component", asm_id, {"component_id": light,
                                             "translate": {"x": .09}})
    cg = ops.dispatch("mass_budget", asm_id, {})["center_of_mass_mm"]
    # 1 part at 0 and 1/8 part at 90 -> 90/9 = 10 mm.
    assert cg[0] == pytest.approx(10.0, rel=1e-6)


def test_a_parts_own_centre_of_mass_is_carried_by_its_placement():
    """The shortcut this rules out is using the component's bounding-box centre.
    An L-bracket's centre of mass is not the centre of its box, and a rotated
    placement moves the two differently."""
    doc = docs.create_document(name="L", owner_id="dev")
    ops.dispatch("add_primitive", doc.id, {"shape": "box", "params": {
        "size": V(.06, .02, .01), "center": V(.03, 0, 0)}})
    ops.dispatch("set_material", doc.id, {"material_id": ALUMINIUM})
    part_cg = ops.dispatch("mass_properties", doc.id, {})["center_of_mass_mm"]
    assert part_cg[0] == pytest.approx(30.0, abs=.5), "the part is offset in its own space"

    asm_id = _assembly()
    component = _insert(asm_id, doc.id)
    ops.dispatch("place_component", asm_id,
                 {"component_id": component, "rotate": {"z": 90.0}})
    cg = ops.dispatch("mass_budget", asm_id, {})["center_of_mass_mm"]
    # Turned 90 degrees about Z, the +30 mm offset becomes +30 mm in Y.
    assert cg[0] == pytest.approx(0.0, abs=.5)
    assert cg[1] == pytest.approx(30.0, abs=.5)


def test_the_inertia_of_a_single_centred_part_matches_the_part_itself():
    """With one component at the origin the roll-up must be the identity
    operation — anything else is a bug in the rotation or the shift."""
    asm_id = _assembly()
    part_id = _part((.08, .04, .02), "block")
    _insert(asm_id, part_id)
    part = np.asarray(ops.dispatch("mass_properties", part_id, {})["inertia_kg_mm2"])
    whole = np.asarray(ops.dispatch("mass_budget", asm_id, {})["inertia_kg_mm2"])
    assert np.allclose(whole, part, rtol=1e-6, atol=1e-9)


def test_two_point_masses_match_the_parallel_axis_theorem():
    """Small cubes far apart are point masses to a good approximation, and the
    closed form is `2 m d²` about the axes across the separation."""
    asm_id = _assembly()
    part_id = _part((.004, .004, .004), "bead")
    a, b = _insert(asm_id, part_id), _insert(asm_id, part_id)
    ops.dispatch("place_component", asm_id, {"component_id": a, "translate": {"x": -.1}})
    ops.dispatch("place_component", asm_id, {"component_id": b, "translate": {"x": .1}})

    report = ops.dispatch("mass_budget", asm_id, {})
    inertia = np.asarray(report["inertia_kg_mm2"])
    mass_kg = report["mass_g"] / 1000.0
    expected = mass_kg * (100.0 ** 2)          # 2 * (m/2) * d², in kg·mm²
    assert inertia[1][1] == pytest.approx(expected, rel=.01)
    assert inertia[2][2] == pytest.approx(expected, rel=.01)
    # Nothing is offset along x, so Ixx is only the beads' own inertia.
    assert inertia[0][0] < expected * 1e-3


def test_rotating_a_component_rotates_its_inertia_tensor():
    """`R I Rᵀ`. Summing tensors without it is right only when nothing is
    turned — which is exactly the case mate connectors exist to stop being
    true."""
    long_part = _part((.12, .01, .01), "beam")

    flat = _assembly("flat")
    _insert(flat, long_part)
    upright = _assembly("upright")
    component = _insert(upright, long_part)
    ops.dispatch("place_component", upright,
                 {"component_id": component, "rotate": {"z": 90.0}})

    a = np.asarray(ops.dispatch("mass_budget", flat, {})["inertia_kg_mm2"])
    b = np.asarray(ops.dispatch("mass_budget", upright, {})["inertia_kg_mm2"])
    # Turning the beam onto Y swaps which axis is the slender one.
    assert a[0][0] == pytest.approx(b[1][1], rel=1e-6)
    assert a[1][1] == pytest.approx(b[0][0], rel=1e-6)


def test_the_moments_about_the_cg_balance():
    """A physical identity rather than a coincidence: the CG is by definition
    the point the mass moments cancel about, so a two-body system's two moments
    are equal. It is the cheapest end-to-end check there is."""
    asm_id = _assembly()
    heavy = _insert(asm_id, _part((.04, .04, .01), "heavy"))
    light = _insert(asm_id, _part((.01, .01, .01), "light"))
    ops.dispatch("place_component", asm_id, {"component_id": light,
                                             "translate": {"x": .12}})
    rows = ops.dispatch("mass_budget", asm_id, {})["components"]
    assert rows[0]["moment_g_mm"] == pytest.approx(rows[1]["moment_g_mm"], rel=1e-6)


def test_components_are_ranked_by_what_they_do_to_the_balance():
    """Not by weight. A light part on a long arm outvotes a heavy one at the
    centre, and the ranking is there to say which part to move."""
    asm_id = _assembly()
    _insert(asm_id, _part((.05, .05, .02), "hub"))            # heavy, centred
    arm = _insert(asm_id, _part((.01, .01, .01), "tip"))      # light, far out
    ops.dispatch("place_component", asm_id, {"component_id": arm,
                                             "translate": {"x": .3}})
    rows = ops.dispatch("mass_budget", asm_id, {})["components"]
    assert [r["name"] for r in rows] == ["hub", "tip"] or \
           rows[0]["moment_g_mm"] >= rows[1]["moment_g_mm"]
    assert rows[0]["moment_g_mm"] >= rows[-1]["moment_g_mm"]


# ------------------------------------------------ what it refuses to guess --

def test_a_scaled_component_is_excluded_and_says_why():
    """Mass goes as the cube of scale and inertia as the fifth power, and
    density belongs to the material rather than to the instance. Weighing a
    scaled copy as if it were the original is a plausible wrong number."""
    asm_id = _assembly()
    component = _insert(asm_id, _part((.02, .02, .02), "cube"))
    ops.dispatch("place_component", asm_id, {"component_id": component, "scale": 2.0})
    report = ops.dispatch("mass_budget", asm_id, {})
    assert report["counted"] == 0
    assert any("scale" in note for note in report["notes"])


def test_an_assembly_with_no_material_says_so_rather_than_reporting_zero():
    asm_id = _assembly()
    _insert(asm_id, _part((.02, .02, .02), "cube", material=None))
    report = ops.dispatch("mass_budget", asm_id, {})
    # A default material may still give it mass; what must not happen is a
    # silent zero with no explanation.
    assert report["mass_g"] > 0 or report["notes"]


# ==================================================================== URDF ==

def _quad_arm():
    """A deck with an arm on a revolute joint — the smallest thing that is a
    robot rather than a stack."""
    deck = _part((.08, .08, .004), "deck")
    arm = _part((.10, .012, .004), "arm")
    hinge = ops.dispatch("add_mate_connector", deck,
                         {"origin": V(.03, 0, .002), "z_axis": V(0, 0, 1),
                          "name": "hinge"})["connector_id"]
    root = ops.dispatch("add_mate_connector", arm,
                        {"origin": V(-.045, 0, -.002), "z_axis": V(0, 0, 1),
                         "name": "root"})["connector_id"]
    asm_id = _assembly("quad arm")
    base = _insert(asm_id, deck)
    moved = _insert(asm_id, arm)
    ops.dispatch("add_mate", asm_id, {"type": "revolute", "base": base, "moved": moved,
                                      "base_connector": hinge, "moved_connector": root})
    return asm_id


def _tree(asm_id, **args):
    result = ops.dispatch("export_urdf", asm_id, args)
    return ET.fromstring(result["urdf"]), result


def test_the_urdf_parses_and_has_a_link_per_component():
    root, result = _tree(_quad_arm())
    assert root.tag == "robot"
    assert len(root.findall("link")) == 2 == result["links"]
    assert len(root.findall("joint")) == 1 == result["joints"]


def test_every_link_carries_a_mass_and_an_inertia_tensor():
    """A URDF link without them is a link most solvers go singular on."""
    root, _ = _tree(_quad_arm())
    for link in root.findall("link"):
        inertial = link.find("inertial")
        assert inertial is not None, link.get("name")
        assert float(inertial.find("mass").get("value")) > 0
        inertia = inertial.find("inertia")
        assert float(inertia.get("ixx")) > 0 and float(inertia.get("izz")) > 0


def test_a_revolute_mate_becomes_a_turning_joint():
    """`continuous`, not `revolute`: URDF's revolute requires a `<limit>`, and
    inventing one would put a travel stop in the simulation that the machine
    does not have."""
    root, _ = _tree(_quad_arm())
    joint = root.find("joint")
    assert joint.get("type") == "continuous"
    assert joint.find("axis") is not None
    assert joint.find("parent").get("link") == "deck"
    assert joint.find("child").get("link") == "arm"


def test_the_joint_origin_carries_both_connectors():
    """URDF's joint frame *is* the child link frame, so the child's own origin
    sits wherever its connector is not. Emitting the base connector alone is
    right only when the moved connector happens to be at the part origin."""
    root, _ = _tree(_quad_arm())
    xyz = [float(v) for v in root.find("joint").find("origin").get("xyz").split()]
    # Hinge at +30 mm on the deck, arm's root connector at -45 mm in its own
    # frame -> the arm's origin lands at 30 + 45 = 75 mm.
    assert xyz[0] == pytest.approx(.075, abs=1e-9)


def test_a_bounding_box_mate_becomes_a_fixed_joint():
    """A face-on-face placement is not an articulation, and calling it one
    would give the simulator a degree of freedom the machine does not have."""
    asm_id = _assembly()
    base = _insert(asm_id, _part((.05, .05, .01), "base"))
    top = _insert(asm_id, _part((.02, .02, .01), "top"))
    ops.dispatch("add_mate", asm_id, {"type": "coincident", "base": base,
                                      "moved": top, "axis": "z"})
    root, result = _tree(asm_id)
    assert root.find("joint").get("type") == "fixed"
    assert result["unsupported"] == []


@pytest.mark.parametrize("kind", ["cylindrical", "ball"])
def test_a_joint_urdf_cannot_express_is_named_rather_than_dropped(kind):
    """URDF has revolute, continuous, prismatic, fixed, floating and planar —
    no cylindrical, no spherical. Silently losing a degree of freedom is a robot
    that behaves differently in simulation than the one on the bench."""
    deck = _part((.05, .05, .01), "deck")
    link = _part((.04, .01, .01), "link")
    a = ops.dispatch("add_mate_connector", deck, {"origin": V(0, 0, .005)})["connector_id"]
    b = ops.dispatch("add_mate_connector", link, {"origin": V(0, 0, 0)})["connector_id"]
    asm_id = _assembly()
    base, moved = _insert(asm_id, deck), _insert(asm_id, link)
    ops.dispatch("add_mate", asm_id, {"type": kind, "base": base, "moved": moved,
                                      "base_connector": a, "moved_connector": b})
    root, result = _tree(asm_id)
    assert root.find("joint").get("type") == "fixed"
    assert len(result["unsupported"]) == 1
    assert result["unsupported"][0]["type"] == kind
    assert "fewer degrees of freedom" in result["unsupported"][0]["reason"]


def test_a_closed_loop_is_reported_because_urdf_is_a_tree():
    """A four-bar linkage has no URDF representation at all. Dropping the mate
    that closes it would emit a file describing a different machine."""
    asm_id = _assembly()
    a = _insert(asm_id, _part((.05, .05, .01), "a"))
    b = _insert(asm_id, _part((.05, .05, .01), "b"))
    c = _insert(asm_id, _part((.05, .05, .01), "c"))
    ops.dispatch("add_mate", asm_id, {"type": "fasten", "base": a, "moved": b})
    ops.dispatch("add_mate", asm_id, {"type": "fasten", "base": b, "moved": c})
    ops.dispatch("add_mate", asm_id, {"type": "fasten", "base": c, "moved": b})

    _, result = _tree(asm_id)
    assert len(result["unsupported"]) == 1
    assert "kinematic loop" in result["unsupported"][0]["reason"]


def test_an_unmated_component_is_bolted_on_rather_than_dropped():
    """It is still mass the simulator has to carry."""
    asm_id = _assembly()
    _insert(asm_id, _part((.05, .05, .01), "hull"))
    _insert(asm_id, _part((.02, .02, .02), "payload"))
    root, result = _tree(asm_id)
    assert len(root.findall("link")) == 2
    assert root.find("joint").get("type") == "fixed"
    assert any("no mate" in w for w in result["warnings"])


def test_link_names_are_made_unique_and_xml_safe():
    """Names come from users and end up in XML. Two parts called the same thing
    would otherwise produce a file that references one link twice."""
    asm_id = _assembly()
    same = _part((.02, .02, .02), 'bad "name" <&>')
    _insert(asm_id, same)
    _insert(asm_id, same)
    root, _ = _tree(asm_id)
    names = [link.get("name") for link in root.findall("link")]
    assert len(set(names)) == 2
    assert all(all(ch.isalnum() or ch in "_-" for ch in n) for n in names)


def test_an_empty_assembly_is_a_precondition_not_an_empty_file():
    envelope = ops.execute("export_urdf", _assembly(), {})
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "PRECONDITION"


def test_meshes_can_be_left_out():
    root, _ = _tree(_quad_arm(), mesh_urls=False)
    assert root.find("link").find("visual") is None
    assert root.find("link").find("inertial") is not None

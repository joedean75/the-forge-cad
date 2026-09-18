"""SRDF: the semantic half, and the places it refuses to invent one."""

import xml.etree.ElementTree as ET

import pytest

from app.services import assembly_service
from app.services import cad_operations as ops
from app.services import document_service as docs
from app.services.op_errors import PreconditionFailed

V = lambda x, y, z: {"x": x, "y": y, "z": z}  # noqa: E731
ALUMINIUM = "al-6061"


def _part(size, name="part") -> str:
    doc = docs.create_document(name=name, owner_id="dev")
    ops.dispatch("add_primitive", doc.id,
                 {"shape": "box", "params": {"size": V(*size), "center": V(0, 0, 0)}})
    ops.dispatch("set_material", doc.id, {"material_id": ALUMINIUM})
    return doc.id


def _assembly(name="rig"):
    return assembly_service.create_assembly(name=name, owner_id="dev").id


def _insert(asm_id, part_id) -> str:
    return ops.dispatch("insert_part", asm_id, {"part_id": part_id})["component_id"]


def _connector(doc_id, origin, name):
    return ops.dispatch("add_mate_connector", doc_id,
                        {"origin": origin, "z_axis": V(0, 0, 1), "name": name})["connector_id"]


def _arm_rig(part_name="arm"):
    """A deck with an arm on a revolute joint — one chain, one mated pair."""
    deck, arm = _part((.08, .08, .004), "deck"), _part((.10, .012, .004), part_name)
    hinge = _connector(deck, V(.03, 0, .002), "hinge")
    root = _connector(arm, V(-.045, 0, -.002), "root")
    asm_id = _assembly("arm rig")
    base, moved = _insert(asm_id, deck), _insert(asm_id, arm)
    ops.dispatch("add_mate", asm_id, {"type": "revolute", "base": base, "moved": moved,
                                      "base_connector": hinge, "moved_connector": root})
    return asm_id


def _robot(result):
    root = ET.fromstring(result["srdf"])
    assert root.tag == "robot"
    return root


def test_a_chain_becomes_a_group_and_the_mated_pair_is_adjacent():
    result = ops.dispatch("export_srdf", _arm_rig(), {})
    root = _robot(result)

    groups = root.findall("group")
    assert len(groups) == 1 == result["groups"]
    chain = groups[0].find("chain")
    assert chain.get("base_link") == "deck" and chain.get("tip_link") == "arm"

    pairs = root.findall("disable_collisions")
    assert [p.get("reason") for p in pairs] == ["Adjacent"]
    assert {pairs[0].get("link1"), pairs[0].get("link2")} == {"deck", "arm"}


def test_the_link_names_are_the_ones_the_urdf_uses():
    """A group naming a link the URDF does not have plans against nothing, and
    nothing reports an error anywhere."""
    asm_id = _arm_rig()
    urdf_links = {link.get("name") for link in
                  ET.fromstring(ops.dispatch("export_urdf", asm_id, {})["urdf"]).findall("link")}
    chain = _robot(ops.dispatch("export_srdf", asm_id, {})).find("group").find("chain")
    assert {chain.get("base_link"), chain.get("tip_link")} <= urdf_links


def test_an_assembly_that_cannot_move_is_refused_rather_than_written_empty():
    """Every joint fixed means no group, and an SRDF with no groups is a file
    that says nothing a planner can use."""
    deck, plate = _part((.08, .08, .004), "deck"), _part((.06, .06, .004), "plate")
    asm_id = _assembly("stack")
    base, moved = _insert(asm_id, deck), _insert(asm_id, plate)
    ops.dispatch("add_mate", asm_id, {"type": "coincident", "base": base,
                                      "moved": moved, "axis": "z"})
    with pytest.raises(PreconditionFailed) as excinfo:
        ops.dispatch("export_srdf", asm_id, {})
    assert "cannot move" in str(excinfo.value)


def test_a_duplicate_name_drops_the_pair_and_says_why():
    """Interference reports pairs by name; link names are de-duplicated. An
    ambiguous name cannot be matched back, and disabling the wrong pair tells a
    planner it may drive one part through another."""
    asm_id = _arm_rig(part_name="deck")      # two components both called "deck"
    result = ops.dispatch("export_srdf", asm_id, {"clearance_mm": 50.0})
    names = {c.name for c in assembly_service.get_assembly(asm_id).components}
    assert names == {"deck"}, "the fixture needs the collision of names"
    # The mated pair is still exact — it comes from ids, not from names.
    reasons = [p.get("reason") for p in _robot(result).findall("disable_collisions")]
    assert "Adjacent" in reasons
    assert any("more than one component" in w for w in result["warnings"])


def test_touching_pairs_are_disabled_as_default():
    """A wide clearance makes the two parts 'touch', which is the case a planner
    would otherwise refuse to move through."""
    result = ops.dispatch("export_srdf", _arm_rig(), {"clearance_mm": 200.0})
    pairs = _robot(result).findall("disable_collisions")
    # Adjacent wins for the mated pair; any further pair is a Default one.
    assert pairs and all(p.get("reason") in ("Adjacent", "Default") for p in pairs)
    assert len({(p.get("link1"), p.get("link2")) for p in pairs}) == len(pairs), "no duplicates"

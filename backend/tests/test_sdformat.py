"""SDFormat: the joints URDF made us decline, and the two it gives up in return.

Every test here compares the *same assembly* through both exporters. A new
format asserted only against itself proves nothing — the reason this one exists
is that two of `export_urdf`'s refusals are limits of URDF rather than facts
about the assembly, and that claim is only meaningful side by side.
"""

import xml.etree.ElementTree as ET

from app.services import assembly_service
from app.services import cad_operations as ops
from app.services import document_service as docs

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


def _two_link(mate_type: str):
    """A deck and an arm joined by one connector mate of the given type."""
    deck, arm = _part((.08, .08, .004), "deck"), _part((.10, .012, .004), "arm")
    hinge = _connector(deck, V(.03, 0, .002), "hinge")
    root = _connector(arm, V(-.045, 0, -.002), "root")
    asm_id = _assembly(f"{mate_type} rig")
    base, moved = _insert(asm_id, deck), _insert(asm_id, arm)
    ops.dispatch("add_mate", asm_id, {"type": mate_type, "base": base, "moved": moved,
                                      "base_connector": hinge, "moved_connector": root})
    return asm_id


def _model(result):
    root = ET.fromstring(result["sdf"])
    assert root.tag == "sdf"
    return root.find("model")


def test_the_model_parses_and_carries_a_link_per_component():
    result = ops.dispatch("export_sdf", _two_link("revolute"), {})
    model = _model(result)
    assert len(model.findall("link")) == 2 == result["links"]
    assert len(model.findall("joint")) == 1 == result["joints"]
    assert model.findall("joint")[0].get("type") == "revolute"


def test_a_ball_mate_is_a_real_joint_here_and_a_fixed_one_in_urdf():
    """URDF has no spherical joint, so it loses three degrees of freedom and
    says so. SDFormat has one, which is the whole reason this exporter exists."""
    asm_id = _two_link("ball")

    as_urdf = ops.dispatch("export_urdf", asm_id, {})
    joint = ET.fromstring(as_urdf["urdf"]).find("joint")
    assert joint.get("type") == "fixed"
    assert any(row["type"] == "ball" for row in as_urdf["unsupported"])

    as_sdf = ops.dispatch("export_sdf", asm_id, {})
    assert _model(as_sdf).find("joint").get("type") == "ball"
    assert not as_sdf["unsupported"]


def test_a_closed_loop_is_a_graph_here_and_unrepresentable_in_urdf():
    """A URDF link has exactly one parent; an SDFormat model is a graph."""
    deck, arm, post = (_part((.08, .08, .004), "deck"), _part((.10, .012, .004), "arm"),
                       _part((.02, .02, .05), "post"))
    asm_id = _assembly("loop")
    base, moved, second = _insert(asm_id, deck), _insert(asm_id, arm), _insert(asm_id, post)
    a = _connector(deck, V(.03, 0, .002), "a")
    b = _connector(arm, V(-.045, 0, -.002), "b")
    c = _connector(post, V(0, 0, .025), "c")
    d = _connector(arm, V(.045, 0, -.002), "d")
    ops.dispatch("add_mate", asm_id, {"type": "revolute", "base": base, "moved": moved,
                                      "base_connector": a, "moved_connector": b})
    # A second parent for the arm — the edge that closes the loop.
    ops.dispatch("add_mate", asm_id, {"type": "revolute", "base": second, "moved": moved,
                                      "base_connector": c, "moved_connector": d})

    as_urdf = ops.dispatch("export_urdf", asm_id, {})
    assert any("loop" in row["reason"] for row in as_urdf["unsupported"])

    as_sdf = ops.dispatch("export_sdf", asm_id, {})
    joints = _model(as_sdf).findall("joint")
    assert len(joints) == 2, "both edges of the loop are written"
    assert not as_sdf["unsupported"]


def test_a_cylindrical_mate_is_declined_rather_than_coupled_into_a_screw():
    """`screw` is the near miss and it is the wrong answer: it ties rotation to
    translation through a pitch, where a cylindrical mate leaves them free."""
    result = ops.dispatch("export_sdf", _two_link("cylindrical"), {})
    assert _model(result).find("joint").get("type") == "fixed"
    reason = next(r["reason"] for r in result["unsupported"] if r["type"] == "cylindrical")
    assert "screw" in reason and "independent" in reason


def test_planar_inverts_and_the_file_says_which_exporter_has_it():
    """The one place URDF is the better target, reported rather than smoothed."""
    asm_id = _two_link("planar")
    assert ET.fromstring(ops.dispatch("export_urdf", asm_id, {})["urdf"]
                         ).find("joint").get("type") == "planar"
    result = ops.dispatch("export_sdf", asm_id, {})
    assert _model(result).find("joint").get("type") == "fixed"
    assert "URDF" in next(r["reason"] for r in result["unsupported"]
                          if r["type"] == "planar")


def test_each_link_carries_its_own_placement():
    """A URDF link has no pose of its own — position is whatever the joint tree
    implies. An SDFormat link states where it is, so the component's actual
    placement is written rather than reconstructed."""
    asm_id = _two_link("revolute")
    asm = assembly_service.get_assembly(asm_id)
    placed = asm.components[1]
    result = ops.dispatch("export_sdf", asm_id, {})
    poses = [link.find("pose").text.split() for link in _model(result).findall("link")]
    assert len(poses) == 2 and all(len(p) == 6 for p in poses), "x y z r p y"
    written = [float(v) for v in poses[1][:3]]
    expected = [placed.transform.translate.x, placed.transform.translate.y,
                placed.transform.translate.z]
    assert written == [round(e, 9) or e for e in written] and \
        max(abs(w - e) for w, e in zip(written, expected)) < 1e-9


def test_an_empty_assembly_is_refused_rather_than_written_empty():
    env = ops.execute("export_sdf", _assembly("nothing"), {}, include_state=False)
    assert not env["ok"] and env["error"]["code"] == "PRECONDITION"

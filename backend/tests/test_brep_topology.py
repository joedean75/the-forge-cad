"""Addressable topology, and the operations that need it.

This is the idiom the SDF kernel cannot express at any API quality. Its "fillet"
is a smooth boolean between two bodies — it cannot name one edge of one body —
and its "shell" is `abs(d) - t/2`, which hollows uniformly and cannot leave a
face open. Both are useful; neither is what an engineer means.

The tests check the two things that make selection usable: a model can *see*
what it may select, and a reference *survives* the rebuild that follows.
"""

import math

import pytest

from app.cad_kernel.evaluator import brep
from app.services import cad_operations as ops
from app.services import document_service as docs

pytestmark = pytest.mark.skipif(not brep.AVAILABLE, reason="OCP (OpenCascade) not installed")

V = lambda x, y, z: {"x": x, "y": y, "z": z}  # noqa: E731


def _plate(x=.08, y=.05, z=.02):
    doc = docs.create_document(name="Plate", resolution=48, owner_id=None)
    ops.dispatch("add_primitive", doc.id, {"shape": "box", "params": {"size": V(x, y, z)}})
    return doc.id


def _bored_plate():
    doc_id = _plate()
    ops.dispatch("add_primitive", doc_id, {"shape": "cylinder", "operation": "subtract",
                                           "params": {"radius": .008, "height": .2}})
    return doc_id


def _volume_mm3(doc_id):
    return docs.get_document(doc_id).derived.volume * 1e9


def _vertical_edges(doc_id, length_mm=20.0):
    edges = ops.execute("list_edges", doc_id, {"kind": "line"})["result"]["edges"]
    return [e["id"] for e in edges if abs(e["length_mm"] - length_mm) < .01]


# ------------------------------------------------------------ seeing them --

def test_a_model_can_list_the_faces_it_may_select():
    result = ops.execute("list_faces", _bored_plate(), {})["result"]
    assert result["count"] == 7                      # six box faces + the bore wall
    kinds = sorted(f["kind"] for f in result["faces"])
    assert kinds == ["cylinder"] + ["plane"] * 6


def test_a_face_describes_itself_well_enough_to_be_told_apart():
    """An id alone is useless to a model; it has to know which one is the bore."""
    faces = ops.execute("list_faces", _bored_plate(), {})["result"]["faces"]
    bore = next(f for f in faces if f["kind"] == "cylinder")
    assert bore["radius_mm"] == pytest.approx(8.0)
    assert bore["center_mm"] == pytest.approx([0, 0, 0], abs=1e-6)


def test_edges_can_be_filtered_to_the_ones_worth_selecting():
    doc_id = _bored_plate()
    circles = ops.execute("list_edges", doc_id, {"kind": "circle"})["result"]
    assert circles["count"] == 2                     # top and bottom of the bore
    assert all(e["radius_mm"] == pytest.approx(8.0) for e in circles["edges"])

    long_only = ops.execute("list_edges", doc_id, {"min_length_mm": 40.0})["result"]
    assert all(e["length_mm"] >= 40.0 for e in long_only["edges"])


def test_names_are_stable_across_repeated_listing():
    doc_id = _bored_plate()
    first = [f["id"] for f in ops.execute("list_faces", doc_id, {})["result"]["faces"]]
    second = [f["id"] for f in ops.execute("list_faces", doc_id, {})["result"]["faces"]]
    assert first == second


def test_a_body_with_no_exact_form_says_so_instead_of_failing_obscurely():
    doc = docs.create_document(name="Shelled", resolution=32, owner_id=None)
    ops.dispatch("add_primitive", doc.id, {"shape": "box"})
    ops.dispatch("add_modifier", doc.id, {"modifier": "shell", "params": {"thickness": .004}})

    env = ops.execute("list_faces", doc.id, {})
    assert env["ok"] is False
    assert env["error"]["code"] == "PRECONDITION"
    assert "no addressable faces or edges" in env["error"]["message"]


# ------------------------------------------------------------- filleting --

def test_filleting_named_edges_removes_the_right_amount_of_material():
    """Four r=6 corners off a 20 mm-tall block: 4·(r² − πr²/4)·h."""
    doc_id = _plate()
    before = _volume_mm3(doc_id)

    env = ops.execute("fillet_edges", doc_id, {"edges": _vertical_edges(doc_id), "radius": .006})
    assert env["ok"], env.get("error")
    assert env["delta"]["changed_geometry"] is True

    removed = before - _volume_mm3(doc_id)
    assert removed == pytest.approx(4 * (36 - math.pi * 36 / 4) * 20, rel=0.01)


def test_a_fillet_is_a_feature_that_can_be_edited_afterwards():
    doc_id = _plate()
    env = ops.execute("fillet_edges", doc_id, {"edges": _vertical_edges(doc_id), "radius": .004})
    feature_id = env["result"]["feature_id"]

    small = _volume_mm3(doc_id)
    ops.execute("set_feature_params", doc_id, {"feature_id": feature_id, "params": {"radius": .008}})
    assert _volume_mm3(doc_id) < small          # a bigger radius removes more


def test_a_fillet_reference_survives_the_upstream_change_that_moves_its_edge():
    """The whole point of naming by descriptor rather than by index."""
    doc_id = _plate()
    root = docs.get_document(doc_id).feature_tree["root"]["id"]
    ops.execute("fillet_edges", doc_id, {"edges": _vertical_edges(doc_id), "radius": .004})

    # Make the plate longer: every vertical edge moves, none is destroyed.
    env = ops.execute("set_feature_params", doc_id,
                      {"feature_id": root, "params": {"size": V(.12, .05, .02)}})
    assert env["ok"], env.get("error")
    assert not env["warnings"], env["warnings"]

    doc = docs.get_document(doc_id)
    assert doc.derived.error is None
    assert doc.derived.bbox.max.x - doc.derived.bbox.min.x == pytest.approx(.12, abs=1e-4)


def test_a_radius_the_geometry_cannot_carry_says_why():
    """Not "install the exact kernel" when the kernel is installed and the radius is absurd."""
    doc_id = _plate()
    env = ops.execute("fillet_edges", doc_id, {"edges": _vertical_edges(doc_id), "radius": .5})
    message = " ".join(w["message"] for w in env["warnings"])
    assert "fillet" in message and "500" in message


def test_selecting_an_edge_that_does_not_exist_names_what_does():
    env = ops.execute("fillet_edges", _plate(), {"edges": ["Edeadbeef"], "radius": .002})
    assert env["ok"] is False
    assert env["error"]["code"] == "BAD_ARG"
    assert "list_edges" in env["error"]["message"]


def test_selecting_nothing_is_a_missing_argument():
    env = ops.execute("fillet_edges", _plate(), {"edges": [], "radius": .002})
    assert env["ok"] is False
    assert env["error"]["code"] == "MISSING_ARG"


def test_a_negative_radius_is_refused():
    env = ops.execute("fillet_edges", _plate(), {"edges": _vertical_edges(_plate()), "radius": -1})
    assert env["ok"] is False
    assert env["error"]["arg"] == "radius"


# ------------------------------------------------------------- chamfering --

def test_chamfering_named_edges_removes_the_right_amount():
    """Four 2 mm chamfers off the vertical corners: 4·(d²/2)·h."""
    doc_id = _plate()
    before = _volume_mm3(doc_id)
    env = ops.execute("chamfer_edges", doc_id, {"edges": _vertical_edges(doc_id), "distance": .002})
    assert env["ok"], env.get("error")
    removed = before - _volume_mm3(doc_id)
    assert removed == pytest.approx(4 * (2 * 2 / 2) * 20, rel=0.02)


# --------------------------------------------------------------- shelling --

def _top_face(doc_id):
    faces = ops.execute("list_faces", doc_id, {})["result"]["faces"]
    return [max((f for f in faces if f.get("normal") == [0.0, 0.0, 1.0]),
                key=lambda f: f["center_mm"][2])["id"]]


def test_shelling_through_a_named_face_leaves_it_open():
    """60x40x30 with 2 mm walls open at the top: 72000 - 56*36*28."""
    doc = docs.create_document(name="Box", resolution=48, owner_id=None)
    ops.dispatch("add_primitive", doc.id, {"shape": "box", "params": {"size": V(.06, .04, .03)}})

    env = ops.execute("shell_faces", doc.id, {"faces": _top_face(doc.id), "thickness": .002})
    assert env["ok"], env.get("error")
    assert _volume_mm3(doc.id) == pytest.approx(60 * 40 * 30 - 56 * 36 * 28, rel=0.01)


def test_a_shelled_body_is_still_a_valid_solid():
    doc = docs.create_document(name="Box", resolution=48, owner_id=None)
    ops.dispatch("add_primitive", doc.id, {"shape": "box", "params": {"size": V(.06, .04, .03)}})
    ops.execute("shell_faces", doc.id, {"faces": _top_face(doc.id), "thickness": .002})

    report = ops.execute("check_geometry", doc.id, {"resolution": 64})["result"]
    assert report["valid"] is True


def test_the_real_shell_differs_from_the_uniform_one():
    """`shell` hollows everything and closes it; `shell_faces` leaves a way in."""
    uniform = docs.create_document(name="U", resolution=48, owner_id=None)
    ops.dispatch("add_primitive", uniform.id, {"shape": "box", "params": {"size": V(.06, .04, .03)}})
    ops.dispatch("add_modifier", uniform.id, {"modifier": "shell", "params": {"thickness": .004}})

    opened = docs.create_document(name="O", resolution=48, owner_id=None)
    ops.dispatch("add_primitive", opened.id, {"shape": "box", "params": {"size": V(.06, .04, .03)}})
    ops.execute("shell_faces", opened.id, {"faces": _top_face(opened.id), "thickness": .002})

    assert docs.get_document(uniform.id).derived.sampler != "brep"
    assert docs.get_document(opened.id).derived.sampler == "brep"


# ------------------------------------------------------- kernel selection --

def test_an_exactly_representable_body_is_evaluated_exactly():
    assert docs.get_document(_bored_plate()).derived.sampler == "brep"


def test_a_body_the_exact_kernel_cannot_build_falls_back_silently():
    doc = docs.create_document(name="SheetMetal", resolution=32, owner_id=None)
    ops.dispatch("add_sheet_metal", doc.id, {"thickness": .0015, "length": .1, "width": .06})
    derived = docs.get_document(doc.id).derived
    assert derived.sampler in ("native", "numpy")
    assert derived.error is None


def test_exact_evaluation_gives_the_true_volume_not_a_sampled_one():
    doc_id = _bored_plate()
    exact = 80 * 50 * 20 - math.pi * 64 * 20
    assert _volume_mm3(doc_id) == pytest.approx(exact, rel=1e-4)


def test_mass_properties_come_from_the_geometry_when_it_is_exact():
    result = ops.execute("mass_properties", _bored_plate(), {})["result"]
    assert result["exact"] is True
    # Exact to the precision the field is rounded to (6 decimals of cm^3, i.e.
    # a thousandth of a mm^3) — not "close", which is what a mesh would give.
    assert result["volume_cm3"] == pytest.approx((80 * 50 * 20 - math.pi * 64 * 20) / 1000,
                                                 abs=1e-6)


def test_the_new_operations_are_published_with_required_arguments():
    catalog = {e["name"]: e for e in ops.catalog()}
    for name, required in (("fillet_edges", {"edges", "radius"}),
                           ("chamfer_edges", {"edges", "distance"}),
                           ("shell_faces", {"faces", "thickness"})):
        assert set(catalog[name]["schema"]["required"]) == required
    assert catalog["list_faces"]["kind"] == "query"
    assert catalog["fillet_edges"]["kind"] == "mutate"

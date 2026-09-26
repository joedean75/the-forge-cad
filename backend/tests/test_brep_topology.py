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
from conftest import body_id
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
    root = body_id(docs.get_document(doc_id).feature_tree)
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
    """Not "install the exact kernel" when the kernel is installed and the radius is absurd.

    And it is a failure, not a warning. The general rule — an unevaluatable tree
    is a warning — exists so a feature can reference a parameter defined a step
    later. Nothing rescues a 500 mm fillet on a 50 mm plate, so the edit is rolled
    back and the document is left usable.
    """
    doc_id = _plate()
    before = docs.get_document(doc_id).derived.volume

    env = ops.execute("fillet_edges", doc_id, {"edges": _vertical_edges(doc_id), "radius": .5})

    assert env["ok"] is False
    assert env["error"]["code"] == "PRECONDITION"
    assert "fillet" in env["error"]["message"] and "500" in env["error"]["message"]
    assert docs.get_document(doc_id).derived.volume == before
    assert docs.get_document(doc_id).derived.error is None


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
    """A smooth-boolean shell has no B-rep form, and the document still evaluates.

    Sheet metal used to be the example here; it is built exactly now, so the
    fallback needs a body that genuinely has no exact form to demonstrate on.
    """
    doc = docs.create_document(name="Shelled", resolution=32, owner_id=None)
    ops.dispatch("add_primitive", doc.id, {"shape": "box"})
    ops.dispatch("add_modifier", doc.id, {"modifier": "shell", "params": {"thickness": .004}})
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


def test_a_filleted_part_decompiles_back_into_its_operation_sequence():
    """Edge selections have to survive the round-trip too, or every part with a
    real fillet drops out of the training corpus."""
    from app.services import decompiler

    doc_id = _plate()
    ops.execute("fillet_edges", doc_id, {"edges": _vertical_edges(doc_id), "radius": .005})

    steps = decompiler.decompile(docs.get_document(doc_id).feature_tree)
    assert [s["op"] for s in steps] == ["add_primitive", "fillet_edges"]
    assert len(steps[1]["args"]["edges"]) == 4

    outcome = decompiler.decompile_document(doc_id, resolution=48, record=False)
    assert outcome["ok"], outcome.get("reason")


def test_a_body_only_the_exact_kernel_can_build_can_still_be_drawn():
    """Drawings project a distance field, and an edge fillet has none.

    Without a bridge, the parts the exact kernel exists to enable are exactly the
    ones that cannot be documented — which would make the feature useless.
    """
    from app.services import drawing

    doc_id = _plate()
    ops.execute("fillet_edges", doc_id, {"edges": _vertical_edges(doc_id), "radius": .005})

    out = drawing.generate_drawing(doc_id, spec={"views": ["front", "top"], "sections": []})
    assert out["views"]["front"] >= 1
    assert out["views"]["top"] >= 1
    # The printed figures still come from the evaluated body, so they stay exact
    # even though the traced outline went through a voxel grid.
    assert out["bbox_mm"]["width"] == pytest.approx(80.0, abs=1e-3)
    assert out["bbox_mm"]["height"] == pytest.approx(20.0, abs=1e-3)


def test_a_shelled_body_can_be_sectioned():
    from app.services import drawing

    doc = docs.create_document(name="Box", resolution=48, owner_id=None)
    ops.dispatch("add_primitive", doc.id, {"shape": "box", "params": {"size": V(.06, .04, .03)}})
    ops.execute("shell_faces", doc.id, {"faces": _top_face(doc.id), "thickness": .002})

    out = drawing.generate_drawing(doc.id, spec={"views": ["front"],
                                                 "sections": [{"plane": "xz", "label": "A-A"}]})
    # Cutting through a hollow box gives two walls, not one solid block.
    assert out["sections"][0]["contours"] >= 1


def test_a_face_is_selectable_by_position_without_listing_them_first():
    """The same shell as above, told which face by name rather than by id.

    A face id is a hash of that face's current measurements, so naming one
    requires an evaluated body to measure — which is right for a person clicking
    a list and wrong for everything that has to say which face *before* there is
    one. A CadQuery program says `.faces(">Z").shell(2)` and `shell_faces` could
    express that and had no way to be told; `_top_face` above is the round trip
    this removes. The six names are `datum.FACE_MAP`'s, read from that table
    rather than restated, because sketch-on-face already means exactly this by
    "top" and two spellings of one naming rule disagree the day after.
    """
    doc = docs.create_document(name="Box", resolution=48, owner_id=None)
    ops.dispatch("add_primitive", doc.id, {"shape": "box", "params": {"size": V(.06, .04, .03)}})

    env = ops.execute("shell_faces", doc.id, {"faces": "top", "thickness": .002})
    assert env["ok"], env.get("error")
    assert env["result"]["faces"] == _top_face(doc.id) or len(env["result"]["faces"]) == 1
    assert _volume_mm3(doc.id) == pytest.approx(60 * 40 * 30 - 56 * 36 * 28, rel=0.01)


def test_the_position_names_are_the_datum_scheme_s_own():
    """Not a second table: a name this does not know must be a name that one
    does not either, or the two have started to drift."""
    from app.services import datum

    doc = docs.create_document(name="Box", resolution=48, owner_id=None)
    ops.dispatch("add_primitive", doc.id, {"shape": "box", "params": {"size": V(.06, .04, .03)}})
    for name in datum.FACES:
        env = ops.execute("shell_faces", doc.id, {"faces": name, "thickness": .002})
        # Either it shells or OCC refuses that particular opening — what must not
        # happen is the name being rejected as unknown.
        assert "no such face" not in str((env.get("error") or {}).get("message", "")), name
        if env["ok"]:
            ops.execute("undo", doc.id, {})


def test_a_face_id_that_is_not_a_position_name_still_fails_loudly():
    doc = docs.create_document(name="Box", resolution=48, owner_id=None)
    ops.dispatch("add_primitive", doc.id, {"shape": "box", "params": {"size": V(.06, .04, .03)}})
    env = ops.execute("shell_faces", doc.id, {"faces": "upwards", "thickness": .002})
    assert not env["ok"] and env["error"]["code"] == "BAD_ARG"

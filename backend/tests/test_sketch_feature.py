"""A sketch as a thing rather than an argument.

A sketch used to exist only inside `ExtrudeFeature.params.sketch`: it could not
be shared, could not exist unconsumed, could not be revolved, and could not be
placed on any face the exact kernel knew about. `SketchFeature` is the sketch
itself — geometry other features consume, contributing no body of its own.

The pointed tests here are the ones about *sharing*: one outline driving two
features, and an edit moving both. A copy would pass every geometric assertion
on the day it was made and drift apart on the next edit.
"""

import math

import pytest

from app.cad_kernel.core.feature_tree import walk_nodes
from app.cad_kernel.evaluator import brep
from app.services import cad_operations as ops
from app.services import decompiler
from app.services import document_service as docs
from app.services import feature_ops

CIRCLE = {
    "points": [{"id": "c", "x": 0.0, "y": 0.0}],
    "circles": [{"id": "k", "center": "c", "radius": 0.02}],
    "constraints": [{"type": "fixed", "point": "c", "x": 0.0, "y": 0.0},
                    {"type": "radius", "circle": "k", "value": 0.02}],
}

# A 5 x 30 rectangle standing at radius 15..20 — a ring when revolved about z.
RING_PROFILE = {
    "points": [{"id": "a", "x": 0.015, "y": -0.015}, {"id": "b", "x": 0.02, "y": -0.015},
               {"id": "c", "x": 0.02, "y": 0.015}, {"id": "e", "x": 0.015, "y": 0.015}],
    "lines": [{"id": "l1", "a": "a", "b": "b"}, {"id": "l2", "a": "b", "b": "c"},
              {"id": "l3", "a": "c", "b": "e"}, {"id": "l4", "a": "e", "b": "a"}],
    "constraints": [],
}


def _circle(radius: float) -> dict:
    spec = {"points": [{"id": "c", "x": 0.0, "y": 0.0}],
            "circles": [{"id": "k", "center": "c", "radius": radius}],
            "constraints": [{"type": "fixed", "point": "c", "x": 0.0, "y": 0.0},
                            {"type": "radius", "circle": "k", "value": radius}]}
    return spec


def _doc(name="sketch feature"):
    return docs.create_document(name=name).id


def _volume_mm3(doc_id) -> float:
    return ops.execute("mass_properties", doc_id, {})["result"]["volume_cm3"] * 1000.0


# ------------------------------------------------------------- the feature --

def test_a_sketch_can_exist_with_nothing_consuming_it():
    doc_id = _doc()
    result = ops.execute("create_sketch", doc_id, {"sketch": CIRCLE})
    assert result["ok"], result["error"]
    assert result["result"]["status"] == "well_constrained"

    listed = ops.execute("list_features", doc_id, {})["result"]["features"]
    assert any(f["type"] == "SketchFeature" for f in listed)


def test_a_sketch_contributes_no_geometry():
    """It is geometry other features consume, so on its own the part is empty."""
    doc_id = _doc()
    ops.execute("create_sketch", doc_id, {"sketch": CIRCLE})
    size = ops.execute("bounding_box", doc_id, {})["result"]["size_mm"]
    # The same 1 mm stand-in an all-suppressed tree gets — not a 40 mm disc.
    assert (size["x"], size["y"], size["z"]) == (1.0, 1.0, 1.0)


def test_a_sketch_never_reaches_an_evaluator():
    from app.cad_kernel.ops.sketch import SketchFeature, SketchNotConsumed

    with pytest.raises(SketchNotConsumed, match="no body of its own"):
        SketchFeature(id="f9").evaluate()


def test_resolve_sketches_takes_the_node_out_of_the_evaluation_tree():
    doc_id = _doc()
    sketch_id = ops.execute("create_sketch", doc_id, {"sketch": CIRCLE})["result"]["feature_id"]
    ops.execute("add_extrude", doc_id, {"sketch": sketch_id, "depth": 0.01})

    doc = docs.get_document(doc_id)
    assert any(n["type"] == "SketchFeature" for n in walk_nodes(doc.feature_tree))
    resolved = docs.resolved_tree(doc)
    assert not [n for n in walk_nodes(resolved) if n["type"] == "SketchFeature"]
    # And the boolean it was a child of collapsed, leaving the extrude alone.
    assert resolved["root"]["type"] == "ExtrudeFeature"


# ------------------------------------------------------------------ sharing --

def test_one_sketch_drives_an_extrude_and_a_revolve():
    doc_id = _doc()
    sketch_id = ops.execute("create_sketch", doc_id, {"sketch": RING_PROFILE})["result"]["feature_id"]
    assert ops.execute("add_extrude", doc_id, {"sketch": sketch_id, "depth": 0.01})["ok"]
    revolved = ops.execute("add_revolve", doc_id, {"sketch": sketch_id, "axis": "z"})
    assert revolved["ok"], revolved["error"]

    consumers = ops.execute("update_sketch", doc_id,
                            {"feature_id": sketch_id, "sketch": RING_PROFILE})
    assert len(consumers["result"]["consumers"]) == 2


def test_editing_a_shared_sketch_moves_every_feature_that_reads_it():
    """The whole point. A copied profile would pass on the day it was made."""
    doc_id = _doc()
    sketch_id = ops.execute("create_sketch", doc_id, {"sketch": _circle(0.02)})["result"]["feature_id"]
    ops.execute("add_extrude", doc_id, {"sketch": sketch_id, "depth": 0.01})
    assert _volume_mm3(doc_id) == pytest.approx(math.pi * 400 * 10, rel=1e-3)

    ops.execute("update_sketch", doc_id, {"feature_id": sketch_id, "sketch": _circle(0.01)})
    assert _volume_mm3(doc_id) == pytest.approx(math.pi * 100 * 10, rel=1e-3)


def test_a_consumer_naming_a_feature_that_is_not_a_sketch_is_refused():
    doc_id = _doc()
    box = ops.execute("add_primitive", doc_id, {"shape": "box"})["result"]["feature_id"]
    result = ops.execute("add_extrude", doc_id, {"sketch": box, "depth": 0.01})
    assert not result["ok"]
    assert result["error"]["code"] == "NO_SUCH_FEATURE"


def test_sketch_ids_are_deterministic():
    """Trajectory replay compares trees, so a sketch id must not come from a clock."""
    def build():
        doc_id = _doc()
        first = ops.execute("create_sketch", doc_id, {"sketch": CIRCLE})["result"]["feature_id"]
        second = ops.execute("create_sketch", doc_id, {"sketch": RING_PROFILE})["result"]["feature_id"]
        return first, second

    assert build() == build()


# --------------------------------------------------------- placement on a face --

exact_only = pytest.mark.skipif(not brep.AVAILABLE, reason="OCP (OpenCascade) not installed")


def _plate_with_sketch_on_top(thickness=0.02):
    doc_id = _doc("on face")
    box = ops.execute("add_primitive", doc_id, {
        "shape": "box", "params": {"size": {"x": 0.08, "y": 0.06, "z": thickness}}})
    box_id = box["result"]["feature_id"]
    faces = ops.execute("list_faces", doc_id, {"feature": box_id})["result"]["faces"]
    top = next(f for f in faces
               if f.get("normal") and abs(f["normal"][2]) > 0.99 and f["center_mm"][2] > 0)
    sketch = ops.execute("create_sketch", doc_id, {
        "sketch": _circle(0.005),
        "on_face": {"feature_id": box_id, "face_id": top["id"]}})
    assert sketch["ok"], sketch["error"]
    return doc_id, box_id, sketch["result"]["feature_id"], top


@exact_only
def test_list_faces_can_be_scoped_to_one_feature():
    """The id space `on_face.face_id` is resolved in — a sketch is placed on a
    face of the feature it names, not of the whole body."""
    doc_id = _doc()
    box_id = ops.execute("add_primitive", doc_id, {"shape": "box"})["result"]["feature_id"]
    ops.execute("add_primitive", doc_id, {"shape": "sphere", "params": {"radius": 0.08}})

    whole = ops.execute("list_faces", doc_id, {})["result"]
    scoped = ops.execute("list_faces", doc_id, {"feature": box_id})["result"]
    assert scoped["count"] == 6 and scoped["feature"] == box_id
    assert whole["count"] != scoped["count"]


@exact_only
def test_a_sketch_placed_on_a_named_face_sits_on_it():
    doc_id, _, sketch_id, top = _plate_with_sketch_on_top()
    ops.execute("add_extrude", doc_id, {"sketch": sketch_id, "depth": 0.01})
    size = ops.execute("bounding_box", doc_id, {})["result"]["size_mm"]
    assert size["z"] == pytest.approx(25.0)      # 20 plate + 5 of boss above it
    assert _volume_mm3(doc_id) == pytest.approx(80 * 60 * 20 + math.pi * 25 * 5, rel=1e-3)


@exact_only
def test_the_sketch_follows_the_face_through_a_resize():
    doc_id, box_id, sketch_id, _ = _plate_with_sketch_on_top()
    ops.execute("add_extrude", doc_id, {"sketch": sketch_id, "depth": 0.01})

    ops.execute("set_feature_params", doc_id,
                {"feature_id": box_id, "params": {"size": {"x": 0.12, "y": 0.06, "z": 0.04}}})
    size = ops.execute("bounding_box", doc_id, {})["result"]["size_mm"]
    assert (size["x"], size["z"]) == (120.0, 45.0), "the boss stayed on the top face"


@exact_only
def test_a_sketch_plane_that_lost_its_face_is_reported_not_relocated():
    """The reference reports the loss rather than the sketch landing somewhere
    plausible — which is the entire reason the naming scheme exists."""
    doc_id = _doc("split")
    box = ops.execute("add_primitive", doc_id, {
        "shape": "box", "params": {"size": {"x": 0.08, "y": 0.06, "z": 0.02}}})
    slot = ops.execute("add_primitive", doc_id, {
        "shape": "box", "params": {"size": {"x": 0.2, "y": 0.008, "z": 0.004},
                                   "center": {"x": 0, "y": 0.024, "z": 0.01}},
        "operation": "subtract", "target": box["result"]["feature_id"]})
    group = slot["result"]["group_id"]

    faces = ops.execute("list_faces", doc_id, {"feature": group})["result"]["faces"]
    top = max((f for f in faces
               if f.get("normal") and abs(f["normal"][2]) > 0.99 and f["center_mm"][2] > 5),
              key=lambda f: f["area_mm2"])
    sketch = ops.execute("create_sketch", doc_id, {
        "sketch": _circle(0.005), "on_face": {"feature_id": group, "face_id": top["id"]}})
    ops.execute("add_extrude", doc_id, {"sketch": sketch["result"]["feature_id"], "depth": 0.01})

    clean = ops.execute("check_references", doc_id, {})["result"]
    assert clean["ok"] and clean["lost"] == 0

    # Move the slot to the middle: the face the sketch sits on is cut in two.
    ops.execute("set_feature_params", doc_id, {
        "feature_id": slot["result"]["feature_id"],
        "params": {"size": {"x": 0.2, "y": 0.008, "z": 0.004},
                   "center": {"x": 0, "y": 0.0, "z": 0.01}}})
    after = ops.execute("check_references", doc_id, {})["result"]
    assert not after["ok"] and after["lost"] == 1
    row = next(f for f in after["features"] if f["type"] == "SketchFeature")
    assert row["references"][0]["status"] in ("split", "deleted", "unresolved")


@exact_only
def test_a_face_id_that_names_nothing_is_refused_at_authoring_time():
    doc_id = _doc()
    box_id = ops.execute("add_primitive", doc_id, {"shape": "box"})["result"]["feature_id"]
    result = ops.execute("create_sketch", doc_id, {
        "sketch": CIRCLE, "on_face": {"feature_id": box_id, "face_id": "Fnope"}})
    assert not result["ok"]
    assert result["error"]["code"] == "BAD_ARG"


@exact_only
def test_a_curved_face_is_not_a_sketch_plane():
    doc_id = _doc()
    cyl = ops.execute("add_primitive", doc_id, {
        "shape": "cylinder", "params": {"radius": 0.03, "height": 0.05}})["result"]["feature_id"]
    faces = ops.execute("list_faces", doc_id, {"feature": cyl})["result"]["faces"]
    curved = next(f for f in faces if f["kind"] == "cylinder")
    result = ops.execute("create_sketch", doc_id, {
        "sketch": CIRCLE, "on_face": {"feature_id": cyl, "face_id": curved["id"]}})
    assert not result["ok"]
    assert "planar" in result["error"]["message"]


# --------------------------------------------------------------- decompiler --

def test_a_shared_sketch_decompiles_to_one_create_and_two_references():
    """Two sketches would rebuild the same outline twice and lose the sharing,
    and the replayed tree would not hash-match."""
    doc_id = _doc()
    sketch_id = ops.execute("create_sketch", doc_id, {"sketch": CIRCLE})["result"]["feature_id"]
    ops.execute("add_extrude", doc_id, {"sketch": sketch_id, "depth": 0.01})
    ops.execute("add_extrude", doc_id, {"sketch": sketch_id, "depth": 0.03})

    steps = decompiler.decompile(docs.get_document(doc_id).feature_tree)
    assert [s["op"] for s in steps] == ["create_sketch", "add_extrude", "add_extrude"]
    assert steps[1]["args"]["sketch"] == {"$step": 0}
    assert steps[2]["args"]["sketch"] == {"$step": 0}
    assert "profile" not in steps[1]["args"], "the sketch supplies it"


@pytest.mark.parametrize("build", [
    lambda d: (ops.execute("add_extrude", d, {"sketch": _sid(d, CIRCLE), "depth": 0.01}),),
    lambda d: (ops.execute("add_revolve", d, {"sketch": _sid(d, RING_PROFILE), "axis": "z"}),),
], ids=["extrude", "revolve"])
def test_a_sketch_driven_document_replays_to_the_same_geometry(build):
    doc_id = _doc()
    build(doc_id)
    result = decompiler.decompile_document(doc_id, record=False)
    assert result["ok"], f"{result.get('cause')}: {result.get('reason')}"


def _sid(doc_id, spec):
    return ops.execute("create_sketch", doc_id, {"sketch": spec})["result"]["feature_id"]


def test_sketch_consumers_finds_the_features_reading_a_sketch():
    doc_id = _doc()
    sketch_id = _sid(doc_id, CIRCLE)
    ops.execute("add_extrude", doc_id, {"sketch": sketch_id, "depth": 0.01})
    tree = docs.get_document(doc_id).feature_tree
    assert [n["type"] for n in feature_ops.sketch_consumers(tree, sketch_id)] == ["ExtrudeFeature"]
    assert feature_ops.sketch_consumers(tree, "nope") == []


def test_the_observation_reports_reference_ids_not_whole_descriptors():
    """An agent reading state back wants the id it would pass; the descriptor is
    what has to be *stored*, and ten fields apiece buries the dimensions."""
    from app.services import observation

    trimmed = observation.trim_params({
        "radius": 0.002,
        "edges": [{"id": "E1", "kind": "line", "length": 0.08, "at": [0, 0, 0]},
                  {"id": "E2", "kind": "line", "length": 0.06, "at": [0, 1, 0]}],
        "on_face": {"feature_id": "f1", "face_id": "F9",
                    "face_ref": {"id": "F9", "kind": "plane", "area": 0.0048}},
        "sketch": {"points": []},
    })
    assert trimmed == {"radius": 0.002, "edges": ["E1", "E2"],
                       "on_face": {"feature_id": "f1", "face_id": "F9"}}

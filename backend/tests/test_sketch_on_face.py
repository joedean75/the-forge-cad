"""Sketch-on-face datum bridge — the pragmatic topological-naming layer.

A sketch placed on another feature's face stores a symbolic {feature_id, face}
reference and re-resolves its plane/offset from that feature's live geometry at
regeneration, so it follows the face when the base feature is resized.
"""

import pytest

from app.services import cad_operations, document_service as ds, feature_ops, datum


def _square_sketch(half=0.01):
    return {
        "points": [
            {"id": "a", "x": -half, "y": -half},
            {"id": "b", "x": half, "y": -half},
            {"id": "c", "x": half, "y": half},
            {"id": "d", "x": -half, "y": half},
        ],
        "lines": [
            {"id": "l0", "a": "a", "b": "b"},
            {"id": "l1", "a": "b", "b": "c"},
            {"id": "l2", "a": "c", "b": "d"},
            {"id": "l3", "a": "d", "b": "a"},
        ],
        "constraints": [],
    }


def _boxed(name="t", size=0.1):
    """A document with one 100 mm box. A blank one is three datum planes and no
    body, so anything that wants a face to resolve has to make one first."""
    doc = ds.create_document(name=name)
    box_id = cad_operations.dispatch("add_primitive", doc.id, {
        "shape": "box", "params": {"size": {"x": size, "y": size, "z": size}}})["feature_id"]
    return ds.get_document(doc.id), box_id


def test_resolve_face_reads_live_bbox():
    doc, box_id = _boxed()
    plane, offset = datum.resolve_face(doc.feature_tree, box_id, "top")
    assert plane == "xy"
    assert offset == pytest.approx(0.05, abs=1e-3)
    _, bottom = datum.resolve_face(doc.feature_tree, box_id, "bottom")
    assert bottom == pytest.approx(-0.05, abs=1e-3)


def test_resolve_face_unknown_face_raises():
    doc, box_id = _boxed()
    with pytest.raises(ValueError):
        datum.resolve_face(doc.feature_tree, box_id, "sideways")


def test_sketch_on_face_follows_upstream_resize():
    doc, box_id = _boxed("onface")
    did = doc.id

    r = cad_operations.dispatch("add_sketch", did, {
        "sketch": _square_sketch(), "depth": 0.02,
        "on_face": {"feature_id": box_id, "face": "top"}, "operation": "union",
    })
    sfid = r["feature_id"]

    node = feature_ops.find_node(ds.get_document(did).feature_tree, sfid)
    assert node["params"]["plane"] == "xy"
    assert node["params"]["offset"] == pytest.approx(0.05, abs=1e-3)
    assert node["params"]["on_face"] == {"feature_id": box_id, "face": "top"}

    # Grow the box to 0.2 tall → top face moves to z = 0.10.
    cad_operations.dispatch("set_feature_params", did, {
        "feature_id": box_id, "params": {"size": {"x": 0.1, "y": 0.1, "z": 0.2}},
    })
    resolved = feature_ops.resolve_datums(
        feature_ops.resolve_suppressed(ds.get_document(did).feature_tree)
    )
    rn = feature_ops.find_node(resolved, sfid)
    assert rn["params"]["offset"] == pytest.approx(0.10, abs=2e-3)


def test_dangling_face_ref_keeps_fallback_offset():
    doc, box_id = _boxed()
    did = doc.id
    cad_operations.dispatch("add_sketch", did, {
        "sketch": _square_sketch(), "depth": 0.02, "offset": 0.05,
        "on_face": {"feature_id": box_id, "face": "top"}, "operation": "union",
    })
    # Point the ref at a non-existent feature; resolution must not raise and must
    # keep the last concrete offset.
    tree = ds.get_document(did).feature_tree
    node = feature_ops.find_node(tree, feature_ops.flatten(tree)[-1]["id"])
    node["params"]["on_face"] = {"feature_id": "does-not-exist", "face": "top"}
    resolved = feature_ops.resolve_datums(feature_ops.resolve_suppressed(tree))  # no raise
    assert resolved["root"] is not None

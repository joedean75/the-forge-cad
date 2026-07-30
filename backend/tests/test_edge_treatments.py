"""Whole-body fillet and chamfer: the operation that used to do nothing.

`add_modifier("fillet")` built a `FilletFeature` — a smooth boolean *between
bodies* — and wrapped the root in it. That leaves it with exactly one child, and
a smooth union of one body is that body, so `resolve_suppressed` collapsed the
feature away before anything evaluated it. The call returned `ok`, a feature id
and an unchanged volume.

For a human that is a puzzling non-event. For an agent being trained against
this surface it is worse: a transition that does not exist, reinforced by a
success signal.
"""

import pytest

from app.services import cad_operations as ops
from app.services import decompiler, document_service as docs
from app.cad_kernel.evaluator import brep

pytestmark = pytest.mark.skipif(
    not brep.AVAILABLE, reason="edge treatments need cadquery-ocp")


def _box(size: float = 0.05):
    doc = docs.create_document(name="Block", resolution=48, owner_id=None)
    ops.dispatch("add_primitive", doc.id,
                 {"shape": "box", "params": {"size": {"x": size, "y": size, "z": size}}})
    return doc.id


def _volume(doc_id: str) -> float:
    return docs.get_document(doc_id).derived.volume


def test_a_whole_body_fillet_removes_material():
    doc_id = _box()
    before = _volume(doc_id)

    result = ops.execute("add_modifier", doc_id,
                         {"modifier": "fillet", "params": {"radius": 0.005}})

    assert result["ok"], result.get("error")
    assert result["delta"]["changed_geometry"] is True
    assert _volume(doc_id) < before          # the whole point, and the old bug


def test_a_whole_body_fillet_builds_a_real_edge_fillet():
    doc_id = _box()
    ops.dispatch("add_modifier", doc_id, {"modifier": "fillet", "params": {"radius": 0.004}})

    types = [f["type"] for f in ops.dispatch("list_features", doc_id, {})["features"]]
    assert "EdgeFilletFeature" in types
    assert "FilletFeature" not in types      # the inert smooth-boolean version


def test_a_whole_body_fillet_rounds_every_edge():
    doc_id = _box()
    edges = ops.dispatch("list_edges", doc_id, {})["count"]

    result = ops.dispatch("add_modifier", doc_id,
                          {"modifier": "fillet", "params": {"radius": 0.004}})

    assert len(result["edges"]) == edges == 12


def test_a_whole_body_chamfer_removes_material():
    doc_id = _box()
    before = _volume(doc_id)

    result = ops.execute("add_modifier", doc_id,
                         {"modifier": "chamfer", "params": {"radius": 0.003}})

    assert result["ok"], result.get("error")
    assert _volume(doc_id) < before


def test_edges_all_is_the_shorthand_for_every_edge():
    doc_id = _box()

    result = ops.dispatch("fillet_edges", doc_id, {"edges": "all", "radius": 0.003})

    assert len(result["edges"]) == 12


def test_an_impossible_fillet_fails_and_leaves_the_body_alone():
    """A radius OCC cannot build is a precondition failure, not a warning.

    The general rule is that an unevaluatable tree is a warning — that is what
    lets a feature reference a parameter defined a step later. This is not that
    case: there is no later step that rescues a 500 mm fillet on a 50 mm cube,
    and leaving it in place would wedge every downstream operation.
    """
    doc_id = _box()
    before = _volume(doc_id)

    result = ops.execute("fillet_edges", doc_id, {"edges": "all", "radius": 0.5})

    assert result["ok"] is False
    assert result["error"]["code"] == "PRECONDITION"
    assert "500 mm fillet" in result["error"]["message"]
    assert _volume(doc_id) == before
    assert docs.get_document(doc_id).derived.error is None


def test_an_unknown_edge_id_names_what_is_available():
    doc_id = _box()

    result = ops.execute("fillet_edges", doc_id, {"edges": ["Ecafe"], "radius": 0.002})

    assert result["error"]["code"] == "BAD_ARG"
    assert "list_edges" in result["error"]["message"]


def test_the_decompiler_steps_through_an_inert_blend():
    """A single-child FilletFeature contributes nothing, so it emits no op —
    and the sequence still rebuilds the same body."""
    tree = {
        "version": "brep-v1",
        "root": {"type": "FilletFeature", "id": "f1", "name": "Fillet",
                 "params": {"radius": 0.004, "operation": "union", "children": [
                     {"type": "BoxFeature", "id": "f2", "name": "Box",
                      "params": {"size": {"x": 0.05, "y": 0.05, "z": 0.05},
                                 "center": {"x": 0, "y": 0, "z": 0}}}]}},
    }

    steps = decompiler.decompile(tree)

    assert [s["op"] for s in steps] == ["add_primitive"]


def test_the_decompiler_declines_a_real_blend():
    """Two children is a genuine smooth blend, and no operation builds one."""
    def _sphere(node_id, x):
        return {"type": "SphereFeature", "id": node_id, "name": "S",
                "params": {"radius": 0.03, "center": {"x": x, "y": 0, "z": 0}}}

    tree = {
        "version": "brep-v1",
        "root": {"type": "FilletFeature", "id": "f1", "name": "Blend",
                 "params": {"radius": 0.004, "operation": "union",
                            "children": [_sphere("f2", -0.02), _sphere("f3", 0.02)]}},
    }

    with pytest.raises(decompiler.Unsupported):
        decompiler.decompile(tree)

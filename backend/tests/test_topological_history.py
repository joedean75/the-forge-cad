"""Generative-history topological naming — following topology instead of re-measuring it.

`core/naming` matches an entity by *descriptor*: kind, size, and position
normalised to the body's bounding box. That is what makes a reference survive a
resize, and it is the only thing that can work for an imported body with no
construction history. What it cannot survive is an edit that **re-topologises**.

Two failures make the case, and they are the two the sprint was written against:

* Cut a slot across an edge a fillet was placed on, and the edge becomes two
  segments. A descriptor picks the nearer one — so half the fillet quietly
  disappears — or gives up. The right answer is "this reference now names both".
* Destroy an entity outright and a descriptor will happily find something a
  little like it nearby. `test_a_destroyed_reference_is_reported_deleted…` pins
  the case where that snap lands on a *different* edge half a millimetre away,
  which is exactly the silent mis-selection the scheme exists to prevent.

The rest guards the properties that make the mechanism safe to rely on: it is
deterministic, it is not a replacement for descriptor matching, and a builder
that reports nothing does not get read as a builder that reported deletion.
"""

import pytest

from app.cad_kernel.core import history, naming
from app.cad_kernel.evaluator import brep
from conftest import body_id
from app.services import cad_operations as ops
from app.services import document_service as docs
from app.services import feature_ops

pytestmark = pytest.mark.skipif(not brep.AVAILABLE, reason="OCP (OpenCascade) not installed")

V = lambda x, y, z: {"x": x, "y": y, "z": z}  # noqa: E731


def _plate(x=.08, y=.05, z=.02):
    doc = docs.create_document(name="Plate", resolution=48, owner_id=None)
    ops.dispatch("add_primitive", doc.id, {"shape": "box", "params": {"size": V(x, y, z)}})
    return doc.id


def _park(doc_id, size, center):
    """A subtract tool sitting clear of the part, so it can be moved onto it later.

    The tool has to be in the tree *before* the selection is made — the whole
    point is that an edit below an existing reference re-topologises what the
    reference names. Parked outside the body it cuts nothing, so the selection is
    taken on the plain plate.
    """
    ops.dispatch("add_primitive", doc_id, {"shape": "box", "operation": "subtract",
                                           "params": {"size": size, "center": center}})
    return [n for n in feature_ops.flatten(docs.get_document(doc_id).feature_tree)
            if n["type"] == "BoxFeature"][-1]["id"]


def _move(doc_id, feature_id, center):
    return ops.execute("set_feature_params", doc_id,
                       {"feature_id": feature_id, "params": {"center": center}})


def _statuses(doc_id):
    report = ops.execute("check_references", doc_id, {})["result"]
    return [row["status"] for f in report["features"] for row in f["references"]]


def _vertical_edges(doc_id, length_mm=20.0):
    edges = ops.execute("list_edges", doc_id, {"kind": "line"})["result"]["edges"]
    return [e["id"] for e in edges if abs(e["length_mm"] - length_mm) < .01]


def _top_edge_at_x(doc_id, x_mm=40.0, z_mm=10.0):
    edges = ops.execute("list_edges", doc_id, {})["result"]["edges"]
    return next(e for e in edges
                if abs(e["length_mm"] - 50) < .01
                and abs(e["midpoint_mm"][0] - x_mm) < .01
                and abs(e["midpoint_mm"][2] - z_mm) < .01)


# ------------------------------------------------------- what a tag names --

def test_a_selection_carries_an_identity_not_only_measurements():
    """The stored reference gains a tag: which feature made this, not what it looks like."""
    doc_id = _plate()
    reference = ops._selected(doc_id, _vertical_edges(doc_id)[:1], "edge")[0]

    assert reference["tag"].endswith((":edge:0", ":edge:1", ":edge:2", ":edge:3",
                                      ":edge:4", ":edge:5", ":edge:6", ":edge:7",
                                      ":edge:8", ":edge:9", ":edge:10", ":edge:11"))
    # The descriptor is still there. Tags are an addition, not a replacement — an
    # imported STEP body has no history and matches on measurements alone.
    assert reference["length_mm"] == pytest.approx(20.0)
    assert "at" in reference


def test_tags_are_deterministic_across_rebuilds():
    """Replay is only possible if the same document names the same entities."""
    first = _plate()
    second = _plate()
    tags = lambda d: sorted(  # noqa: E731
        e["tag"] for e in naming.edge_table(docs.get_exact_shape(d),
                                            docs.get_exact_body(d).entities))
    assert tags(first) == tags(second)


def test_an_entity_is_tagged_by_the_feature_that_created_it():
    """A bore wall belongs to the tool that cut it, so resizing the plate keeps it."""
    doc_id = _plate()
    ops.dispatch("add_primitive", doc_id, {"shape": "cylinder", "operation": "subtract",
                                           "params": {"radius": .008, "height": .2}})
    tool = next(n for n in feature_ops.flatten(docs.get_document(doc_id).feature_tree)
                if n["type"] == "CylinderFeature")

    body = docs.get_exact_body(doc_id)
    bore = next(f for f in naming.face_table(body.shape, body.entities)
                if f["kind"] == "cylinder")
    assert bore["tag"].startswith(f"{tool['id']}:")


# --------------------------------------------------- the re-topologise case --

def test_a_reference_to_an_edge_a_later_cut_splits_names_both_segments():
    """The case descriptors cannot answer, and the reason this sprint exists.

    A slot cut *below* an existing fillet divides two of the edges it was placed
    on. A descriptor match would round one segment of each and silently drop the
    other; following the history rounds both, and the body still rebuilds.
    """
    doc_id = _plate()
    tool = _park(doc_id, V(.1, .02, .006), V(0, .2, 0))
    ops.execute("fillet_edges", doc_id, {"edges": _vertical_edges(doc_id), "radius": .002})

    # Move the slot onto the y = +25 face: the two corner edges there are cut in
    # half, the other two are untouched.
    env = _move(doc_id, tool, V(0, .025, 0))
    assert env["ok"], env.get("error")
    assert not env["warnings"], env["warnings"]
    assert docs.get_document(doc_id).derived.error is None

    report = ops.execute("check_references", doc_id, {})["result"]
    assert report["split"] == 2
    assert report["lost"] == 0
    assert sorted(_statuses(doc_id)) == ["matched", "matched", "split", "split"]

    rows = report["features"][0]["references"]
    assert sorted(r["resolves_to"] for r in rows) == [1, 1, 2, 2]


def test_both_halves_of_a_split_edge_are_actually_filleted():
    """`resolves_to: 2` has to mean two rounded corners, not a nicer-looking report."""
    doc_id = _plate()
    tool = _park(doc_id, V(.1, .02, .006), V(0, .2, 0))
    ops.execute("fillet_edges", doc_id, {"edges": _vertical_edges(doc_id), "radius": .002})
    _move(doc_id, tool, V(0, .025, 0))

    # A fillet turns each sharp corner into a cylindrical face. Four corners were
    # selected, two of them are now in two pieces: six rounded segments.
    faces = ops.execute("list_faces", doc_id, {})["result"]["faces"]
    rounds = [f for f in faces
              if f["kind"] == "cylinder" and f["radius_mm"] == pytest.approx(2.0)]
    assert len(rounds) == 6


# ---------------------------------------------------------- the lost case --

def test_a_destroyed_reference_is_reported_deleted_not_snapped_to_a_neighbour():
    """History says gone; a descriptor would have found something 0.5 mm away.

    The tool machines 0.5 mm off the top of the plate's right-hand end, which
    destroys the edge the fillet was placed on and leaves a parallel one just
    below it. Normalised, that new edge sits 0.05 from where the old one was —
    inside `_MATCH_TOL`, so descriptor matching happily moves the fillet onto it.
    Being told the reference is *deleted* is the only answer that lets a caller
    fix the part rather than ship a differently-shaped one.
    """
    doc_id = _plate()
    target = _top_edge_at_x(doc_id)
    reference = ops._selected(doc_id, [target["id"]], "edge")[0]
    tool = _park(doc_id, V(.04, .06, .0045), V(.2, 0, .01175))
    ops.execute("fillet_edges", doc_id, {"edges": [target["id"]], "radius": .002})

    _move(doc_id, tool, V(.04, 0, .01175))
    assert _statuses(doc_id) == ["deleted"]

    # The same reference stripped of its tag — what the pre-history resolver saw —
    # lands on the edge half a millimetre lower.
    from app.cad_kernel.core.feature_tree import FeatureTree

    tree = FeatureTree.from_dict(docs.resolved_tree(docs.get_document(doc_id)))
    body = brep.brep_from_feature(tree.root.children[0])
    measured = naming.resolve_references(
        body, [{k: v for k, v in reference.items() if k != "tag"}], kind="edge")
    assert measured[0]["status"] == "matched"
    midpoint = naming.edge_descriptor(measured[0]["shapes"][0],
                                      naming.body_bounds(body))["midpoint_mm"]
    assert midpoint[2] == pytest.approx(9.5)


def test_losing_every_reference_says_what_happened_rather_than_failing_blankly():
    doc_id = _plate()
    target = _top_edge_at_x(doc_id)
    tool = _park(doc_id, V(.04, .06, .0045), V(.2, 0, .01175))
    ops.execute("fillet_edges", doc_id, {"edges": [target["id"]], "radius": .002})

    env = _move(doc_id, tool, V(.04, 0, .01175))
    # An unevaluatable tree is a warning, not a failure — that contract is
    # unchanged. What is new is that the warning names the cause.
    assert env["ok"] is True
    assert "destroyed by an upstream edit" in env["warnings"][0]["message"]

    report = ops.execute("check_references", doc_id, {})["result"]
    assert report["ok"] is False and report["lost"] == 1
    assert "reselect" in report["features"][0]["references"][0]["meaning"]


def test_losing_one_reference_of_several_still_warns_though_the_body_builds():
    """The quiet failure: the part is a valid solid, just not the one that was asked for.

    Two edges filleted, one of them later destroyed. The rebuild succeeds on the
    survivor, `check_geometry` is perfectly happy, and nothing in the geometry
    says a rounded edge went missing — so the operation envelope has to.
    """
    doc_id = _plate()
    edges = ops.execute("list_edges", doc_id, {})["result"]["edges"]
    top = [e["id"] for e in edges
           if abs(e["length_mm"] - 50) < .01 and abs(e["midpoint_mm"][2] - 10) < .01]
    assert len(top) == 2
    tool = _park(doc_id, V(.04, .06, .0045), V(.2, 0, .01175))
    ops.execute("fillet_edges", doc_id, {"edges": top, "radius": .002})

    env = _move(doc_id, tool, V(.04, 0, .01175))
    assert env["ok"] is True
    assert docs.get_document(doc_id).derived.error is None      # it built
    assert "no longer resolve" in env["warnings"][0]["message"]  # but say so
    assert sorted(_statuses(doc_id)) == ["deleted", "matched"]


def test_an_edit_that_loses_nothing_does_not_warn():
    doc_id = _plate()
    root = body_id(docs.get_document(doc_id).feature_tree)
    ops.execute("fillet_edges", doc_id, {"edges": _vertical_edges(doc_id), "radius": .003})
    env = ops.execute("set_feature_params", doc_id,
                      {"feature_id": root, "params": {"size": V(.09, .05, .02)}})
    assert env["warnings"] == []


def test_the_verdict_survives_the_build_failure_it_explains():
    """The diagnosis is collected on the way to the failure, so it must outlive it."""
    doc_id = _plate()
    target = _top_edge_at_x(doc_id)
    tool = _park(doc_id, V(.04, .06, .0045), V(.2, 0, .01175))
    ops.execute("fillet_edges", doc_id, {"edges": [target["id"]], "radius": .002})
    _move(doc_id, tool, V(.04, 0, .01175))

    assert docs.get_exact_shape(doc_id) is None       # there is no body to inspect
    verdicts, error = docs.reference_verdicts(doc_id)  # and the verdict is still there
    assert [r["status"] for f in verdicts for r in f["references"]] == ["deleted"]
    assert error


# ------------------------------------------------- what history does not do --

def test_descriptor_matching_still_carries_a_reference_through_a_resize():
    """The property the previous scheme bought is not traded away for this one."""
    doc_id = _plate()
    root = body_id(docs.get_document(doc_id).feature_tree)
    ops.execute("fillet_edges", doc_id, {"edges": _vertical_edges(doc_id), "radius": .004})

    env = ops.execute("set_feature_params", doc_id,
                      {"feature_id": root, "params": {"size": V(.12, .05, .02)}})
    assert env["ok"] and not env["warnings"]
    assert _statuses(doc_id) == ["matched"] * 4
    doc = docs.get_document(doc_id)
    assert doc.derived.bbox.max.x - doc.derived.bbox.min.x == pytest.approx(.12, abs=1e-4)


def test_a_reference_stored_before_tags_existed_still_resolves():
    """Documents persisted by an earlier build have descriptors and nothing else."""
    doc_id = _plate()
    ops.execute("fillet_edges", doc_id, {"edges": _vertical_edges(doc_id), "radius": .004})

    tree = docs.get_document(doc_id).feature_tree
    fillet = next(n for n in feature_ops.flatten(tree) if n["type"] == "EdgeFilletFeature")
    fillet["params"]["edges"] = [{k: v for k, v in r.items() if k != "tag"}
                                 for r in fillet["params"]["edges"]]
    docs.update_document(doc_id, feature_tree=tree)

    assert docs.get_document(doc_id).derived.sampler == "brep"
    assert _statuses(doc_id) == ["matched"] * 4

    box = next(n for n in feature_ops.flatten(docs.get_document(doc_id).feature_tree)
               if n["type"] == "BoxFeature")
    env = ops.execute("set_feature_params", doc_id,
                      {"feature_id": box["id"], "params": {"size": V(.12, .05, .02)}})
    assert env["ok"] and not env["warnings"]
    assert docs.get_document(doc_id).derived.error is None


def test_a_body_with_no_construction_history_still_resolves_by_measurement():
    """An imported STEP solid is one shape and one feature; descriptors are all it has."""
    from app.cad_kernel.ops.exact import BrepBodyFeature
    from app.cad_kernel.evaluator.brep import serialize

    doc_id = _plate()
    payload = serialize(docs.get_exact_shape(doc_id))
    imported = BrepBodyFeature(id="f1", payload=payload)

    shape, hist = brep.build_with_history(imported)
    assert shape is not None
    # Every entity is named by the import itself — there is no earlier feature to
    # attribute it to — and that is enough for a selection to resolve.
    tags = {e["tag"] for e in naming.edge_table(shape, hist["entities"])}
    assert tags and all(t.startswith("f1:") for t in tags)


def test_a_builder_that_reports_nothing_is_not_read_as_reporting_deletion():
    """`BRepFilletAPI` calls untouched box edges deleted; believing it would be a disaster.

    Every edge of a filleted box would come back destroyed, and the next
    operation on the part would refuse to run.
    """
    doc_id = _plate()
    corners = ops.execute("list_edges", doc_id, {"kind": "line"})["result"]["edges"]
    corners = [e for e in corners if abs(e["length_mm"] - 20) < .01]
    ops.execute("fillet_edges", doc_id, {"edges": [corners[0]["id"]], "radius": .003})
    body = docs.get_exact_body(doc_id)
    assert body.entities.removed == set()

    # And a still-sharp corner of the filleted body remains selectable. Reading
    # `IsDeleted` here would have reported all twelve box edges destroyed.
    opposite = ops.execute("list_edges", doc_id, {"kind": "line"})["result"]["edges"]
    far = next(e for e in opposite
               if abs(e["length_mm"] - 20) < .01
               and all(abs(a - b) > 1.0 for a, b in
                       zip(e["midpoint_mm"][:2], corners[0]["midpoint_mm"][:2])))
    env = ops.execute("chamfer_edges", doc_id, {"edges": [far["id"]], "distance": .001})
    assert env["ok"], env.get("error")
    assert "deleted" not in _statuses(doc_id)


def test_a_patterned_copy_is_a_different_entity_from_the_original():
    """Otherwise filleting one instance's corner would round all of them."""
    doc_id = _plate(.02, .02, .02)
    ops.dispatch("add_modifier", doc_id, {"modifier": "pattern",
                                          "params": {"kind": "linear", "count": 3,
                                                     "spacing": .05,
                                                     "axis": V(1, 0, 0)}})
    body = docs.get_exact_body(doc_id)
    table = naming.edge_table(body.shape, body.entities)
    assert len({e["tag"] for e in table}) == len(table)
    assert any(e["tag"].endswith("#1") for e in table)
    assert any(e["tag"].endswith("#2") for e in table)


def test_filleting_one_instance_of_a_pattern_rounds_only_that_instance():
    doc_id = _plate(.02, .02, .02)
    ops.dispatch("add_modifier", doc_id, {"modifier": "pattern",
                                          "params": {"kind": "linear", "count": 3,
                                                     "spacing": .05, "axis": V(1, 0, 0)}})
    edges = ops.execute("list_edges", doc_id, {"kind": "line"})["result"]["edges"]
    middle = next(e for e in edges
                  if abs(e["length_mm"] - 20) < .01 and abs(e["midpoint_mm"][0] - 50) < .01)

    env = ops.execute("fillet_edges", doc_id, {"edges": [middle["id"]], "radius": .002})
    assert env["ok"], env.get("error")
    rounds = [f for f in ops.execute("list_faces", doc_id, {})["result"]["faces"]
              if f["kind"] == "cylinder"]
    assert len(rounds) == 1
    assert rounds[0]["center_mm"][0] == pytest.approx(50.0, abs=1.0)


def test_a_reference_taken_before_a_pattern_is_replicated_with_the_body():
    """The fillet sits below the pattern, so every copy carries it — and still resolves."""
    doc_id = _plate(.02, .02, .02)
    edge = ops.execute("list_edges", doc_id, {"kind": "line"})["result"]["edges"][0]
    ops.execute("fillet_edges", doc_id, {"edges": [edge["id"]], "radius": .002})
    ops.dispatch("add_modifier", doc_id, {"modifier": "pattern",
                                          "params": {"kind": "linear", "count": 3,
                                                     "spacing": .05, "axis": V(1, 0, 0)}})
    rounds = [f for f in ops.execute("list_faces", doc_id, {})["result"]["faces"]
              if f["kind"] == "cylinder"]
    assert len(rounds) == 3
    assert _statuses(doc_id) == ["matched"]


# ------------------------------------------------------------ the surface --

def test_the_reference_check_is_published_as_a_query_operation():
    catalog = {e["name"]: e for e in ops.catalog()}
    assert catalog["check_references"]["kind"] == "query"
    assert catalog["check_references"]["schema"].get("required", []) == []


def test_a_body_with_no_exact_form_says_so():
    doc = docs.create_document(name="Shelled", resolution=32, owner_id=None)
    ops.dispatch("add_primitive", doc.id, {"shape": "box"})
    ops.dispatch("add_modifier", doc.id, {"modifier": "shell", "params": {"thickness": .004}})

    env = ops.execute("check_references", doc.id, {})
    assert env["ok"] is False
    assert env["error"]["code"] == "PRECONDITION"


def test_a_part_with_no_selections_reports_cleanly_rather_than_erroring():
    report = ops.execute("check_references", _plate(), {})["result"]
    assert report == {"features": [], "lost": 0, "split": 0, "ok": True,
                      "message": "every reference resolves"}


# ------------------------------------------------------------- the module --

def test_shape_keys_ignore_orientation():
    """A face and its reverse are one entity; keying them apart would break carry-over."""
    from OCP.TopAbs import TopAbs_ShapeEnum

    shape = docs.get_exact_shape(_plate())
    edge = history._explore(shape, "edge")[0]
    keys = history.ShapeKeys()
    assert keys.key(edge) == keys.key(edge.Reversed())
    assert TopAbs_ShapeEnum.TopAbs_EDGE == edge.ShapeType()


def test_a_split_reference_resolves_to_pieces_of_the_right_kind():
    """OCC's history hands back base shapes; `BRepFilletAPI.Add` will not take one."""
    from OCP.TopAbs import TopAbs_ShapeEnum

    doc_id = _plate()
    tool = _park(doc_id, V(.1, .02, .006), V(0, .2, 0))
    reference = ops._selected(doc_id, _vertical_edges(doc_id), "edge")
    _move(doc_id, tool, V(0, .025, 0))

    shape = docs.get_exact_shape(doc_id)
    outcomes = naming.resolve_references(shape, reference, kind="edge",
                                         entities=docs.get_exact_body(doc_id).entities)
    resolved = [s for o in outcomes for s in o["shapes"]]
    assert len(resolved) == 6
    assert all(s.ShapeType() == TopAbs_ShapeEnum.TopAbs_EDGE for s in resolved)

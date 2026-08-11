"""How much of the corpus can be turned into a verified demonstration?

`decompile_all` and `export_jsonl` worked on single documents long before anyone
ran them over a corpus and looked at the number. That number is the health
metric for the training-data half of this project: a decompiler that recovers
40% of what exists is a very different thing from one that recovers 95%, and
until it is measured nobody knows which they have.

These tests fix the metric in place. `corpus_report` is measured against a
corpus that covers **every feature type the catalog can build** — anything less
measures the easy population and reports a comfortable number — and separately
against the population that genuinely cannot be recovered, to check the report
names the culprit rather than shrugging.
"""

import pytest

from app.cad_kernel.evaluator import brep
from app.services import cad_operations as ops
from app.services import decompiler
from app.services import document_service as docs

V = lambda x, y, z: {"x": x, "y": y, "z": z}  # noqa: E731

exact_only = pytest.mark.skipif(not brep.AVAILABLE, reason="OCP (OpenCascade) not installed")


# Documents persist for the whole test session, so every report here is scoped
# to the ids the test itself created — otherwise a test that builds two
# undecompilable documents would measure them against everything left behind by
# the ones before it.
_MADE: list = []


def _plate(name):
    doc = docs.create_document(name=name, resolution=48, owner_id=None)
    ops.dispatch("add_primitive", doc.id, {"shape": "box", "params": {"size": V(.08, .05, .02)}})
    _MADE.append(doc.id)
    return doc.id


def _blank(name):
    doc_id = docs.create_document(name=name, resolution=48, owner_id=None).id
    _MADE.append(doc_id)
    return doc_id


@pytest.fixture(autouse=True)
def _fresh():
    _MADE.clear()
    yield


def _build_coverage_corpus():
    """One document per feature type, built the way the catalog builds them."""
    d = _plate("sphere")
    ops.dispatch("add_primitive", d, {"shape": "sphere", "operation": "union",
                                      "params": {"radius": .02}})
    d = _plate("cylinder")
    ops.dispatch("add_primitive", d, {"shape": "cylinder", "operation": "subtract",
                                      "params": {"radius": .008, "height": .2}})
    d = _plate("cone")
    ops.dispatch("add_primitive", d, {"shape": "cone", "operation": "union",
                                      "params": {"base_radius": .02, "height": .03}})
    d = _plate("hole")
    ops.dispatch("add_modifier", d, {"modifier": "hole", "params": {"radius": .005, "depth": .1}})
    d = _plate("shell")
    ops.dispatch("add_modifier", d, {"modifier": "shell", "params": {"thickness": .003}})
    d = _plate("pattern")
    ops.dispatch("add_modifier", d, {"modifier": "pattern",
                                     "params": {"kind": "linear", "count": 3,
                                                "spacing": .1, "axis": V(1, 0, 0)}})
    d = _plate("transform")
    ops.dispatch("add_modifier", d, {"modifier": "transform",
                                     "params": {"translate": V(.01, 0, 0),
                                                "rotate": V(0, 0, 15), "scale": 1.0}})
    d = _plate("mirror")
    ops.dispatch("add_mirror", d, {"plane": "yz", "offset": .06})

    ops.dispatch("add_extrude", _blank("extrude"),
                 {"profile": {"type": "rect", "width": .05, "height": .03}, "depth": .02})
    ops.dispatch("add_revolve", _blank("revolve"),
                 {"profile": {"type": "rect", "cx": .03, "width": .01, "height": .02}, "axis": "z"})
    ops.dispatch("add_sweep", _blank("sweep"),
                 {"path": [[0, 0, 0], [.05, 0, 0], [.05, .04, 0]], "radius": .005})
    ops.dispatch("add_loft", _blank("loft"),
                 {"profile_a": {"type": "rect", "width": .04, "height": .04},
                  "profile_b": {"type": "circle", "radius": .015}, "offset_b": .05})
    ops.dispatch("add_sheet_metal", _blank("sheet_metal"),
                 {"thickness": .0015, "length": .1, "width": .05,
                  "flanges": [{"edge": "x+", "angle": 90, "length": .02}]})

    if brep.AVAILABLE:
        ops.dispatch("add_ruled_loft", _blank("ruled_loft"),
                     {"profile_a": {"type": "rect", "width": .04, "height": .04},
                      "profile_b": {"type": "circle", "radius": .015}, "offset_b": .05})
        for name, op, key, size in (("fillet_edges", "fillet_edges", "radius", .003),
                                    ("chamfer_edges", "chamfer_edges", "distance", .002)):
            d = _plate(name)
            edges = ops.execute("list_edges", d, {"kind": "line"})["result"]["edges"]
            picked = [e["id"] for e in edges if abs(e["length_mm"] - 20) < .01]
            ops.execute(op, d, {"edges": picked, key: size})
        d = _plate("shell_faces")
        faces = ops.execute("list_faces", d, {})["result"]["faces"]
        top = max((f for f in faces if f.get("normal") == [0., 0., 1.]),
                  key=lambda f: f["center_mm"][2])
        ops.execute("shell_faces", d, {"faces": [top["id"]], "thickness": .002})


def test_every_feature_the_catalog_builds_decompiles_and_replays():
    """The metric, over a corpus that covers the whole feature space.

    A hit rate measured on whatever happens to be in the datastore flatters
    itself — the rule-based text-to-CAD parser emits mostly plain boxes, and a
    corpus of those scores 100% while telling you nothing. This builds one
    document per feature type first.
    """
    _build_coverage_corpus()
    report = decompiler.corpus_report(resolution=32, doc_ids=_MADE)

    assert report["documents"] >= 13
    assert report["hit_rate"] == 1.0, [
        (r["name"], r["cause"], r["reason"]) for r in report["results"] if not r["ok"]]
    assert report["by_cause"] == {"recovered": report["documents"]}
    # And every type present really was exercised, not merely absent.
    covered = {f["feature"] for f in report["by_feature"]}
    assert {"BoxFeature", "SphereFeature", "CylinderFeature", "ConeFeature",
            "ExtrudeFeature", "RevolveFeature", "SweepFeature", "LoftFeature",
            "SheetMetalFeature", "HoleFeature", "ShellFeature", "PatternFeature",
            "TransformFeature", "MirrorFeature", "BooleanFeature"} <= covered


@exact_only
def test_the_exact_kernel_features_are_covered_too():
    _build_coverage_corpus()
    report = decompiler.corpus_report(resolution=32, doc_ids=_MADE)
    covered = {f["feature"] for f in report["by_feature"]}
    assert {"EdgeFilletFeature", "EdgeChamferFeature", "FaceShellFeature",
            "RuledLoftFeature"} <= covered
    assert report["hit_rate"] == 1.0


# ------------------------------------------------ what genuinely cannot be --

def _hand_built(name, root):
    doc = docs.create_document(name=name, resolution=48, owner_id=None)
    docs.update_document(doc.id, feature_tree={"version": "brep-v1", "root": root})
    _MADE.append(doc.id)
    return doc.id


def _box(node_id, size=.04, center=None):
    return {"type": "BoxFeature", "id": node_id,
            "params": {"size": V(size, size, size), "center": center or V(0, 0, 0)}}


def test_the_report_names_the_feature_that_blocked_each_document():
    """"8% of documents are unrecoverable" is not actionable; the type is.

    `blocking` is the column that matters. Every type in an undecompilable
    document shows a 0% rate — a `BoxFeature` sitting next to an
    `OptimizeFeature` is dragged down with it — so the rate alone would send you
    off to fix boxes.
    """
    _hand_built("optimized", {"type": "OptimizeFeature", "id": "f1",
                              "params": {"children": [_box("f2")]}})
    _hand_built("blended", {"type": "FilletFeature", "id": "f1", "params": {
        "radius": .005, "children": [
            _box("f2"),
            {"type": "SphereFeature", "id": "f3",
             "params": {"radius": .025, "center": V(.02, 0, 0)}}]}})

    report = decompiler.corpus_report(resolution=32, doc_ids=_MADE)
    assert report["hit_rate"] == 0.0
    assert report["by_cause"] == {"unsupported": 2}

    blocking = {f["feature"]: f["blocking"] for f in report["by_feature"]}
    assert blocking["OptimizeFeature"] == 1
    assert blocking["FilletFeature"] == 1
    # Present in a failing document, but not the reason it failed.
    assert blocking["BoxFeature"] == 0
    assert {f["feature"] for f in report["by_feature"] if f["rate"] == 0.0} >= {"BoxFeature"}


def test_an_imported_body_says_it_was_imported_rather_than_unbuildable():
    """`BrepBodyFeature` used to fall through to "no operation builds" — true, but
    it reads as a missing emitter rather than as data no replay can hold."""
    _hand_built("step import", {"type": "BrepBodyFeature", "id": "f1",
                                "params": {"payload": "", "source": {}}})
    outcome = decompiler.decompile_document(_MADE[-1], resolution=32, record=False)
    assert outcome["failed_at"] == "BrepBodyFeature"
    assert "serialized B-rep payload" in outcome["reason"]


# --------------------------------------------- the gap the metric surfaced --

def test_a_nested_boolean_is_recovered_now_that_a_build_can_be_targeted():
    """`union(plate, subtract(boss, bore))` — a bore through a boss only.

    This was the one *fixable* thing the corpus report turned up: whole-tree
    generation produces the shape routinely, and no operation could express it,
    because every builder combined with the root. It is the difference between
    a hole in the boss and a hole through the whole part.
    """
    doc_id = _hand_built("boss with bore", {
        "type": "BooleanFeature", "id": "f1", "params": {"operation": "union", "children": [
            {"type": "BoxFeature", "id": "f2",
             "params": {"size": V(.1, .06, .01), "center": V(0, 0, 0)}},
            {"type": "BooleanFeature", "id": "f3", "params": {"operation": "subtract", "children": [
                {"type": "CylinderFeature", "id": "f4",
                 "params": {"radius": .02, "height": .03, "center": V(0, 0, .02), "axis": V(0, 0, 1)}},
                {"type": "CylinderFeature", "id": "f5",
                 "params": {"radius": .008, "height": .06, "center": V(0, 0, .02), "axis": V(0, 0, 1)}}]}}]}})

    steps = decompiler.decompile(docs.get_document(doc_id).feature_tree)
    assert [s["op"] for s in steps] == ["add_primitive"] * 3
    assert steps[2]["args"]["operation"] == "subtract"
    assert steps[2]["args"]["target"] == {"$step": 1}

    outcome = decompiler.decompile_document(doc_id, resolution=32, record=False)
    assert outcome["ok"], outcome.get("reason")


def test_a_third_body_joins_the_group_rather_than_nesting_another_pair():
    """Same body either way; different tree, and the tree is what is compared."""
    doc_id = _hand_built("boss, bore and slot", {
        "type": "BooleanFeature", "id": "f1", "params": {"operation": "union", "children": [
            {"type": "BoxFeature", "id": "f2",
             "params": {"size": V(.1, .06, .01), "center": V(0, 0, 0)}},
            {"type": "BooleanFeature", "id": "f3", "params": {"operation": "subtract", "children": [
                {"type": "CylinderFeature", "id": "f4",
                 "params": {"radius": .02, "height": .03, "center": V(0, 0, .02), "axis": V(0, 0, 1)}},
                {"type": "CylinderFeature", "id": "f5",
                 "params": {"radius": .008, "height": .06, "center": V(0, 0, .02), "axis": V(0, 0, 1)}},
                {"type": "BoxFeature", "id": "f6",
                 "params": {"size": V(.05, .004, .06), "center": V(0, 0, .03)}}]}}]}})

    steps = decompiler.decompile(docs.get_document(doc_id).feature_tree)
    assert steps[3]["args"]["target"] == {"$group": 2}
    assert decompiler.decompile_document(doc_id, resolution=32, record=False)["ok"]


def test_targeting_one_feature_leaves_the_rest_of_the_body_alone():
    """The capability itself, through the catalog — not just via a replay."""
    doc_id = _plate("targeted cut")
    boss = ops.dispatch("add_primitive", doc_id, {
        "shape": "cylinder", "operation": "union",
        "params": {"radius": .015, "height": .02, "center": V(0, 0, .02)}})
    before = docs.get_document(doc_id).derived.volume

    env = ops.execute("add_primitive", doc_id, {
        "shape": "cylinder", "operation": "subtract", "target": boss["feature_id"],
        "params": {"radius": .006, "height": .2, "center": V(0, 0, 0)}})
    assert env["ok"], env.get("error")
    assert env["result"]["group_id"]

    # A long bore aimed at the boss must not reach the plate below it.
    plate_only = .08 * .05 * .02
    after = docs.get_document(doc_id).derived.volume
    assert after < before
    assert after > plate_only * 0.99


def test_targeting_a_feature_that_does_not_exist_is_a_typed_failure():
    env = ops.execute("add_primitive", _plate("p"), {
        "shape": "sphere", "target": "nope", "params": {"radius": .01}})
    assert env["ok"] is False
    assert env["error"]["code"] in ("BAD_ARG", "NO_SUCH_FEATURE")


def test_every_builder_can_be_targeted():
    """A nested boolean's children are arbitrary bodies, not just primitives."""
    catalog = {e["name"]: e for e in ops.catalog()}
    for name in ("add_primitive", "add_extrude", "add_revolve", "add_sweep",
                 "add_loft", "add_ruled_loft", "add_sheet_metal"):
        assert "target" in catalog[name]["schema"]["properties"], name


def test_an_unreplayable_document_is_counted_not_a_crash():
    """`corpus_report` exists to count the documents that do not decompile, so
    one of them must not take it down.

    `Unsupported` is raised from two places: `decompile`, which was guarded, and
    `replay` via `resolve_refs` when a step targets a group no earlier step
    created — which was not. A single such document aborted the whole report
    with a traceback, and it is exactly the kind of document the report is
    measuring.
    """
    from app.services import cad_operations as ops
    from app.services import decompiler
    from app.services import document_service as docs

    doc = docs.create_document(name="unreplayable", resolution=32, owner_id=None)
    ops.execute("add_primitive", doc.id, {"shape": "box"})

    # A step whose `$group` reference cannot resolve is what escaped before.
    steps = [{"op": "add_primitive", "args": {"shape": "box"}},
             {"op": "add_primitive", "args": {"shape": "sphere",
                                              "target": {"$group": 7}}}]
    with pytest.raises(decompiler.Unsupported):
        decompiler.replay(steps, doc.id)

    # The report survives whatever is in the store and still reports a number.
    report = decompiler.corpus_report(resolution=32, record=False)
    assert report["documents"] >= 1
    assert report["recovered"] <= report["documents"]
    assert sum(report["by_cause"].values()) == report["documents"]

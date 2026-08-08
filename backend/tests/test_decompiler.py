"""Recovering the operation sequence that builds an existing tree.

Text-to-CAD emits a whole feature tree in one shot, so the highest-volume
generation path exercises the action space not at all and yields no tool-use
data. Decompiling turns every tree already in the datastore into a trajectory.

The property that matters is that it never lies: a sequence is only offered as
training data if replaying it reproduces the original geometry exactly.
"""

import pytest

from app.services import cad_operations as ops
from app.services import decompiler
from app.services import document_service as docs
from app.services import trajectories
from app.services.decompiler import Unsupported


def _doc(name="decomp"):
    return docs.create_document(name=name, resolution=32, owner_id=None)


def _built(*calls):
    """A document built through the operation API, and its id."""
    doc = _doc()
    for op, args in calls:
        env = ops.execute(op, doc.id, args)
        assert env["ok"], env.get("error")
    return doc.id


# --------------------------------------------------------- shape of the ops --

def test_a_single_primitive_decompiles_to_one_call():
    doc_id = _built(("add_primitive", {"shape": "box",
                                       "params": {"size": {"x": .08, "y": .04, "z": .01}}}))
    steps = decompiler.decompile(docs.get_document(doc_id).feature_tree)
    assert [s["op"] for s in steps] == ["add_primitive"]
    assert steps[0]["args"]["shape"] == "box"
    assert steps[0]["args"]["params"]["size"] == {"x": .08, "y": .04, "z": .01}


def test_a_subtraction_keeps_its_operation():
    doc_id = _built(
        ("add_primitive", {"shape": "box", "params": {"size": {"x": .06, "y": .06, "z": .06}}}),
        ("add_primitive", {"shape": "cylinder", "operation": "subtract",
                           "params": {"radius": .01, "height": .2}}),
    )
    steps = decompiler.decompile(docs.get_document(doc_id).feature_tree)
    assert steps[1]["args"]["operation"] == "subtract"
    # A union is the default, so it is left off rather than stated redundantly.
    assert "operation" not in steps[0]["args"]


def test_a_whole_body_modifier_needs_no_target():
    doc_id = _built(
        ("add_primitive", {"shape": "box"}),
        ("add_modifier", {"modifier": "shell", "params": {"thickness": .002}}),
    )
    steps = decompiler.decompile(docs.get_document(doc_id).feature_tree)
    assert [s["op"] for s in steps] == ["add_primitive", "add_modifier"]
    assert "target" not in steps[1]["args"]
    assert steps[1]["args"]["params"]["thickness"] == .002


def test_a_whole_body_fillet_decompiles_as_the_edge_op_that_built_it():
    """`add_modifier("fillet")` builds an `EdgeFilletFeature`, so that is what
    comes back — not the smooth-boolean modifier the name used to imply."""
    doc_id = _built(
        ("add_primitive", {"shape": "box"}),
        ("add_modifier", {"modifier": "fillet", "params": {"radius": .002}}),
    )
    steps = decompiler.decompile(docs.get_document(doc_id).feature_tree)
    assert [s["op"] for s in steps] == ["add_primitive", "fillet_edges"]
    assert steps[1]["args"]["radius"] == .002
    assert len(steps[1]["args"]["edges"]) == 12


def test_a_targeted_modifier_refers_to_the_step_that_created_its_feature():
    """The id a feature gets is only known at replay, so the reference is symbolic."""
    doc = _doc()
    ops.execute("add_primitive", doc.id, {"shape": "box"})
    env = ops.execute("add_primitive", doc.id, {"shape": "cylinder"})
    ops.execute("add_modifier", doc.id, {"modifier": "pattern", "target": env["result"]["feature_id"],
                                         "params": {"count": 3, "spacing": .02}})
    steps = decompiler.decompile(docs.get_document(doc.id).feature_tree)
    target = steps[-1]["args"]["target"]
    assert target == {"$step": 1}


def test_an_extrude_decompiles_through_its_own_operation():
    doc_id = _built(("add_extrude", {"profile": {"type": "circle", "radius": .02, "cx": 0, "cy": 0},
                                     "depth": .01}))
    steps = decompiler.decompile(docs.get_document(doc_id).feature_tree)
    assert steps[0]["op"] == "add_extrude"
    assert steps[0]["args"]["profile"]["type"] == "circle"


# ------------------------------------------------------------- round trips --

@pytest.mark.parametrize("calls", [
    pytest.param([("add_primitive", {"shape": "box"})], id="box"),
    pytest.param([("add_primitive", {"shape": "sphere", "params": {"radius": .03}})], id="sphere"),
    pytest.param([
        ("add_primitive", {"shape": "box", "params": {"size": {"x": .08, "y": .05, "z": .02}}}),
        ("add_primitive", {"shape": "cylinder", "operation": "subtract",
                           "params": {"radius": .008, "height": .2}}),
    ], id="plate-with-hole"),
    pytest.param([
        ("add_primitive", {"shape": "box"}),
        ("add_modifier", {"modifier": "fillet", "params": {"radius": .003}}),
    ], id="filleted-box"),
    pytest.param([
        ("add_primitive", {"shape": "box", "params": {"size": {"x": .06, "y": .06, "z": .02}}}),
        ("add_primitive", {"shape": "cylinder", "params": {"radius": .01, "height": .04}}),
        ("add_primitive", {"shape": "cylinder", "operation": "subtract",
                           "params": {"radius": .004, "height": .2}}),
    ], id="boss-and-bore"),
    pytest.param([("add_revolve", {"profile": {"type": "rect", "width": .01, "height": .03,
                                               "cx": .02, "cy": 0}})], id="revolve"),
    pytest.param([("add_sheet_metal", {"thickness": .0015, "length": .1, "width": .06})],
                 id="sheet-metal"),
])
def test_decompiled_sequences_rebuild_the_same_geometry(calls):
    """The self-verifying property, across the shapes the kernel can express."""
    doc_id = _built(*calls)
    outcome = decompiler.decompile_document(doc_id, resolution=32, record=False)
    assert outcome["ok"], outcome.get("reason")
    assert outcome["n_steps"] == len(calls)


def test_a_text_to_cad_document_decompiles_even_though_it_was_never_built_by_ops():
    """The whole point: recovering data from the path that produces none."""
    doc = docs.create_document(prompt="a 60x30x5 mm plate", resolution=32, owner_id=None)
    outcome = decompiler.decompile_document(doc.id, resolution=32, record=False)
    assert outcome["ok"], outcome.get("reason")


def test_decompiling_leaves_the_original_document_untouched():
    doc_id = _built(("add_primitive", {"shape": "box"}))
    before = docs.get_document(doc_id).feature_tree
    decompiler.decompile_document(doc_id, resolution=32, record=False)
    assert docs.get_document(doc_id).feature_tree == before


def test_the_scratch_document_is_cleaned_up():
    doc_id = _built(("add_primitive", {"shape": "box"}))
    before = {s.id for s in docs.list_documents()}
    decompiler.decompile_document(doc_id, resolution=32, record=False)
    assert {s.id for s in docs.list_documents()} == before


# ---------------------------------------------------------- honest failure --

def test_an_opaque_body_is_reported_rather_than_approximated():
    """An ImportFeature is a cached voxel grid; no operation rebuilds it."""
    tree = {"version": "brep-v1", "root": {
        "type": "ImportFeature", "id": "f1", "params": {"cache": {}, "source": {}}}}
    with pytest.raises(Unsupported, match="voxel grid"):
        decompiler.decompile(tree)


def test_an_unknown_feature_type_is_refused():
    tree = {"version": "brep-v1", "root": {"type": "SomeFutureFeature", "id": "f1", "params": {}}}
    with pytest.raises(Unsupported, match="no operation builds"):
        decompiler.decompile(tree)


def test_a_document_with_an_opaque_body_reports_unsupported_not_a_crash():
    doc = _doc()
    docs.update_document(doc.id, feature_tree={"version": "brep-v1", "root": {
        "type": "ImportFeature", "id": "f1", "params": {"cache": {}, "source": {}}}})
    outcome = decompiler.decompile_document(doc.id, resolution=32, record=False)
    assert outcome["ok"] is False
    assert outcome["unsupported"] is True


def test_a_trajectory_that_fails_to_reproduce_is_discarded(monkeypatch):
    """A demonstration that rebuilds the wrong part teaches the wrong thing."""
    doc_id = _built(("add_primitive", {"shape": "box"}))
    hashes = iter(["expected", "something-else"])
    monkeypatch.setattr(decompiler, "_hash_tree_geometry", lambda *a, **k: next(hashes))

    before = {r["id"] for r in trajectories.list_all()}
    outcome = decompiler.decompile_document(doc_id, resolution=32, record=True)

    assert outcome["ok"] is False
    assert "differs from the original" in outcome["reason"]
    assert {r["id"] for r in trajectories.list_all()} == before


# ------------------------------------------------------- trajectory output --

def test_a_verified_decompile_is_recorded_as_a_trajectory():
    doc = docs.create_document(prompt="a 40x20x4 mm bracket", resolution=32, owner_id=None)
    outcome = decompiler.decompile_document(doc.id, resolution=32, record=True)
    assert outcome["ok"], outcome.get("reason")

    record = trajectories.get(outcome["trajectory_id"])
    assert record["kind"] == "decompiled"
    # The goal is the document's own prompt: a (prompt -> op sequence) example.
    assert record["goal"] == "a 40x20x4 mm bracket"
    assert len(record["steps"]) == outcome["n_steps"]
    assert all(s["ok"] for s in record["steps"])


def test_the_recorded_steps_are_real_executions_not_a_fabricated_transcript():
    doc_id = _built(("add_primitive", {"shape": "box"}))
    outcome = decompiler.decompile_document(doc_id, resolution=32, record=True)
    step = trajectories.get(outcome["trajectory_id"])["steps"][0]
    # Observed state and a measured latency only exist if the op actually ran.
    assert step["obs_after"]["features"]
    assert step["delta"]["features"]["added"]
    assert step["latency_ms"] >= 0


def test_bulk_decompile_reports_what_it_recovered():
    _built(("add_primitive", {"shape": "box"}))
    report = decompiler.decompile_all(resolution=32, limit=3)
    assert report["total"] >= 1
    assert report["recovered"] <= report["total"]


def test_decompiling_over_http_returns_the_verified_sequence():
    from fastapi.testclient import TestClient

    from main import app

    client = TestClient(app)
    doc = docs.create_document(prompt="a 50x25x3 mm plate", resolution=32, owner_id=None)
    r = client.post("/api/trajectories/decompile", params={"doc_id": doc.id, "resolution": 32})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["n_steps"] >= 1
    assert trajectories.get(body["trajectory_id"])["kind"] == "decompiled"


# -------------------------------------------------------------- parameters --

def test_a_parameterised_document_decompiles_and_keeps_its_parameters():
    """Design intent used to make a document unrecoverable.

    The stored tree holds the *expression* — that is what a parameter is — so
    the emitted `add_primitive` carried `"=length"` into `params.size.x`, which
    the schema refuses because a size is a number. Every document with a
    parameter in it was therefore recorded as `replay_failed`. The recovery is
    the sequence a person performs: declare, build with literals, then bind.
    """
    doc_id = _built(
        ("set_parameters", {"parameters": [
            {"name": "length", "expr": "80", "unit": "mm"},
            {"name": "thickness", "expr": "length/10", "unit": "mm"}]}),
        ("add_primitive", {"shape": "box",
                           "params": {"size": {"x": .08, "y": .04, "z": .008}}}),
    )
    from app.cad_kernel.core.feature_tree import walk_nodes

    box = next(n for n in walk_nodes(docs.get_document(doc_id).feature_tree)
               if n.get("type") == "BoxFeature")
    ops.execute("set_feature_params", doc_id, {
        "feature_id": box["id"],
        "params": {"size": {"x": "=length", "y": .04, "z": "=thickness"}}})

    outcome = decompiler.decompile_document(doc_id, resolution=32, record=False)
    assert outcome["ok"], outcome.get("reason")
    emitted = [s["op"] for s in outcome["steps"]]
    assert emitted[0] == "set_parameters", emitted
    assert emitted[-1] == "set_feature_params", emitted
    # Nothing numeric may carry an expression string: the schema is what a
    # constrained decoder reads.
    build = next(s for s in outcome["steps"] if s["op"] == "add_primitive")
    assert isinstance(build["args"]["params"]["size"]["x"], float)


def test_prepending_the_parameter_table_moves_every_reference_with_it():
    """A `{"$step": i}` counts from position zero, and the prologue lands in
    front of every geometry step. An unshifted reference targets the step before
    the one it means — which is a *valid* op on the wrong feature, so replay
    would succeed and the verification would have to catch it."""
    doc_id = _built(
        ("set_parameters", {"parameters": [{"name": "r", "expr": "6", "unit": "mm"}]}),
        ("add_primitive", {"shape": "box",
                           "params": {"size": {"x": .05, "y": .05, "z": .02}}}),
        ("add_primitive", {"shape": "cylinder", "operation": "subtract",
                           "params": {"radius": .006, "height": .06}}),
    )
    outcome = decompiler.decompile_document(doc_id, resolution=32, record=False)
    assert outcome["ok"], outcome.get("reason")
    targets = [s["args"]["target"] for s in outcome["steps"] if "target" in s["args"]]
    # Step 0 is now `set_parameters`, so the first body is step 1.
    assert all(t != {"$step": 0} for t in targets), outcome["steps"]

"""Tests for Forge Copilot — the agentic operation loop (roadmap feature 01)."""

import json
import pytest

from fastapi.testclient import TestClient

from main import app
from app.services import cad_operations, copilot

client = TestClient(app)


@pytest.mark.parametrize("subject", ["documents", "assemblies"])
def test_followup_history_reaches_every_planner_step_with_a_fresh_budget(monkeypatch, subject):
    """Exercise both HTTP routes through to actual provider messages, after a
    budget stop. Historical actions must not consume this run's action budget."""
    subject_id = _new_doc() if subject == "documents" else _assembly()
    sent = _capture_messages(monkeypatch, [{"op": "unknown_operation", "args": {}}])
    path = f"/api/{subject}/{subject_id}/copilot"
    first = client.post(path, json={"goal": "Build a NEMA 17 rig with a 100 mm base", "max_steps": 1})
    assert first.status_code == 200, first.text
    previous = client.get(f"{path}/{first.json()['job_id']}").json()
    assert previous["stop_reason"] == "budget"
    history = ("User: Build a NEMA 17 rig with a 100 mm base\nCopilot: "
               + previous["summary"] + "\nOperations: " + json.dumps(previous["steps"]))
    sent.clear()
    followup = client.post(path, json={"goal": "Continue", "max_steps": 2, "history": history})
    assert followup.status_code == 200, followup.text
    outcome = client.get(f"{path}/{followup.json()['job_id']}").json()
    assert len(outcome["steps"]) == 2
    assert len(sent) == 2
    for messages in sent:
        assert history in messages[-1]["content"]
        assert "Goal: Continue" in messages[-1]["content"]
        assert "do not repeat them blindly" in messages[-1]["content"]
        assert history not in messages[0]["content"]
    assert "Steps so far in this run:\n(none yet)" in sent[0][-1]["content"]
    # A new chat sends no history: it must not inherit a preceding request's.
    sent.clear()
    fresh = client.post(path, json={"goal": "An unrelated task", "max_steps": 1})
    assert fresh.status_code == 200
    assert "NEMA 17" not in sent[0][-1]["content"]


@pytest.mark.parametrize("subject", ["documents", "assemblies"])
def test_oversized_conversation_is_rejected_before_starting_a_job(subject):
    subject_id = _new_doc() if subject == "documents" else _assembly()
    response = client.post(f"/api/{subject}/{subject_id}/copilot",
                           json={"goal": "continue", "history": "x" * 60001})
    assert response.status_code == 422


def test_conversation_context_and_reference_images_are_sent_together(monkeypatch):
    sent = _capture_messages(monkeypatch, [{"done": True, "summary": "done"}])
    copilot.run(_new_doc(), "Use this reference", history="User: 100 mm base", images=[_PNG])
    content = sent[0][-1]["content"]
    assert "User: 100 mm base" in content[0]["text"]
    # Each image is introduced by its own label, so a render arriving later in
    # the run cannot be mistaken for the user's reference.
    assert "Image 1" in content[1]["text"]
    assert content[2]["image_url"]["url"] == _PNG


def _new_doc() -> str:
    r = client.post("/api/documents", json={"resolution": 48})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _scripted(goal, transcript, summary, **_):
    """A deterministic stand-in for the LLM planner: plate, then a hole, then done.

    Takes `**_` because it stands in for two things: the three-argument
    `planner` protocol, and `_plan_next` itself, which is also told the subject.
    """
    n = len([s for s in transcript if s.get("kind") == "op"])
    if n == 0:
        return {"op": "add_primitive", "rationale": "base plate",
                "args": {"shape": "box", "operation": "union",
                         "params": {"size": {"x": 0.1, "y": 0.06, "z": 0.02}, "center": {"x": 0, "y": 0, "z": 0}}}}
    if n == 1:
        return {"op": "add_primitive", "rationale": "corner hole",
                "args": {"shape": "cylinder", "operation": "subtract",
                         "params": {"radius": 0.006, "height": 0.05, "center": {"x": 0.03, "y": 0.0, "z": 0}}}}
    return {"done": True, "summary": "Built a mounting plate with a hole."}


def test_copilot_run_executes_a_plan_directly():
    doc_id = _new_doc()
    out = copilot.run(doc_id, "a 100x60x20 mm plate with a hole", planner=_scripted)
    ops = [s for s in out["steps"] if s.get("kind") == "op"]
    assert len(ops) == 2
    assert all(s["ok"] for s in ops)
    assert "plate" in out["summary"].lower()
    assert out["document"] is not None
    assert "/mesh/" in out["document"].derived.mesh_url


def test_copilot_stops_at_max_steps():
    doc_id = _new_doc()
    # A planner that never says done — the loop must be bounded.
    forever = lambda goal, t, s: {"op": "bounding_box", "args": {}, "rationale": "peek"}
    out = copilot.run(doc_id, "loop", planner=forever, max_steps=3)
    assert len([s for s in out["steps"] if s.get("kind") == "op"]) == 3


def test_copilot_records_operation_errors_without_crashing():
    doc_id = _new_doc()
    once = {"n": 0}

    def bad(goal, transcript, summary):
        if once["n"] == 0:
            once["n"] = 1
            return {"op": "set_feature_params", "args": {"feature_id": "nope", "params": {}}}
        return {"done": True, "summary": "done"}

    out = copilot.run(doc_id, "x", planner=bad)
    op = [s for s in out["steps"] if s.get("kind") == "op"][0]
    assert op["ok"] is False and op["error"]


def test_copilot_endpoint_runs_scripted_plan(monkeypatch):
    monkeypatch.setattr(copilot, "_plan_next", _scripted)
    doc_id = _new_doc()
    r = client.post(f"/api/documents/{doc_id}/copilot", json={"goal": "plate with a hole"})
    assert r.status_code == 200, r.text
    start = r.json()
    assert start["job_id"]
    # TestClient runs background tasks before returning, so one poll resolves it.
    r = client.get(f"/api/documents/{doc_id}/copilot/{start['job_id']}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["status"] == "done"
    ok_ops = [s for s in body["steps"] if s.get("kind") == "op" and s.get("ok")]
    assert len(ok_ops) == 2
    assert "/mesh/" in body["document"]["derived"]["mesh_url"]
    # Version history is fetched via /versions, never inlined in job responses.
    assert "version_history" not in body["document"]


def test_copilot_endpoint_requires_goal():
    doc_id = _new_doc()
    r = client.post(f"/api/documents/{doc_id}/copilot", json={"goal": "   "})
    assert r.status_code == 422


def test_copilot_endpoint_unknown_document_404():
    r = client.post("/api/documents/does-not-exist/copilot", json={"goal": "x"})
    assert r.status_code == 404


def test_done_is_rechecked_and_never_accepts_unmet_requirements(monkeypatch):
    monkeypatch.setattr(copilot, "_unmet_requirements", lambda _: ["Width is incorrect"])
    out = copilot.run(_new_doc(), "width", planner=lambda *a: {"done": True, "summary": "All done"})
    assert out["finished"] is False
    assert out["stop_reason"] == "verification_failed"
    assert "Goal not confirmed complete" in out["summary"]


def test_repeated_unknown_operation_is_bounded():
    out = copilot.run(_new_doc(), "goal", max_steps=16,
                      planner=lambda *a: {"op": "unknown_operation", "args": {}})
    assert out["stop_reason"] == "repeated_failure"
    assert len([s for s in out["steps"] if s["kind"] == "op"]) == 3


def test_cancel_during_planning_prevents_mutation():
    flag = {"cancel": False}
    def planner(*args):
        flag["cancel"] = True
        return {"op": "add_primitive", "args": {"shape": "box"}}
    out = copilot.run(_new_doc(), "goal", planner=planner, cancelled=lambda: flag["cancel"])
    assert out["stop_reason"] == "cancelled" and not out["finished"]
    assert out["steps"] == []


def test_unconfigured_is_not_success():
    out = copilot.run(_new_doc(), "goal")
    assert not out["finished"] and out["stop_reason"] == "unconfigured"


def test_result_summary_is_bounded_and_removes_nested_geometry():
    import json
    result = copilot._trim_result({"nested": {"feature_tree": "x"*100000},
                                  "rows": ["x"*10000]*1000})
    assert len(json.dumps(result)) < 12000
    assert "feature_tree" not in json.dumps(result)


def test_cancel_requires_matching_document():
    from app.routers.operations import _COPILOT_JOBS
    doc_id, other = _new_doc(), _new_doc()
    # `subject_id`, not `doc_id`: the job store now holds assembly runs too, and
    # the key names whichever kind of subject the run is against.
    _COPILOT_JOBS["cancel-test"] = {"id": "cancel-test", "subject_id": doc_id,
                                    "subject": "document", "status": "running", "steps": []}
    assert client.post(f"/api/documents/{other}/copilot/cancel-test/cancel").status_code == 404
    assert client.post(f"/api/documents/{doc_id}/copilot/cancel-test/cancel").status_code == 200
    assert _COPILOT_JOBS["cancel-test"]["cancel_requested"] is True


def test_invalid_geometry_cannot_finish_without_a_spec(monkeypatch):
    execute = copilot.cad_operations.execute
    def checked(op, *args, **kwargs):
        if op == "check_geometry":
            return {"ok": True, "result": {"valid": False, "failed": ["watertight"]}}
        return execute(op, *args, **kwargs)
    monkeypatch.setattr(copilot.cad_operations, "execute", checked)
    out = copilot.run(_new_doc(), "plate", planner=_scripted)
    assert not out["finished"]
    assert out["stop_reason"] == "verification_failed"


# ---------------------------------------------------------------------------
# The assembly subject: the same loop against the other half of the catalog.
# ---------------------------------------------------------------------------

def _assembly() -> str:
    return client.post("/api/assemblies", json={"name": "rig"}).json()["id"]


def _part() -> str:
    from conftest import base_box

    doc_id = _new_doc()
    base_box(client, doc_id, size=(0.1, 0.06, 0.02), center=(0.0, 0.0, 0.0))
    return doc_id


def test_the_copilot_composes_an_assembly():
    """The loop is the same; what changes is which id it is handed and therefore
    which operations exist. Inserting a part and reading the components back is
    the smallest run that proves the assembly path is wired end to end."""
    asm_id, part_id = _assembly(), _part()

    def scripted(goal, transcript, summary, **_):
        n = len([s for s in transcript if s.get("kind") == "op"])
        if n == 0:
            return {"op": "insert_part", "args": {"part_id": part_id},
                    "rationale": "the plate"}
        if n == 1:
            return {"op": "list_components", "args": {}, "rationale": "check it landed"}
        return {"done": True, "summary": "Placed the plate."}

    out = copilot.run(asm_id, "put the plate in", planner=scripted, subject="assembly")
    ops = [s for s in out["steps"] if s.get("kind") == "op"]
    assert len(ops) == 2 and all(o["ok"] for o in ops), ops
    # The subject comes back under its own name, and the other one is empty.
    assert out["assembly"] is not None and out["document"] is None
    assert len(out["assembly"].components) == 1
    assert out["finished"] is True


def test_the_planner_is_only_offered_the_operations_that_take_this_id():
    """A planner driving an assembly that is handed the document catalog calls it,
    and every call is refused against an assembly id — the budget goes on
    discovering what was never applicable."""
    from app.services import action_space

    assembly = {t["function"]["name"] for t in action_space.tools("assembly")}
    document = {t["function"]["name"] for t in action_space.tools("document")}
    assert "insert_part" in assembly and "add_hole" not in assembly
    assert "add_hole" in document and "insert_part" not in document
    assert not (assembly & document)

    prompt = action_space.system_prompt(with_tools=False, subject="assembly")
    assert "check_interference" in prompt and "add_hole" not in prompt


def test_a_part_operation_against_an_assembly_says_which_it_is():
    """`execute` would refuse this as a missing document, which is a confusing
    answer to a real mistake: the id is fine, the operation is for another kind
    of thing."""
    asm_id = _assembly()

    def scripted(goal, transcript, summary, **_):
        if not transcript:
            return {"op": "add_hole", "args": {}, "rationale": "wrong subject"}
        return {"done": True, "summary": "stopped"}

    out = copilot.run(asm_id, "drill it", planner=scripted, subject="assembly")
    bad = [s for s in out["steps"] if s.get("kind") == "op"][0]
    assert bad["ok"] is False
    assert "operates on a document" in bad["error"]


def test_interference_is_the_assemblys_check_before_it_claims_done(monkeypatch):
    """A part is checked for a valid solid; an assembly is checked for parts
    passing through each other, which is the failure that looks correct in every
    other reading it has."""
    asm_id, part_id = _assembly(), _part()
    execute = copilot.cad_operations.execute

    def checked(op, *args, **kwargs):
        if op == "check_interference":
            return {"ok": True, "result": {"clear": False, "interferences": 2}}
        return execute(op, *args, **kwargs)

    monkeypatch.setattr(copilot.cad_operations, "execute", checked)

    def scripted(goal, transcript, summary, **_):
        if not [s for s in transcript if s.get("kind") == "op"]:
            return {"op": "insert_part", "args": {"part_id": part_id}, "rationale": "part"}
        return {"done": True, "summary": "placed"}

    out = copilot.run(asm_id, "stack them", planner=scripted, subject="assembly")
    checks = [s for s in out["steps"] if s.get("kind") == "verification"]
    assert checks and checks[0]["op"] == "check_interference"
    assert checks[0]["ok"] is False
    # And it does not get to claim success with parts intersecting.
    assert out["finished"] is False
    assert "2 pair(s)" in " ".join(out["unmet"])


def test_the_assembly_copilot_endpoint_runs_a_scripted_plan(monkeypatch):
    asm_id, part_id = _assembly(), _part()

    def scripted(goal, transcript, summary, **_):
        if not [s for s in transcript if s.get("kind") == "op"]:
            return {"op": "insert_part", "args": {"part_id": part_id}, "rationale": "part"}
        return {"done": True, "summary": "Placed one part."}

    monkeypatch.setattr(copilot, "_plan_next", scripted)
    r = client.post(f"/api/assemblies/{asm_id}/copilot", json={"goal": "place the plate"})
    assert r.status_code == 200, r.text
    job = r.json()["job_id"]
    body = client.get(f"/api/assemblies/{asm_id}/copilot/{job}").json()
    assert body["ok"] is True and body["status"] == "done"
    # The assembly comes back on the assembly route; the document field stays empty.
    assert body["assembly"] is not None and body["document"] is None
    assert len(body["assembly"]["components"]) == 1


def test_an_assembly_job_id_does_not_resolve_through_another_assembly():
    from app.routers.operations import _COPILOT_JOBS

    mine, theirs = _assembly(), _assembly()
    _COPILOT_JOBS["asm-cancel"] = {"id": "asm-cancel", "subject_id": mine,
                                   "subject": "assembly", "status": "running", "steps": []}
    assert client.post(f"/api/assemblies/{theirs}/copilot/asm-cancel/cancel").status_code == 404
    assert client.post(f"/api/assemblies/{mine}/copilot/asm-cancel/cancel").status_code == 200
    assert _COPILOT_JOBS["asm-cancel"]["cancel_requested"] is True


def test_a_document_job_id_does_not_resolve_through_the_assembly_route():
    """The two stores are one dict, so the subject is part of the identity."""
    from app.routers.operations import _COPILOT_JOBS

    asm_id = _assembly()
    _COPILOT_JOBS["doc-job"] = {"id": "doc-job", "subject_id": asm_id,
                                "subject": "document", "status": "running", "steps": []}
    assert client.post(f"/api/assemblies/{asm_id}/copilot/doc-job/cancel").status_code == 404


# ---------------------------------------------------------------------------
# build_part: the assembly loop designs the parts it needs
# ---------------------------------------------------------------------------

def _box_planner(*, size=(0.08, 0.05, 0.01)):
    """A part planner that builds one box and stops — a stand-in for the part
    copilot's own LLM, so an assembly build is deterministic in tests."""
    def plan(goal, transcript, summary, **_):
        if [x for x in transcript if x.get("kind") == "op"]:
            return {"done": True, "summary": "built a block"}
        return {"op": "add_primitive", "rationale": "the body",
                "args": {"shape": "box", "operation": "union",
                         "params": {"size": dict(zip("xyz", size)),
                                    "center": {"x": 0, "y": 0, "z": 0}}}}
    return plan


def test_build_part_designs_a_part_and_places_it(monkeypatch):
    """The recursion that makes a whole assembly possible: one assembly step
    runs a whole part run, and what it produces is a component."""
    monkeypatch.setattr(copilot, "_plan_next", _box_planner())
    asm_id = _assembly()
    env = cad_operations.execute("build_part", asm_id,
                                 {"goal": "an 80 x 50 x 10 mm plate", "name": "plate"})
    assert env["ok"], env.get("error")
    result = env["result"]
    assert result["component_count"] == 1
    assert result["part_finished"] is True and result["part_steps"] == 1

    # The part is a real document with a real body, not a placeholder.
    from app.services import document_service, feature_ops
    built = document_service.get_document(result["part_id"])
    assert feature_ops.has_solid(built.feature_tree)
    assert built.derived.volume > 0


def test_a_part_that_never_got_a_body_is_not_placed(monkeypatch):
    """A component whose part has no body is a BOM row and an empty viewport —
    harder to notice than a refusal, so the empty document goes with it."""
    monkeypatch.setattr(copilot, "_plan_next",
                        lambda goal, transcript, summary, **_: {"done": True, "summary": "did nothing"})
    asm_id = _assembly()
    env = cad_operations.execute("build_part", asm_id, {"goal": "a bracket"})
    assert env["ok"] is False
    assert env["error"]["code"] == "PRECONDITION"

    from app.services import assembly_service, document_service
    assert assembly_service.get_assembly(asm_id).components == []
    # And no empty document was left behind in the library.
    named = [d for d in document_service.list_documents() if d.name == "a bracket"]
    assert named == []


def test_the_nested_part_run_is_charged_to_the_same_account(monkeypatch):
    """Otherwise every operation a part run performs is free, and the cheapest
    way to spend somebody's quota is to ask for an assembly."""
    monkeypatch.setattr(copilot, "_plan_next", _box_planner())
    asm_id = _assembly()
    charged = []

    def scripted(goal, transcript, summary, **_):
        if not [x for x in transcript if x.get("kind") == "op"]:
            return {"op": "build_part", "args": {"goal": "a plate"}, "rationale": "need one"}
        return {"done": True, "summary": "done"}

    copilot.run(asm_id, "make a plate", planner=scripted, subject="assembly",
                meter=lambda: charged.append(1))
    # One for `build_part` itself, and one for the mutation inside its sub-run.
    assert len(charged) >= 2, charged


def test_a_build_is_not_done_while_a_component_is_unmated(monkeypatch):
    """"Assembled" means held. A part that was placed and never constrained sits
    at the origin, inside whatever else is there."""
    monkeypatch.setattr(copilot, "_plan_next", _box_planner())
    asm_id = _assembly()

    def scripted(goal, transcript, summary, **_):
        built = [x for x in transcript if x.get("op") == "build_part" and x.get("ok")]
        if len(built) < 2:
            return {"op": "build_part", "args": {"goal": f"plate {len(built) + 1}"},
                    "rationale": "a part"}
        return {"done": True, "summary": "two plates, unmated"}

    out = copilot.run(asm_id, "two plates", planner=scripted, subject="assembly", max_steps=6)
    assert out["finished"] is False
    assert any("not held by any mate" in u for u in out["unmet"]), out["unmet"]


def test_the_loop_finishes_once_every_part_is_built_and_mated(monkeypatch):
    """The whole shape the loop exists for: plan parts, build each with the part
    copilot, mate them, check interference, and only then report done."""
    monkeypatch.setattr(copilot, "_plan_next", _box_planner())
    asm_id = _assembly()

    def scripted(goal, transcript, summary, **_):
        built = [x for x in transcript if x.get("op") == "build_part" and x.get("ok")]
        mated = [x for x in transcript if x.get("op") == "add_mate" and x.get("ok")]
        if len(built) < 2:
            return {"op": "build_part", "args": {"goal": f"plate {len(built) + 1}"},
                    "rationale": "a part the assembly needs"}
        if not mated:
            # The component ids come back from the build steps themselves.
            ids = [x["result"]["component_id"] for x in built]
            return {"op": "add_mate", "rationale": "stack them",
                    "args": {"type": "coincident", "base": ids[0], "moved": ids[1],
                             "axis": "z"}}
        return {"done": True, "summary": "Built two plates and stacked them."}

    out = copilot.run(asm_id, "a two-plate stack", planner=scripted,
                      subject="assembly", max_steps=8)
    ops = [x["op"] for x in out["steps"] if x.get("kind") == "op"]
    assert ops == ["build_part", "build_part", "add_mate"], ops
    checks = [x for x in out["steps"] if x.get("kind") == "verification"]
    assert any(c["op"] == "check_interference" for c in checks)
    assert out["finished"] is True and out["unmet"] == [], (out["stop_reason"], out["unmet"])
    assert len(out["assembly"].components) == 2 and len(out["assembly"].mates) == 1


def test_an_assembly_build_gets_a_bigger_budget_than_an_edit():
    """A part is a whole nested run, so a budget tuned for editing one body
    stops a build halfway and reports it as out of budget."""
    assert copilot.step_default("assembly") > copilot.step_default("document")
    assert copilot.step_cap("assembly") > copilot.step_cap("document")


# ---------------------------------------------------------------------------
# What the build loop says when parts overlap, and what it does not call a clash
# ---------------------------------------------------------------------------

def test_touching_parts_are_not_a_clash(monkeypatch):
    """The bug this replaced: `clear` is false for a *near miss* as well as a
    clash, and a coincident mate holds two faces at a 0 mm gap by definition —
    so gating on it failed every correctly assembled stack while reporting
    "0 pair(s) of parts intersect"."""
    execute = copilot.cad_operations.execute

    def touching(op, *args, **kwargs):
        if op == "check_interference":
            return {"ok": True, "result": {
                "clear": False,           # false because of the near miss below
                "interferences": 0,
                "clearance_warnings": 1,
                "pairs": [{"a": "base", "b": "top", "status": "clearance",
                           "gap_mm": 0.0, "penetration_mm": 0.0}]}}
        return execute(op, *args, **kwargs)

    monkeypatch.setattr(copilot.cad_operations, "execute", touching)
    notes = []
    failures = copilot._verify("assembly", "any-id", notes.append)
    assert failures == []
    assert notes[0]["ok"] is True
    # And the near miss is still reported rather than hidden.
    assert "1 pair(s) touching" in notes[0]["result"]["summary"]


def test_an_overlap_names_the_parts_and_how_deep(monkeypatch):
    """"1 pair(s) intersect" is a fact the planner cannot act on: it does not say
    which two parts, or whether it is out by half a millimetre or by the whole
    thickness of a plate."""
    execute = copilot.cad_operations.execute

    def clashing(op, *args, **kwargs):
        if op == "check_interference":
            return {"ok": True, "result": {
                "clear": False, "interferences": 2, "clearance_warnings": 0,
                "pairs": [
                    {"a": "base plate", "b": "top plate", "status": "interference",
                     "gap_mm": -6.0, "penetration_mm": 6.0},
                    {"a": "top plate", "b": "bracket", "status": "interference",
                     "gap_mm": -0.5, "penetration_mm": 0.5}]}}
        return execute(op, *args, **kwargs)

    monkeypatch.setattr(copilot.cad_operations, "execute", clashing)
    failures = copilot._verify("assembly", "any-id", lambda _entry: None)
    assert len(failures) == 1
    message = failures[0]
    # Which parts, and by how much — worst first.
    assert "base plate into top plate by 6.0 mm" in message
    assert "top plate into bracket by 0.5 mm" in message
    # And what to do about it, because the obvious move makes it worse.
    assert "rather than deleting it" in message


def test_deleting_the_mate_fails_both_checks_instead_of_one(monkeypatch):
    """Why that instruction is in the message. Removing the mate that positions
    an overlapping part leaves it unheld *and* still overlapping — which is what
    a live run actually did before the guidance was added."""
    monkeypatch.setattr(copilot, "_plan_next", _box_planner(size=(0.08, 0.05, 0.01)))
    asm_id = _assembly()

    def scripted(goal, transcript, summary, **_):
        built = [x for x in transcript if x.get("op") == "build_part" and x.get("ok")]
        mated = [x for x in transcript if x.get("op") == "add_mate" and x.get("ok")]
        removed = [x for x in transcript if x.get("op") == "remove_mate" and x.get("ok")]
        if len(built) < 2:
            return {"op": "build_part", "args": {"goal": f"plate {len(built) + 1}"},
                    "rationale": "a part"}
        if not mated:
            ids = [x["result"]["component_id"] for x in built]
            return {"op": "add_mate", "rationale": "stack",
                    "args": {"type": "coincident", "base": ids[0], "moved": ids[1],
                             "axis": "z"}}
        if not removed:
            mate_id = mated[0]["result"].get("mate_id")
            return {"op": "remove_mate", "args": {"mate_id": mate_id},
                    "rationale": "the wrong move"}
        return {"done": True, "summary": "removed the mate"}

    out = copilot.run(asm_id, "two plates", planner=scripted, subject="assembly",
                      max_steps=8)
    assert out["finished"] is False
    # Unheld again, which is the half the planner gave itself back.
    assert any("not held by any mate" in u for u in out["unmet"]), out["unmet"]


def test_the_planner_is_told_not_to_delete_the_mate():
    """The guidance has to reach the model, not only the failure message."""
    from app.services import action_space

    prompt = action_space.system_prompt(with_tools=True, subject="assembly")
    assert "Do not delete the mate" in prompt
    assert "offset_mm" in prompt
    # And the part-editing prompt is not cluttered with any of it.
    assert "Do not delete the mate" not in action_space.system_prompt(
        with_tools=True, subject="document")


def test_a_second_bounding_box_mate_says_it_replaced_the_first(monkeypatch):
    """The trap a live build fell into: mate z, then x, then y to centre a plate,
    and each call returns ok while undoing the last one's placement. The scheme
    places all three axes from one mate, so the second is a replacement — and
    nothing said so."""
    monkeypatch.setattr(copilot, "_plan_next", _box_planner())
    asm_id = _assembly()
    from app.services import assembly_service

    ids = [cad_operations.execute("build_part", asm_id, {"goal": f"plate {i}"})
           ["result"]["component_id"] for i in (1, 2)]

    first = cad_operations.execute("add_mate", asm_id, {
        "type": "coincident", "base": ids[0], "moved": ids[1], "axis": "z"})
    assert first["ok"] and "replaced" not in (first["result"].get("note") or "")
    stacked = next(c.transform.translate.z for c in
                   assembly_service.get_assembly(asm_id).components if c.id == ids[1])
    assert stacked > 0, "the z mate should lift the top plate clear"

    second = cad_operations.execute("add_mate", asm_id, {
        "type": "coincident", "base": ids[0], "moved": ids[1], "axis": "x",
        "base_face": "center", "moved_face": "center"})
    assert second["ok"]
    note = second["result"].get("note") or ""
    assert "replaced the placement" in note, note
    assert first["result"]["mate_id"] in note
    # And the placement really was replaced — which is what the note is for.
    after = next(c.transform.translate.z for c in
                 assembly_service.get_assembly(asm_id).components if c.id == ids[1])
    assert after == 0.0 and after != stacked


def test_the_planner_is_told_one_bounding_box_mate_per_pair():
    from app.services import action_space

    prompt = action_space.system_prompt(with_tools=True, subject="assembly")
    assert "One bounding-box mate per pair" in prompt


# ---------------------------------------------------------------------------
# Picking a model, for one run
# ---------------------------------------------------------------------------

def _capture_model(monkeypatch, decision):
    """Pretend a provider is configured and record which model each call names."""
    from app.services import llm_client

    seen = []

    def fake_post(messages, *, timeout=60.0, max_tokens=None, tools=None, model=None):
        seen.append(model)
        return {"content": json.dumps(decision)}

    monkeypatch.setattr(llm_client, "is_configured", lambda: True)
    monkeypatch.setattr(llm_client, "supports_tools", lambda: False)
    monkeypatch.setattr(llm_client, "_post_chat", fake_post)
    return seen


def test_a_run_plans_with_the_model_it_was_given(monkeypatch):
    from app.services import llm_client

    seen = _capture_model(monkeypatch, {"done": True, "summary": "nothing to do"})
    before = llm_client.LLM_MODEL

    out = copilot.run(_new_doc(), "leave it alone", model="openai/gpt-5-mini")
    assert seen == ["openai/gpt-5-mini"]
    assert out["model"] == "openai/gpt-5-mini"
    # And the process-wide default is untouched — the reason this is per run and
    # not `POST /api/llm/model`.
    assert llm_client.LLM_MODEL == before


def test_without_a_model_the_configured_default_answers(monkeypatch):
    from app.services import llm_client

    seen = _capture_model(monkeypatch, {"done": True, "summary": "done"})
    out = copilot.run(_new_doc(), "leave it alone")
    assert seen == [None], "no override means the client falls back on its own"
    assert out["model"] == llm_client.LLM_MODEL


def test_a_part_designed_inside_a_build_uses_the_builds_model(monkeypatch):
    """A build planned by one model and detailed by another compares nothing."""
    seen = _capture_model(monkeypatch, {"done": True, "summary": "part done"})
    asm_id = _assembly()

    def scripted(goal, transcript, summary, **_):
        if not [x for x in transcript if x.get("kind") == "op"]:
            return {"op": "build_part", "args": {"goal": "a plate"}, "rationale": "need one"}
        return {"done": True, "summary": "done"}

    # The assembly planner is scripted, so every recorded call is the *nested*
    # part run reaching for the model the build was started with.
    copilot.run(asm_id, "one plate", planner=scripted, subject="assembly",
                model="google/gemini-3.5-flash", max_steps=3)
    assert seen and set(seen) == {"google/gemini-3.5-flash"}, seen


def test_an_unknown_model_is_refused_with_the_ones_that_exist():
    """An arbitrary string is a run that dies at the first planner call with a
    provider error nobody can act on."""
    doc_id = _new_doc()
    r = client.post(f"/api/documents/{doc_id}/copilot",
                    json={"goal": "x", "model": "not-a-real-model"})
    assert r.status_code == 422
    assert "unknown model" in r.text

    asm_id = _assembly()
    r = client.post(f"/api/assemblies/{asm_id}/copilot",
                    json={"goal": "x", "model": "not-a-real-model"})
    assert r.status_code == 422


def test_the_job_reports_which_model_ran(monkeypatch):
    from app.services import llm_client

    _capture_model(monkeypatch, {"done": True, "summary": "done"})
    allowed = llm_client.available_models()[0]
    doc_id = _new_doc()
    r = client.post(f"/api/documents/{doc_id}/copilot",
                    json={"goal": "leave it", "model": allowed})
    assert r.status_code == 200, r.text
    job = r.json()["job_id"]
    body = client.get(f"/api/documents/{doc_id}/copilot/{job}").json()
    assert body["model"] == allowed


def test_an_empty_target_means_the_whole_body_not_a_missing_feature():
    """What a live `gpt-5-mini` run did six times: send `target: ""` for an
    optional reference. `feature  not found` names nothing a caller can fix."""
    doc_id = _new_doc()
    env = cad_operations.execute("add_primitive", doc_id, {
        "shape": "box", "operation": "union", "target": "",
        "params": {"size": {"x": 0.08, "y": 0.05, "z": 0.01},
                   "center": {"x": 0, "y": 0, "z": 0}}})
    assert env["ok"], env.get("error")

    # A target that names something real still has to be honoured, and one that
    # names something absent must still fail.
    bad = cad_operations.execute("add_primitive", doc_id, {
        "shape": "box", "operation": "union", "target": "f999",
        "params": {"size": {"x": 0.01, "y": 0.01, "z": 0.01},
                   "center": {"x": 0, "y": 0, "z": 0}}})
    assert bad["ok"] is False


# ---------------------------------------------------------------------------
# A reference image with the goal
# ---------------------------------------------------------------------------

_PNG = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
        "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def _capture_messages(monkeypatch, decisions):
    """Pretend a provider is configured and record every message list sent."""
    from app.services import llm_client

    sent = []
    queue = list(decisions)

    def fake_post(messages, *, timeout=60.0, max_tokens=None, tools=None, model=None):
        sent.append(messages)
        return {"content": json.dumps(queue.pop(0) if len(queue) > 1 else queue[0])}

    monkeypatch.setattr(llm_client, "is_configured", lambda: True)
    monkeypatch.setattr(llm_client, "supports_tools", lambda: False)
    monkeypatch.setattr(llm_client, "_post_chat", fake_post)
    return sent


def test_the_image_reaches_the_planner_on_every_turn(monkeypatch):
    """The planner is stateless between steps, so an image sent only on the first
    turn is a reference the model has lost by the second."""
    sent = _capture_messages(monkeypatch, [
        {"op": "add_primitive", "rationale": "plate",
         "args": {"shape": "box", "operation": "union",
                  "params": {"size": {"x": 0.05, "y": 0.05, "z": 0.01},
                             "center": {"x": 0, "y": 0, "z": 0}}}},
        {"done": True, "summary": "built it"},
    ])
    out = copilot.run(_new_doc(), "make what is in the picture", images=[_PNG], max_steps=4)
    assert len(sent) >= 2
    for messages in sent:
        content = messages[-1]["content"]
        assert isinstance(content, list), "the user turn carries the image as a content part"
        assert "Goal: make what is in the picture" in content[0]["text"]
        # By position, not by type: the goal, then a label, then the image.
        # Keying a dict on `type` takes whichever text part happens to be last,
        # which stops meaning the goal the moment a render is attached too.
        assert "Image 1: the user's reference" in content[1]["text"]
        # The user's reference stays first. A review render may follow it on the
        # last turn; what must never happen is the reference being displaced.
        urls = [p["image_url"]["url"] for p in content if p["type"] == "image_url"]
        assert urls[0] == _PNG
    assert out["has_image"] is True


def test_without_an_image_the_user_turn_stays_plain_text(monkeypatch):
    sent = _capture_messages(monkeypatch, [{"done": True, "summary": "done"}])
    copilot.run(_new_doc(), "leave it")
    assert isinstance(sent[0][-1]["content"], str)


def test_a_malformed_image_is_refused_before_the_run_starts():
    doc_id = _new_doc()
    for bad in ("https://example.com/cat.png",           # a URL the provider would fetch
                "data:text/html;base64,PGgxPmhpPC9oMT4=",  # not an image type
                "data:image/png;base64,@@@not-base64@@@",
                "data:image/png,rawbytes"):               # not base64 encoded
        r = client.post(f"/api/documents/{doc_id}/copilot", json={"goal": "x", "image": bad})
        assert r.status_code == 422, (bad, r.text)


def test_a_text_only_model_is_refused_an_image(monkeypatch):
    """The provider's own answer arrives mid-run as a planner error nobody can act on."""
    from app.services import llm_client

    monkeypatch.setattr(llm_client, "LLM_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setattr(llm_client, "_openrouter_catalog", lambda: {
        m: frozenset({"text"}) if "deepseek" in m else frozenset({"text", "image"})
        for m in llm_client._PROVIDER_MODELS["openrouter"]})
    assert llm_client.accepts_images("deepseek/deepseek-v4-pro-0813") is False
    assert llm_client.accepts_images("anthropic/claude-sonnet-5") is True
    vision = llm_client.current_config()["vision_models"]
    assert "anthropic/claude-sonnet-5" in vision
    assert not any("deepseek" in m for m in vision)

    asm_id = _assembly()
    r = client.post(f"/api/assemblies/{asm_id}/copilot",
                    json={"goal": "x", "image": _PNG, "model": "deepseek/deepseek-v4-pro-0813"})
    assert r.status_code == 422
    assert "does not accept images" in r.text


def test_a_provider_that_publishes_no_modalities_is_not_refused(monkeypatch):
    from app.services import llm_client

    monkeypatch.setattr(llm_client, "LLM_BASE_URL", "https://api.moonshot.cn/v1")
    assert llm_client.accepts_images("kimi-k2-turbo-preview") is None
    assert llm_client.current_config()["vision_models"] is None


def test_the_job_says_an_image_went_with_it_and_does_not_keep_it(monkeypatch):
    from app.routers import operations

    _capture_model(monkeypatch, {"done": True, "summary": "done"})
    monkeypatch.setattr(operations.llm_client, "accepts_images", lambda m=None: True)
    doc_id = _new_doc()
    r = client.post(f"/api/documents/{doc_id}/copilot", json={"goal": "leave it", "image": _PNG})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    body = client.get(f"/api/documents/{doc_id}/copilot/{job_id}").json()
    assert body["has_image"] is True and body["image_count"] == 1
    assert _PNG not in json.dumps(operations._COPILOT_JOBS[job_id], default=str)


def test_an_assembly_run_gets_more_time_as_well_as_more_steps():
    """Doubling the steps alone changes nothing when a long build hits the
    clock first."""
    assert copilot.seconds_default("assembly") > copilot.seconds_default("document")
    # And inside the twenty minutes the client polls for.
    assert copilot.seconds_default("assembly") < 20 * 60


_JPEG = "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQ=="


def test_several_images_all_reach_the_planner_in_order(monkeypatch):
    sent = _capture_messages(monkeypatch, [{"done": True, "summary": "done"}])
    out = copilot.run(_new_doc(), "match these views", images=[_PNG, _JPEG, _PNG])
    content = sent[0][-1]["content"]
    urls = [p["image_url"]["url"] for p in content if p["type"] == "image_url"]
    assert urls == [_PNG, _JPEG, _PNG]
    labels = [p["text"] for p in content if p["type"] == "text"]
    assert [label.split(":")[0] for label in labels[1:4]] == ["Image 1", "Image 2", "Image 3"]
    assert "in the order they gave them" in labels[1]
    assert out["image_count"] == 3


def test_the_route_takes_a_list_and_the_single_form_together(monkeypatch):
    """`image` is merged in front rather than ignored: pydantic drops unknown
    fields, so a client still sending it would lose its image without an error."""
    from app.routers import operations

    _capture_model(monkeypatch, {"done": True, "summary": "done"})
    monkeypatch.setattr(operations.llm_client, "accepts_images", lambda m=None: True)
    asm_id = _assembly()
    r = client.post(f"/api/assemblies/{asm_id}/copilot",
                    json={"goal": "leave it", "image": _PNG, "images": [_JPEG, _PNG]})
    assert r.status_code == 200, r.text
    assert r.json()["image_count"] == 3


def test_too_many_images_are_refused():
    doc_id = _new_doc()
    r = client.post(f"/api/documents/{doc_id}/copilot",
                    json={"goal": "x", "images": [_PNG] * (copilot.MAX_IMAGES + 1)})
    assert r.status_code == 422
    assert f"at most {copilot.MAX_IMAGES}" in r.text


def test_images_are_limited_by_their_total_size_not_only_each(monkeypatch):
    """Each image passing on its own is not enough: together they have to fit
    under the JSON body limit."""
    from app.routers import operations

    one = len(__import__("base64").b64decode(_PNG.split(",", 1)[1]))
    monkeypatch.setattr(operations, "MAX_TOTAL_IMAGE_BYTES", one * 2)
    doc_id = _new_doc()
    r = client.post(f"/api/documents/{doc_id}/copilot", json={"goal": "x", "images": [_PNG] * 3})
    assert r.status_code == 422
    assert "the limit is" in r.text


def test_a_bad_image_in_a_list_is_named_by_position():
    doc_id = _new_doc()
    r = client.post(f"/api/documents/{doc_id}/copilot",
                    json={"goal": "x", "images": [_PNG, "https://example.com/b.png"]})
    assert r.status_code == 422
    assert "image 2" in r.text


_PLATE = {"op": "add_primitive", "rationale": "plate",
          "args": {"shape": "box", "operation": "union",
                   "params": {"size": {"x": 0.05, "y": 0.05, "z": 0.01},
                              "center": {"x": 0, "y": 0, "z": 0}}}}


def test_the_copilot_looks_at_what_it_built_before_reporting_success(monkeypatch):
    """The gap every deterministic check leaves open.

    `check_geometry` says the body is a valid, watertight solid and says nothing
    about whether it is the shape that was asked for — a live run reported a
    120 x 80 plate over a body measuring 50 x 30 and passed every check it ran.
    So the planner is shown a render of its own work and gets one more turn.
    """
    sent = _capture_messages(monkeypatch, [_PLATE, {"done": True, "summary": "built it"}])
    out = copilot.run(_new_doc(), "a 50 mm plate", max_steps=4)

    # The edit, the first done, and the done taken after looking at the render.
    assert len(sent) == 3
    content = sent[-1][-1]["content"]
    urls = [p["image_url"]["url"] for p in content if p["type"] == "image_url"]
    assert len(urls) == 1 and urls[0].startswith("data:image/png;base64,")
    labels = [p["text"] for p in content if p["type"] == "text"]
    assert any("render of what you have built" in label for label in labels)
    assert any("Do not report success for a body that does not look like" in label
               for label in labels)

    reviews = [s for s in out["steps"] if s.get("op") == "visual_review"]
    # Rendered once, not once per turn: the second `done` arrives against the
    # same geometry, and re-rendering an unchanged body is a loop.
    assert len(reviews) == 1
    assert reviews[0]["result"]["views"][0]["url"].startswith("/api/documents/")
    assert out["finished"] is True and out["stop_reason"] == "done"


def test_the_review_turn_is_not_charged_to_the_build(monkeypatch):
    """A run asking for two steps still takes two; the look costs the run nothing."""
    sent = _capture_messages(monkeypatch, [{"op": "unknown_operation", "args": {}}])
    out = copilot.run(_new_doc(), "keep going", max_steps=2)
    assert len([s for s in out["steps"] if s.get("kind") == "op"]) == 2
    assert out["stop_reason"] == "budget"
    assert len(sent) == 2


def test_a_run_that_changed_nothing_renders_nothing(monkeypatch):
    """Answering a question is not building a body, and has nothing to look at."""
    sent = _capture_messages(monkeypatch, [
        {"done": True, "summary": "it is a plate", "result_kind": "answered"}])
    out = copilot.run(_new_doc(), "what is this?", max_steps=3)
    assert len(sent) == 1
    assert isinstance(sent[0][-1]["content"], str)
    assert not [s for s in out["steps"] if s.get("op") == "visual_review"]


def test_a_text_only_model_is_never_sent_a_render(monkeypatch):
    """Otherwise the provider refuses mid-run, as a planner error nobody can act on.

    The route checks vision support for images the *user* attached; a render the
    copilot makes for itself never passed that gate.
    """
    from app.services import llm_client

    monkeypatch.setattr(llm_client, "accepts_images", lambda model=None: False)
    sent = _capture_messages(monkeypatch, [_PLATE, {"done": True, "summary": "built it"}])
    out = copilot.run(_new_doc(), "a 50 mm plate", max_steps=4)

    assert len(sent) == 2
    assert all(isinstance(m[-1]["content"], str) for m in sent)
    skipped = [s for s in out["steps"] if s.get("op") == "visual_review"]
    assert skipped and "does not accept images" in skipped[0]["result"]["summary"]
    assert out["finished"] is True


def test_a_repair_after_a_review_is_shown_the_new_body_not_the_old_render(monkeypatch):
    """The render has to expire with the body it was a render of.

    A review that survives the repair it prompted is worse than no review: the
    planner is shown a picture with no hole in it, captioned as the current
    state, immediately after cutting the hole — so it cuts it again.
    """
    hole = {"op": "add_hole", "rationale": "the missing hole",
            "args": {"radius": 0.004, "depth": 0.01}}
    sent = _capture_messages(monkeypatch, [
        _PLATE,
        {"done": True, "summary": "built it"},
        hole,
        {"done": True, "summary": "now with the hole"},
        {"done": True, "summary": "now with the hole"},
    ])
    out = copilot.run(_new_doc(), "a 50 mm plate with a hole", max_steps=6)

    def images(turn):
        content = sent[turn][-1]["content"]
        return [] if isinstance(content, str) else [
            p["image_url"]["url"] for p in content if p["type"] == "image_url"]

    assert len(sent) == 5
    assert images(0) == [] and images(1) == []
    assert len(images(2)) == 1, "the first review shows the plate"
    assert images(3) == [], "the render must not outlive the body it showed"
    assert len(images(4)) == 1, "the repaired body is looked at in its turn"
    assert images(4) != images(2), "and it is a new picture, not the old one"

    reviews = [s for s in out["steps"] if s.get("op") == "visual_review"]
    assert len(reviews) == 2
    assert reviews[0]["result"]["views"][0]["url"] != reviews[1]["result"]["views"][0]["url"]
    assert out["finished"] is True and out["stop_reason"] == "done"


_BRIEF = {"op": "set_spec", "rationale": "what the part has to be",
          "args": {"goal": "a 50 mm plate",
                   "requirements": [{"kind": "bounding_box", "axis": "x",
                                     "value": 50, "tolerance": 1}]}}


def test_the_brief_rule_is_sent_for_a_part_and_not_for_an_assembly(monkeypatch):
    """The prompt is assembled twice — once for tool providers, once from
    scratch for prose ones — so a rule reaching only one reaches half the runs.
    This drives the prose path, which is the one `_capture_messages` exercises.
    """
    sent = _capture_messages(monkeypatch, [{"done": True, "summary": "done"}])
    copilot.run(_new_doc(), "what is this?", max_steps=1)
    assert "Write the brief before you build" in sent[0][0]["content"]

    sent.clear()
    copilot.run(_assembly(), "what is this?", subject="assembly", max_steps=1)
    # An assembly is judged by its mates and its interference, and `set_spec`
    # does not take an assembly id — the rule would be unfollowable there.
    assert "Write the brief before you build" not in sent[0][0]["content"]


def test_a_build_that_nobody_scored_says_so(monkeypatch):
    """The spec gate is inert when no spec was ever written.

    `_unmet_requirements` returns nothing for a document with no requirements,
    so a run that never wrote one passes by having nothing to fail. That is not
    a refusal — a fillet request has nothing measurable — but it must be visible.
    """
    sent = _capture_messages(monkeypatch, [_PLATE, {"done": True, "summary": "built it"}])
    out = copilot.run(_new_doc(), "a 50 mm plate", max_steps=4)
    notes = [s for s in out["steps"] if s.get("op") == "spec_check"]
    assert notes and "without a design brief" in notes[0]["result"]["summary"]
    assert out["finished"] is True and out["stop_reason"] == "done"


def test_a_brief_recorded_first_turns_the_spec_gate_back_on(monkeypatch):
    sent = _capture_messages(monkeypatch, [_BRIEF, _PLATE, {"done": True, "summary": "built it"}])
    out = copilot.run(_new_doc(), "a 50 mm plate", max_steps=6)
    assert [s.get("op") for s in out["steps"] if s.get("kind") == "op"][0] == "set_spec"
    # Scored rather than merely checked, so the note is absent and the run is
    # finished on a measurement rather than on the body being well formed.
    assert not [s for s in out["steps"] if s.get("op") == "spec_check"]
    assert out["unmet"] == [] and out["finished"] is True


def test_writing_the_brief_is_not_building_the_part(monkeypatch):
    """A run that records requirements and stops has not met them.

    `verify_against_spec` refuses a document with no evaluated geometry, so this
    arrives as a real failure rather than as a spec that passed vacuously.
    """
    sent = _capture_messages(monkeypatch, [_BRIEF, {"done": True, "summary": "all done"}])
    out = copilot.run(_new_doc(), "a 50 mm plate", max_steps=4)
    assert out["finished"] is False
    assert out["stop_reason"] == "verification_failed"
    assert any("could not be checked" in row for row in out["unmet"])


def test_a_brief_with_no_body_verifies_nothing_it_did_not_build(monkeypatch):
    """`set_spec` is a mutate, and a mutate is not a change to the body.

    Three checks mean "the body moved" and were reading "some operation ran":
    validating a solid that was never built, rendering it, and complaining that
    a run finished without the brief it had just written. The spec itself is
    still read — that contract is about the part, not about this run's edits —
    and it reports honestly that there is no geometry to score.
    """
    sent = _capture_messages(monkeypatch, [
        _BRIEF, {"done": True, "summary": "noted the requirements", "result_kind": "answered"}])
    out = copilot.run(_new_doc(), "what should this plate be?", max_steps=4)

    ops = [s.get("op") for s in out["steps"]]
    assert "check_geometry" not in ops, "nothing was built, so there is no solid to check"
    assert "visual_review" not in ops, "and nothing to look at"
    assert "spec_check" not in ops, "the brief it just wrote is not a missing brief"
    assert any("could not be checked" in row for row in out["unmet"])


def test_the_failure_playbook_reaches_both_subjects(monkeypatch):
    """A rule the assembly half never sees is a rule for half the runs."""
    sent = _capture_messages(monkeypatch, [{"done": True, "summary": "done"}])
    copilot.run(_new_doc(), "what is this?", max_steps=1)
    assert "the code says which axis to change" in sent[0][0]["content"]

    sent.clear()
    copilot.run(_assembly(), "what is this?", subject="assembly", max_steps=1)
    assert "the code says which axis to change" in sent[0][0]["content"]


def test_the_playbook_keys_on_the_code_the_transcript_actually_carries(monkeypatch):
    """The playbook is indexed by code, so the code has to be what the planner sees.

    A failure reaches the transcript as `"CODE: message"`. If that prefix ever
    stopped being the typed code, every line of the playbook would be keyed on
    something the planner is never shown — and nothing else would break, which
    is why this is asserted rather than assumed.
    """
    from app.services import copilot_context

    sent = _capture_messages(monkeypatch, [
        {"op": "set_feature_params", "args": {"feature_id": "nope", "params": {}}},
        {"done": True, "summary": "gave up", "result_kind": "answered"},
    ])
    out = copilot.run(_new_doc(), "edit a feature that is not there", max_steps=3)

    failed = [s for s in out["steps"] if s.get("kind") == "op" and not s.get("ok")]
    assert failed, "the call should have failed"
    code = failed[0]["error"].split(":", 1)[0]
    assert code == "NO_SUCH_FEATURE"
    assert code in copilot_context.PLAYBOOK, "the playbook does not name the code it will see"
    # The remedy rides alongside, so the planner gets both the code and the
    # sentence about it without the playbook restating every remedy.
    assert failed[0]["error_detail"]["remedy"]

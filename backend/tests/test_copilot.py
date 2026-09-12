"""Tests for Forge Copilot — the agentic operation loop (roadmap feature 01)."""

from fastapi.testclient import TestClient

from main import app
from app.services import copilot

client = TestClient(app)


def _new_doc() -> str:
    r = client.post("/api/documents", json={"resolution": 48})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _scripted(goal, transcript, summary):
    """A deterministic stand-in for the LLM planner: plate, then a hole, then done."""
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
    _COPILOT_JOBS["cancel-test"] = {"id": "cancel-test", "doc_id": doc_id, "status": "running", "steps": []}
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

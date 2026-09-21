"""Recording agent interaction sequences as training data.

The copilot already built the right structure and then dropped it on the floor:
the transcript lived in a process-local dict capped at 200 entries and died with
the process, and the prompt that started it all was never stored. So the one
thing a tool-use fine-tune needs — (goal, observation, action, result)* — was
the one thing this system never wrote down.
"""

import json

from fastapi.testclient import TestClient

from app.services import cad_operations as ops
from app.services import copilot
from app.services import document_service as docs
from app.services import trajectories
from main import app

client = TestClient(app)


def _doc(name="traj"):
    return docs.create_document(name=name, resolution=32, owner_id=None)


# ------------------------------------------------------------- recording --

def test_nothing_is_recorded_unless_a_session_is_open():
    """Recording is opt-in; the normal edit path stays free of side effects."""
    doc = _doc()
    before = len(trajectories.list_all())
    ops.execute("add_primitive", doc.id, {"shape": "box"})
    assert len(trajectories.list_all()) == before


def test_a_recorded_session_captures_the_action_and_its_result():
    doc = _doc()
    with trajectories.recording(doc.id, goal="an 80x40x10 plate") as tid:
        ops.execute("add_primitive", doc.id, {"shape": "box",
                                              "params": {"size": {"x": .08, "y": .04, "z": .01}}})
    record = trajectories.get(tid)
    assert record["goal"] == "an 80x40x10 plate"
    assert record["outcome"] == "done"
    step = record["steps"][0]
    assert step["op"] == "add_primitive"
    assert step["args"]["params"]["size"] == {"x": .08, "y": .04, "z": .01}
    assert step["ok"] is True


def test_both_sides_of_the_transition_are_kept():
    """An example needs the state the action was chosen from, not just the result."""
    doc = _doc()
    with trajectories.recording(doc.id, goal="g") as tid:
        ops.execute("add_primitive", doc.id, {"shape": "box"})
    step = trajectories.get(tid)["steps"][0]
    assert step["obs_before"] is not None and step["obs_after"] is not None
    assert step["delta"]["features"]["added"]
    assert step["latency_ms"] >= 0


def test_failures_are_recorded_too():
    """The most valuable examples: what a wrong call looks like and how it reads."""
    doc = _doc()
    with trajectories.recording(doc.id, goal="g") as tid:
        ops.execute("add_sketch", doc.id, {})
    step = trajectories.get(tid)["steps"][0]
    assert step["ok"] is False
    assert step["error"]["code"] == "MISSING_ARG"


def test_steps_are_ordered_and_indexed():
    doc = _doc()
    with trajectories.recording(doc.id, goal="g") as tid:
        ops.execute("add_primitive", doc.id, {"shape": "box"})
        ops.execute("bounding_box", doc.id, {})
        ops.execute("add_primitive", doc.id, {"shape": "sphere"})
    steps = trajectories.get(tid)["steps"]
    assert [s["i"] for s in steps] == [0, 1, 2]
    assert [s["op"] for s in steps] == ["add_primitive", "bounding_box", "add_primitive"]


def test_recording_stops_when_the_session_closes():
    doc = _doc()
    with trajectories.recording(doc.id, goal="g") as tid:
        ops.execute("add_primitive", doc.id, {"shape": "box"})
    ops.execute("add_primitive", doc.id, {"shape": "sphere"})
    assert len(trajectories.get(tid)["steps"]) == 1


def test_an_exception_inside_a_recording_still_closes_it():
    doc = _doc()
    tid = None
    try:
        with trajectories.recording(doc.id, goal="g") as tid:
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert trajectories.get(tid)["outcome"] == "error"


def test_recording_survives_a_restart_because_it_is_persisted():
    """The copilot transcript used to die with the process."""
    doc = _doc()
    with trajectories.recording(doc.id, goal="durable") as tid:
        ops.execute("add_primitive", doc.id, {"shape": "box"})
    from app.services import persistence
    assert persistence.load_trajectory(tid)["goal"] == "durable"


# --------------------------------------------------------------- provenance --

def test_a_generated_document_remembers_the_prompt_that_made_it():
    from conftest import plate_tree, whole_tree_document

    doc = whole_tree_document(plate_tree(), prompt="a 60x30x5 mm plate")
    assert doc.source_prompt == "a 60x30x5 mm plate"


def test_a_blank_document_has_no_source_prompt():
    assert _doc().source_prompt is None


# ------------------------------------------------------------ copilot runs --

def test_a_copilot_run_is_recorded_as_one_trajectory():
    doc = _doc()
    plan = [
        {"op": "add_primitive", "args": {"shape": "box"}, "rationale": "start from stock"},
        {"done": True, "summary": "made a box"},
    ]
    outcome = copilot.run(doc.id, "make a box", planner=lambda *a, **k: plan.pop(0))
    record = trajectories.get(outcome["trajectory_id"])
    assert record["kind"] == "copilot"
    assert record["goal"] == "make a box"
    assert record["steps"][0]["op"] == "add_primitive"
    # The planner's reasoning is what makes this a demonstration rather than a log.
    assert record["steps"][0]["rationale"] == "start from stock"


def test_copilot_surfaces_the_typed_error_code_to_its_planner():
    doc = _doc()
    plan = [
        {"op": "add_sketch", "args": {}, "rationale": "oops"},
        {"done": True, "summary": "stopped"},
    ]
    outcome = copilot.run(doc.id, "g", planner=lambda *a, **k: plan.pop(0))
    step = next(s for s in outcome["steps"] if s.get("kind") == "op")
    assert step["ok"] is False
    assert "MISSING_ARG" in step["error"]


# ------------------------------------------------------------------ export --

def _export(tid):
    """The first training line for a recorded trajectory."""
    record = trajectories.get(tid)
    lines = trajectories.export_jsonl([record]).splitlines()
    return json.loads(lines[0]) if lines else None


def test_export_produces_one_line_per_planner_turn():
    """A turn is one rebuilt user message and one tool call — what the copilot
    sends. It used to be an alternating system/user/assistant/tool conversation,
    which inference never sends; `datasets/corpus.py` holds the single generator
    and `test_dataset.py` pins it against a live payload."""
    doc = _doc()
    with trajectories.recording(doc.id, goal="make a plate") as tid:
        ops.execute("add_primitive", doc.id, {"shape": "box"})

    example = _export(tid)
    assert [m["role"] for m in example["messages"]] == ["system", "user", "assistant"]
    call = example["messages"][-1]["tool_calls"][0]
    assert call["function"]["name"] == "add_primitive"
    assert json.loads(call["function"]["arguments"]) == {"shape": "box"}
    assert example["parallel_tool_calls"] is False
    assert example["meta"]["source"] == "recorded" and example["meta"]["op"] == "add_primitive"


def test_the_prompt_is_generated_from_the_live_catalog():
    """So the training format cannot drift from the surface the model will call."""
    doc = _doc()
    with trajectories.recording(doc.id, goal="g") as tid:
        ops.execute("add_primitive", doc.id, {"shape": "box"})
    example = _export(tid)
    offered = {t["function"]["name"] for t in example["tools"]}
    assert {"add_primitive", "check_geometry", "set_feature_params"} <= offered


def test_a_failed_call_is_not_exported_as_something_to_copy():
    doc = _doc()
    with trajectories.recording(doc.id, goal="g") as tid:
        ops.execute("add_sketch", doc.id, {})          # MISSING_ARG
    assert trajectories.export_jsonl([trajectories.get(tid)]) == ""


def test_export_drops_trajectories_with_no_actions():
    doc = _doc()
    with trajectories.recording(doc.id, goal="did nothing"):
        pass
    assert trajectories.export_jsonl(min_steps=1).count('"did nothing"') == 0


# ------------------------------------------------------------ http surface --

def test_trajectories_are_listed_and_fetchable_over_http():
    doc = _doc()
    with trajectories.recording(doc.id, goal="http goal") as tid:
        ops.execute("add_primitive", doc.id, {"shape": "box"})

    listed = client.get("/api/trajectories", params={"doc_id": doc.id}).json()["trajectories"]
    assert [t["id"] for t in listed] == [tid]
    assert listed[0]["n_steps"] == 1

    assert client.get(f"/api/trajectories/{tid}").json()["goal"] == "http goal"


def test_the_jsonl_endpoint_returns_one_example_per_line():
    doc = _doc()
    with trajectories.recording(doc.id, goal="line goal"):
        ops.execute("add_primitive", doc.id, {"shape": "box"})

    r = client.get("/api/trajectories/export.jsonl")
    assert r.status_code == 200
    lines = [json.loads(ln) for ln in r.text.splitlines() if ln]
    # The goal lives inside the turn now, where the planner reads it, rather
    # than as a field beside the messages.
    def goal_of(example):
        content = example["messages"][1]["content"]
        return content if isinstance(content, str) else json.dumps(content)
    assert any("line goal" in goal_of(ex) for ex in lines)


def test_an_unknown_trajectory_is_a_typed_404():
    r = client.get("/api/trajectories/nope")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "NO_SUCH_TRAJECTORY"

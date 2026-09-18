"""The reset/step adapter: the reward is the spec, and it never raises."""

from evals.agent import Task
from evals.env import ForgeEnv

PLATE = {"op": "add_primitive",
         "args": {"shape": "box", "operation": "union",
                  "params": {"size": {"x": 0.05, "y": 0.05, "z": 0.01},
                             "center": {"x": 0, "y": 0, "z": 0}}}}


def _task(**over) -> Task:
    return Task(id=over.pop("id", "plate"),
                goal="a 50 mm plate",
                requirements=[{"kind": "bounding_box", "axis": "x",
                               "value": 50, "tolerance": 1}],
                **over)


def test_one_action_takes_the_reward_from_nothing_to_met():
    env = ForgeEnv(_task(), resolution=32)
    obs, info = env.reset()
    try:
        assert info["doc_id"] and info["requirements"] == 1
        assert obs.get("kind") == "document"

        obs, reward, terminated, truncated, info = env.step(PLATE)
        assert info["ok"] is True
        assert reward == 1.0, info
        assert terminated is True and truncated is False
        assert info["geometry_ok"] is True
    finally:
        env.close()


def test_an_unscoreable_state_is_zero_with_a_reason_rather_than_an_exception():
    """`verify_against_spec` refuses a document with no evaluated geometry.

    That refusal is correct and it must not escape `step()`: an environment that
    throws mid-episode is one every training loop wraps in a bare `except`.
    """
    env = ForgeEnv(_task(id="unscoreable"), resolution=32)
    env.reset()
    try:
        # A query that changes nothing, against a document with no body yet.
        obs, reward, terminated, truncated, info = env.step(
            {"op": "bounding_box", "args": {}})
        assert reward == 0.0
        assert info["reward_note"], "the reason it could not be scored"
        assert terminated is False
    finally:
        env.close()


def test_a_failed_action_returns_the_typed_code_rather_than_raising():
    env = ForgeEnv(_task(id="failing"), resolution=32)
    env.reset()
    try:
        obs, reward, terminated, truncated, info = env.step(
            {"op": "set_feature_params", "args": {"feature_id": "nope", "params": {}}})
        assert info["ok"] is False
        assert info["error"] == "NO_SUCH_FEATURE"
        assert info["remedy"], "the remedy rides along, as everywhere else"
        assert reward == 0.0 and terminated is False
    finally:
        env.close()


def test_the_episode_truncates_on_the_task_budget():
    env = ForgeEnv(_task(id="budget", max_steps=2), resolution=32)
    env.reset()
    try:
        _, _, _, truncated, _ = env.step({"op": "bounding_box", "args": {}})
        assert truncated is False
        _, _, terminated, truncated, info = env.step({"op": "bounding_box", "args": {}})
        assert truncated is True and terminated is False
        assert info["steps"] == 2
    finally:
        env.close()


def test_an_episode_is_a_recorded_trajectory():
    """The reason this sits over `trajectories` rather than beside it."""
    from app.services import trajectories

    with ForgeEnv(_task(id="recorded"), resolution=32) as env:
        env.step(PLATE)
        doc_id = env.doc_id
    recorded = trajectories.list_for_document(doc_id)
    assert recorded, "the episode was not recorded"
    steps = recorded[0].get("steps") or []
    assert any(s.get("op") == "add_primitive" for s in steps)


def test_reset_starts_a_fresh_document_each_time():
    env = ForgeEnv(_task(id="fresh"), resolution=32)
    env.reset()
    try:
        env.step(PLATE)
        first = env.doc_id
        obs, _ = env.reset()
        assert env.doc_id != first
        # Not "no features": a new document is three datum planes and a root,
        # none of which is material. `has_solid` is the question that means body.
        from app.services import document_service, feature_ops
        fresh = document_service.get_document(env.doc_id)
        assert not feature_ops.has_solid(fresh.feature_tree), "a new episode starts with no body"
        assert env.steps == 0
    finally:
        env.close()

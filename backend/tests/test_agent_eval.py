"""The benchmark, and the part of it that runs without an API key.

`evals.agent` is the answer to "is the agent getting better?" — and a harness
nobody runs decays. The adversarial suite needs no key, so it runs here on every
commit: scoring, budgets, the error histogram and the report are all exercised,
and the numbers it defends are the ones A2 moved.

The suite's headline is not a pass rate. It is **caught**: of the calls that
should have been rejected or flagged, how many were. Before the surface was
tightened that number was 0 of 5, and each of the five returned ``ok: true``
with a body nobody asked for. It is 9 of 9 now, and all four were turned up by
widening the *goal* suite: a `params` key belonging to a different shape, a
3-vector missing a component, and an extent given as zero. Every one was a
declared-looking argument that validated and then produced a body nobody asked
for.
"""

import pytest

from app.services import llm_client
from evals import agent
from evals import tasks as task_set


@pytest.fixture(scope="module")
def adversarial():
    return agent.run_suite(task_set.ADVERSARIAL)


def test_every_known_mistake_is_caught(adversarial):
    caught = [r for r in adversarial["results"]
              if r["expect"] in ("refuse", "warn") and (r["refused"] or r["warnings"])]
    missed = [r["task_id"] for r in adversarial["results"]
              if r["expect"] in ("refuse", "warn") and not (r["refused"] or r["warnings"])]
    assert not missed, f"passed silently: {missed}"
    # The literal is deliberate: it is edited when a mistake is *added* to the
    # suite, which is a moment worth noticing, and it fails if one is quietly
    # dropped. 5 when the surface was tightened; 7 with the two that found the
    # per-shape `params` hole — `radius` on a cone and on a shell are declared
    # keys belonging to another shape, so they passed validation and defaulted.
    assert len(caught) == adversarial["expected_flags"] == 9


def test_the_controls_still_build_the_part(adversarial):
    """The other half of the assertion. Tightening a surface until nothing works
    would make every other row look like an improvement."""
    controls = [r for r in adversarial["results"] if r["expect"] == "succeed"]
    assert controls and all(r["score"] >= 1.0 and r["geometry_ok"] for r in controls)


def test_a_refusal_is_recoverable(adversarial):
    """A typed error is only worth having if the next call can be right."""
    row = next(r for r in adversarial["results"]
               if r["task_id"] == "adv:recovers-after-a-refusal")
    assert row["refused"] and row["score"] >= 1.0


def test_the_report_splits_failures_by_code(adversarial):
    """`MISSING_ARG` dominating and `PRECONDITION` dominating are different
    problems with different fixes, and only the histogram tells them apart."""
    assert adversarial["by_error_code"]
    assert set(adversarial["by_error_code"]) <= {
        "MISSING_ARG", "BAD_ARG", "PRECONDITION", "NO_SUCH_FEATURE",
        "EVAL_FAILED", "UNKNOWN_OP", "NO_SUCH_DOCUMENT", "INTERNAL"}


def test_the_resolution_is_pinned_not_inherited(adversarial):
    """`capped_resolution` clamps to the account's plan, so an unpinned
    benchmark scores differently depending on who ran it."""
    assert adversarial["resolution"] == agent.DEFAULT_RESOLUTION


def test_scoring_uses_the_spec_verifier_rather_than_a_second_opinion():
    """One definition of correct. A second scorer means the weaker one wins an
    argument eventually, and `design_spec` is the one that fails loudly when a
    requirement cannot be measured."""
    task = next(t for t in task_set.ADVERSARIAL if t.id == "adv:correct-plate")
    result = agent.run_task(task)
    assert result.total == len(task.requirements)
    assert result.met == result.total and result.score == 1.0


def test_a_run_that_stops_short_of_the_goal_says_so():
    """A summary that reads the same whether or not the requirements were met is
    the silent success this surface is built to avoid."""
    from app.services import cad_operations, copilot, document_service

    doc_id = document_service.create_document(name="unfinished").id
    cad_operations.execute("set_spec", doc_id, {"requirements": [
        {"kind": "bounding_box", "axis": "x", "value": 250, "tolerance": 1}]})
    run = copilot.run(doc_id, "a 250 mm plate", max_steps=1,
                      planner=agent.scripted_planner([]))
    assert "unmet" in run["summary"]
    assert run["unmet"]


def test_the_loop_checks_the_spec_before_declaring_itself_done():
    from app.services import cad_operations, copilot, document_service

    doc_id = document_service.create_document(name="checked").id
    cad_operations.execute("set_spec", doc_id, {"requirements": [
        {"kind": "bounding_box", "axis": "x", "value": 250, "tolerance": 1}]})
    run = copilot.run(doc_id, "a 250 mm plate", max_steps=4,
                      planner=agent.scripted_planner([]))

    # The self-check is a `verification` entry rather than a fabricated `op`
    # one. Asserting on the behaviour instead of on the note's name is the
    # point: this test pinned the string "verify_against_spec" and went red the
    # moment the loop renamed it, while the guarantee it exists to defend — the
    # spec is read before the loop calls itself done — never moved.
    checks = [s for s in run["steps"] if s.get("kind") == "verification"]
    assert checks, "the spec must be read before the loop declares itself done"
    # Bounded: one repair attempt, then a fresh check. Not a check on every step,
    # which would spend the whole budget checking.
    assert len(checks) <= 2, f"checked {len(checks)} times, not once plus a retry"
    assert checks[0]["result"]["unmet"]
    # And a run that never met the spec is not a success, whatever it stopped on.
    assert run["finished"] is False
    assert run["stop_reason"] == "verification_failed"
    assert run["unmet"]


def test_a_document_with_no_spec_is_not_given_a_phantom_failure():
    from app.services import copilot, document_service

    doc_id = document_service.create_document(name="unspecified").id
    run = copilot.run(doc_id, "anything", max_steps=2, planner=agent.scripted_planner([]))
    assert run["finished"] and not run["unmet"]


@pytest.mark.skipif(llm_client.is_configured(), reason="a key is configured")
def test_the_goal_suite_declines_rather_than_pretending_without_a_key():
    assert agent.main(["--suite", "goals"]) == 2


def test_a_task_the_planner_never_answered_is_not_a_score():
    """An Opus run over eleven edit tasks met `HTTP 402: this request would exceed
    your available credits` on five of them, took **zero steps** on each, and the
    suite reported a mean of 0.818 — the five unrun tasks contributing whatever
    their *seed* already scored. A benchmark that averages in tasks it could not
    attempt reads highest exactly when the model is least available.

    The excluded tasks stay in `results`, because an artifact that hides what it
    could not run cannot be audited afterwards.
    """
    ran = agent.TaskResult(task_id="ran", score=1.0, met=2, total=2, geometry_ok=True,
                           steps=3, max_steps=8, finished=True, ok_steps=3)
    never = agent.TaskResult(task_id="never", score=0.8, met=4, total=5,
                             geometry_ok=True, steps=0, max_steps=8, finished=False,
                             ok_steps=0, unrun=True)

    data = agent.report([ran, never], resolution=96)
    assert data["tasks"] == 1, "the unrun task is not one of the tasks scored"
    assert data["unrun"] == 1 and data["unrun_tasks"] == ["never"]
    assert data["mean_score"] == 1.0, "0.8 from a task that never ran must not average in"
    assert {r["task_id"] for r in data["results"]} == {"ran", "never"}
    assert "never ran" in agent.format_report(data)

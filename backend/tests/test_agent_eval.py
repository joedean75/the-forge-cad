"""The benchmark, and the part of it that runs without an API key.

`evals.agent` is the answer to "is the agent getting better?" — and a harness
nobody runs decays. The adversarial suite needs no key, so it runs here on every
commit: scoring, budgets, the error histogram and the report are all exercised,
and the numbers it defends are the ones A2 moved.

The suite's headline is not a pass rate. It is **caught**: of the calls that
should have been rejected or flagged, how many were. Before the surface was
tightened that number was 0 of 5, and each of the five returned ``ok: true``
with a body nobody asked for.
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
    assert len(caught) == adversarial["expected_flags"] == 5


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
    checks = [s for s in run["steps"] if s.get("op") == "verify_against_spec"]
    assert len(checks) == 1, "checked once on the way to done, not on every step"
    assert checks[0]["result"]["unmet"]


def test_a_document_with_no_spec_is_not_given_a_phantom_failure():
    from app.services import copilot, document_service

    doc_id = document_service.create_document(name="unspecified").id
    run = copilot.run(doc_id, "anything", max_steps=2, planner=agent.scripted_planner([]))
    assert run["finished"] and not run["unmet"]


@pytest.mark.skipif(llm_client.is_configured(), reason="a key is configured")
def test_the_goal_suite_declines_rather_than_pretending_without_a_key():
    assert agent.main(["--suite", "goals"]) == 2

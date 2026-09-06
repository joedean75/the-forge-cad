"""One vocabulary for all six modes, and FEA on the queue (FLOW-02, JOB-06, API-01).

The failure this replaces is not a crash. It is that an Analysis panel with no
load cases and an Analysis panel nobody had run looked identical — both empty —
and only one of them was a button press away. Six panels each decided for
themselves what "ready" meant, and the differences were invisible until somebody
hit one.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import http_part
from app.services import (
    cad_operations, derived_cache, document_service as docs, jobs, persistence, workflow,
)
from main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _clean_queue():
    for row in persistence.list_job_rows():
        persistence.delete_job(row["id"])
    yield


def _part() -> str:
    return http_part(client, (0.06, 0.04, 0.01), resolution=32)


def _blank() -> str:
    resp = client.post("/api/documents", json={"name": "empty", "resolution": 32})
    return resp.json()["id"]


def _state(doc_id: str, mode: str, settings=None) -> dict:
    env = cad_operations.execute("workflow_status", doc_id,
                                 {"settings": settings or {}}, include_state=False)
    assert env["ok"] is True, env.get("error")
    return next(m for m in env["result"]["modes"] if m["mode"] == mode)


# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------

def test_a_document_with_no_body_blocks_every_mode_that_needs_one():
    doc_id = _blank()
    for mode in ("optimize", "analysis", "make", "validate", "machine"):
        state = _state(doc_id, mode)
        assert state["state"] == "blocked"
        assert "body" in state["missing"]


def test_model_is_never_blocked_and_has_no_product():
    """It displays the body itself; there is nothing downstream of it to go stale."""
    assert _state(_blank(), "model")["state"] == "ready"
    assert workflow.MODES["model"].product is None


def test_a_blocked_mode_names_the_step_that_unblocks_it():
    """'Blocked' on its own sends somebody hunting."""
    doc_id = _part()
    state = _state(doc_id, "analysis")
    assert state["state"] == "blocked"
    assert set(state["missing"]) == {"material", "loads"}
    assert "set_material" in state["detail"]


def test_a_missing_spec_blocks_validate_but_nothing_else():
    doc_id = _part()
    assert _state(doc_id, "validate")["state"] == "blocked"
    assert _state(doc_id, "make")["state"] == "ready"


def test_ready_means_the_inputs_are_there_and_nothing_is_computed():
    doc_id = _part()
    assert _state(doc_id, "make")["state"] == "ready"


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------

def test_a_computed_report_reads_as_current_and_then_stale():
    doc_id = _part()
    settings = {"process": "additive", "build_axis": "auto", "resolution": 32}
    env = cad_operations.execute("dfm", doc_id, {"process": "additive", "resolution": 32})
    assert env["ok"] is True, env.get("error")

    assert _state(doc_id, "make", {"make": settings})["state"] == "current"

    from app.cad_kernel.core.feature_tree import walk_nodes

    box = next(n["id"] for n in walk_nodes(docs.get_document(doc_id).feature_tree)
               if n.get("type") == "BoxFeature")
    cad_operations.execute("set_feature_params", doc_id, {
        "feature_id": box, "params": {"size": {"x": 0.09, "y": 0.04, "z": 0.01}}})

    after = _state(doc_id, "make", {"make": settings})
    assert after["state"] == "stale"
    assert "moved" in after["detail"]


def test_a_report_for_another_process_is_not_current_for_this_one():
    """`DfmReport.process` is a display label, so freshness keys off what was asked."""
    doc_id = _part()
    cad_operations.execute("dfm", doc_id, {"process": "additive", "resolution": 32})
    milling = {"make": {"process": "milling", "build_axis": "auto", "resolution": 32}}
    assert _state(doc_id, "make", milling)["state"] == "stale"


def test_freshness_survives_the_tab_that_computed_it():
    """The whole reason this moved server-side: a second viewer gets an answer."""
    doc_id = _part()
    spec = cad_operations.execute("set_spec", doc_id, {"requirements": [
        {"kind": "mass", "max": 500.0}]})
    assert spec["ok"] is True, spec.get("error")
    verified = cad_operations.execute("verify_against_spec", doc_id, {})
    assert verified["ok"] is True, verified.get("error")
    # Re-read the document from the datastore, which is what another process
    # would see; the freshness answer has to come with it.
    docs.DOCUMENTS.pop(doc_id, None)
    assert _state(doc_id, "validate")["state"] == "current"


# ---------------------------------------------------------------------------
# Jobs in the mode vocabulary
# ---------------------------------------------------------------------------

def test_a_queued_job_shows_as_queued_rather_than_as_nothing_happening():
    doc_id = _part()
    cad_operations.execute("set_material", doc_id, {"material_id": "al-6061"})
    _give_loads(doc_id)
    env = cad_operations.execute("start_analysis", doc_id, {})
    assert env["ok"] is True, env.get("error")

    state = _state(doc_id, "analysis")
    assert state["state"] == "queued"
    assert state["job_id"] == env["result"]["job_id"]


def test_a_finished_solve_is_published_and_reads_as_current():
    doc_id = _part()
    cad_operations.execute("set_material", doc_id, {"material_id": "al-6061"})
    _give_loads(doc_id)
    cad_operations.execute("start_analysis", doc_id, {})
    jobs.run_pending_inline()

    doc = docs.get_document(doc_id)
    assert derived_cache.held(doc, "analysis") is not None
    settings = {"analysis": {"elements": None,
                             "load_cases": [lc.model_dump()
                                            for lc in doc.optimization.load_cases]}}
    assert _state(doc_id, "analysis", settings)["state"] == "current"


def test_a_solve_that_lands_after_an_edit_is_not_published():
    """The dangerous stale number: a stress figure from a body since thinned."""
    from app.cad_kernel.core.feature_tree import walk_nodes

    doc_id = _part()
    cad_operations.execute("set_material", doc_id, {"material_id": "al-6061"})
    _give_loads(doc_id)
    cad_operations.execute("start_analysis", doc_id, {})

    box = next(n["id"] for n in walk_nodes(docs.get_document(doc_id).feature_tree)
               if n.get("type") == "BoxFeature")
    cad_operations.execute("set_feature_params", doc_id, {
        "feature_id": box, "params": {"size": {"x": 0.2, "y": 0.04, "z": 0.01}}})

    jobs.run_pending_inline()
    job = jobs.for_subject(doc_id)[0]
    assert job.status == jobs.SUCCEEDED           # the solve itself was fine
    assert job.result["published"] is False       # but it describes an older body
    assert "older body" in job.result["note"]
    assert derived_cache.held(docs.get_document(doc_id), "analysis") is None


def test_a_failed_job_is_reported_as_failed_not_as_ready():
    doc_id = _part()
    cad_operations.execute("set_material", doc_id, {"material_id": "al-6061"})
    _give_loads(doc_id)
    env = cad_operations.execute("start_analysis", doc_id, {})
    job_id = env["result"]["job_id"]
    jobs._mutate(job_id, lambda j: (setattr(j, "status", jobs.FAILED),
                                    setattr(j, "message", "solver exploded"),
                                    setattr(j, "finished_at", "2026-01-01T00:00:00+00:00")))
    state = _state(doc_id, "analysis")
    assert state["state"] == "failed"
    assert state["detail"] == "solver exploded"


# ---------------------------------------------------------------------------
# The job operations
# ---------------------------------------------------------------------------

def test_job_status_reads_one_job_and_lists_them_all():
    doc_id = _part()
    cad_operations.execute("set_material", doc_id, {"material_id": "al-6061"})
    _give_loads(doc_id)
    job_id = cad_operations.execute("start_analysis", doc_id, {})["result"]["job_id"]

    one = cad_operations.execute("job_status", doc_id, {"job_id": job_id})
    assert one["ok"] is True
    assert one["result"]["id"] == job_id

    listed = cad_operations.execute("job_status", doc_id, {})
    assert [j["id"] for j in listed["result"]["jobs"]] == [job_id]


def test_a_job_id_from_another_document_does_not_resolve():
    mine, theirs = _part(), _part()
    for doc_id in (mine, theirs):
        cad_operations.execute("set_material", doc_id, {"material_id": "al-6061"})
        _give_loads(doc_id)
    job_id = cad_operations.execute("start_analysis", theirs, {})["result"]["job_id"]

    env = cad_operations.execute("job_status", mine, {"job_id": job_id})
    assert env["ok"] is False
    assert env["error"]["code"] == "NO_SUCH_DOCUMENT"


def test_cancel_job_stops_a_queued_solve():
    doc_id = _part()
    cad_operations.execute("set_material", doc_id, {"material_id": "al-6061"})
    _give_loads(doc_id)
    job_id = cad_operations.execute("start_analysis", doc_id, {})["result"]["job_id"]

    env = cad_operations.execute("cancel_job", doc_id, {"job_id": job_id})
    assert env["ok"] is True
    assert env["result"]["status"] == jobs.CANCELLED


def test_simulate_stays_inline_because_it_is_fast():
    """The roadmap's own rule: retain fast operations inline where evidence
    supports it. Moving a sub-second call off the request path buys a poll loop
    and nothing else."""
    doc_id = _part()
    env = cad_operations.execute("simulate", doc_id, {"resolution": 32})
    assert env["ok"] is True, env.get("error")
    assert "max_stress" in env["result"]
    assert "job_id" not in env["result"]


def _give_loads(doc_id: str) -> None:
    from app.models.schemas import LoadCase, OptimizationSetup, Vector3

    doc = docs.get_document(doc_id)
    doc.optimization = OptimizationSetup(
        load_cases=[LoadCase(id="l1", name="tip",
                             point=Vector3(x=0.03, y=0.0, z=0.0),
                             force=Vector3(x=0.0, y=0.0, z=-100.0))],
        resolution=32)
    docs.persist_derived(doc)

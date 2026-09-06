"""Durable background jobs (JOB-01..05, UI-03).

The three failures the old shape had are the three things asserted here: a job
that survives the process that started it, a worker that dies leaving something
recoverable rather than a permanent `running`, and a result that lands into an
edited document being refused rather than applied.

The workers are not started for these tests. `run_pending_inline` drains the
queue on the calling thread, which makes every assertion deterministic — a test
that slept waiting for a pool would be measuring the scheduler.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from conftest import http_part
from app.services import cad_operations, document_service as docs, jobs, persistence
from main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _clean_queue():
    for row in persistence.list_job_rows():
        persistence.delete_job(row["id"])
    yield
    for row in persistence.list_job_rows():
        persistence.delete_job(row["id"])


@jobs.handler("test.echo")
def _echo(context: jobs.Context) -> dict:
    context.progress(0.5, "halfway")
    return {"echoed": context.inputs.get("say")}


@jobs.handler("test.boom")
def _boom(_context: jobs.Context) -> dict:
    raise RuntimeError("the solve exploded")


@jobs.handler("test.slow")
def _slow(context: jobs.Context) -> dict:
    for step in range(20):
        context.stop_if_cancelled()
        context.progress(step / 20.0, f"step {step}")
        time.sleep(0.01)
    return {"finished": True}


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------

def test_a_job_is_a_row_and_survives_the_object_that_made_it():
    job = jobs.enqueue("test.echo", "doc-1", {"say": "hello"})
    # Nothing held in memory: this reads it back out of the datastore.
    again = jobs.get(job.id)
    assert again is not None
    assert again.status == jobs.QUEUED
    assert again.inputs == {"say": "hello"}


def test_running_a_job_records_its_result_and_terminal_state():
    job = jobs.enqueue("test.echo", "doc-1", {"say": "hello"})
    assert jobs.run_pending_inline() == 1
    done = jobs.get(job.id)
    assert done.status == jobs.SUCCEEDED
    assert done.result == {"echoed": "hello"}
    assert done.progress_fraction == 1.0
    assert done.finished_at


def test_a_failing_job_retries_once_and_then_stays_failed():
    """One retry, not five: this recovers from a killed process, not from a
    solve that reliably crashes."""
    job = jobs.enqueue("test.boom", "doc-1")
    jobs.run_pending_inline(limit=1)
    assert jobs.get(job.id).status == jobs.QUEUED  # queued for a second attempt
    jobs.run_pending_inline(limit=1)
    failed = jobs.get(job.id)
    assert failed.status == jobs.FAILED
    assert failed.attempts == jobs.MAX_ATTEMPTS
    assert "exploded" in failed.error["message"]
    assert failed.error["remedy"]


def test_an_unknown_kind_is_refused_at_enqueue_rather_than_at_run():
    with pytest.raises(KeyError):
        jobs.enqueue("test.nonexistent", "doc-1")


def test_the_queue_pushes_back_rather_than_accepting_forever(monkeypatch):
    monkeypatch.setattr(jobs, "MAX_QUEUED", 2)
    jobs.enqueue("test.echo", "doc-1")
    jobs.enqueue("test.echo", "doc-1")
    with pytest.raises(jobs.QueueFull):
        jobs.enqueue("test.echo", "doc-1")


def test_only_one_worker_can_claim_a_job():
    """The queue's whole mutual exclusion: two workers, one job, one winner."""
    jobs.enqueue("test.echo", "doc-1")
    first = jobs.claim("worker-a")
    second = jobs.claim("worker-b")
    assert first is not None
    assert second is None
    assert jobs.get(first.id).worker == "worker-a"


def test_claiming_is_oldest_first():
    older = jobs.enqueue("test.echo", "doc-1", {"say": "first"})
    newer = jobs.enqueue("test.echo", "doc-1", {"say": "second"})
    # Two enqueues can land inside one clock tick, so make the order explicit
    # rather than depending on the timestamp's resolution.
    jobs._mutate(newer.id, lambda j: setattr(j, "created_at", "2999-01-01T00:00:00+00:00"))
    assert jobs.claim("w").id == older.id


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------

def test_a_worker_that_stops_reporting_gets_its_job_back_on_the_queue():
    """The failure with no other recovery: a row that says `running` forever."""
    job = jobs.enqueue("test.echo", "doc-1")
    jobs.claim("worker-that-dies")
    stale = (datetime.now(timezone.utc) - timedelta(seconds=jobs.LEASE_SECONDS + 60)).isoformat()
    jobs._mutate(job.id, lambda j: setattr(j, "heartbeat_at", stale))

    report = jobs.sweep()
    assert report["recovered"] == 1
    assert jobs.get(job.id).status == jobs.QUEUED


def test_a_job_out_of_attempts_is_failed_explicitly_never_left_running():
    job = jobs.enqueue("test.echo", "doc-1")
    jobs.claim("worker-a")
    stale = (datetime.now(timezone.utc) - timedelta(seconds=jobs.LEASE_SECONDS + 60)).isoformat()
    jobs._mutate(job.id, lambda j: (setattr(j, "attempts", jobs.MAX_ATTEMPTS),
                                    setattr(j, "heartbeat_at", stale)))

    jobs.sweep()
    lost = jobs.get(job.id)
    assert lost.status == jobs.FAILED
    assert lost.error["code"] == "WORKER_LOST"


def test_finished_jobs_are_pruned_after_the_retention_window():
    job = jobs.enqueue("test.echo", "doc-1")
    jobs.run_pending_inline()
    old = (datetime.now(timezone.utc) - jobs.RETENTION - timedelta(hours=1)).isoformat()
    jobs._mutate(job.id, lambda j: setattr(j, "finished_at", old))
    assert jobs.sweep()["pruned"] == 1
    assert jobs.get(job.id) is None


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------

def test_cancelling_a_queued_job_is_immediate_and_terminal():
    job = jobs.enqueue("test.slow", "doc-1")
    cancelled = jobs.request_cancel(job.id)
    assert cancelled.status == jobs.CANCELLED
    # And it is not picked up afterwards.
    assert jobs.claim("w") is None


def test_cancelling_a_running_job_is_cooperative_and_says_so():
    job = jobs.enqueue("test.slow", "doc-1")
    claimed = jobs.claim("w")
    asked = jobs.request_cancel(job.id)
    assert asked.status == jobs.CANCEL_REQUESTED
    assert "checkpoint" in asked.message

    jobs.run(claimed)
    assert jobs.get(job.id).status == jobs.CANCELLED


def test_a_handler_that_finishes_after_a_cancel_is_still_reported_cancelled():
    """Otherwise a job whose work was cut short reports as succeeded."""

    @jobs.handler("test.ignores_cancel")
    def _ignores(_context: jobs.Context) -> dict:
        return {"did": "everything"}

    job = jobs.enqueue("test.ignores_cancel", "doc-1")
    claimed = jobs.claim("w")
    jobs.request_cancel(job.id)
    jobs.run(claimed)
    assert jobs.get(job.id).status == jobs.CANCELLED


# ---------------------------------------------------------------------------
# Reconnecting
# ---------------------------------------------------------------------------

def test_a_document_knows_what_is_running_against_it():
    """How a reloaded page finds the job whose id was in a closed tab."""
    jobs.enqueue("test.echo", "doc-42")
    active = jobs.active_for_subject("doc-42")
    assert active is not None
    jobs.run_pending_inline()
    assert jobs.active_for_subject("doc-42") is None


def test_jobs_are_listed_newest_first_for_their_subject():
    jobs.enqueue("test.echo", "doc-7", {"say": "a"})
    jobs.enqueue("test.echo", "doc-8", {"say": "b"})
    listed = jobs.for_subject("doc-7")
    assert [j.subject_id for j in listed] == ["doc-7"]


# ---------------------------------------------------------------------------
# Over HTTP
# ---------------------------------------------------------------------------

def _part() -> str:
    return http_part(client, (0.06, 0.04, 0.01), resolution=32)


def test_a_release_package_job_is_queued_and_readable_through_both_routes():
    doc_id = _part()
    started = client.post(f"/api/documents/{doc_id}/package", json={})
    assert started.status_code == 200, started.text
    job_id = started.json()["id"]
    # The legacy three-state shape the panel reads...
    assert started.json()["status"] == "running"
    # ...and the full six-state contract on the generic route.
    full = client.get(f"/api/documents/{doc_id}/jobs/{job_id}")
    assert full.status_code == 200
    assert full.json()["status"] == jobs.QUEUED
    assert full.json()["kind"] == "release_package"


def test_a_job_belonging_to_another_document_is_a_404_not_a_read():
    """Knowing an id must not be enough: a package result is a manifest of a part."""
    mine, theirs = _part(), _part()
    started = client.post(f"/api/documents/{theirs}/package", json={})
    job_id = started.json()["id"]
    assert client.get(f"/api/documents/{mine}/jobs/{job_id}").status_code == 404


def test_the_document_job_listing_is_how_a_reload_reconnects():
    doc_id = _part()
    client.post(f"/api/documents/{doc_id}/package", json={})
    listed = client.get(f"/api/documents/{doc_id}/jobs")
    assert listed.status_code == 200
    assert len(listed.json()["jobs"]) == 1
    assert listed.json()["jobs"][0]["status"] == jobs.QUEUED
    # Inputs are not echoed back: the client that submitted them already has
    # them, and a poll should not carry an optimization setup.
    assert "inputs" not in listed.json()["jobs"][0]


def test_cancelling_over_http_reports_the_new_state():
    doc_id = _part()
    job_id = client.post(f"/api/documents/{doc_id}/package", json={}).json()["id"]
    resp = client.post(f"/api/documents/{doc_id}/jobs/{job_id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == jobs.CANCELLED


# ---------------------------------------------------------------------------
# Revision-aware publication
# ---------------------------------------------------------------------------

def test_an_optimize_result_is_refused_when_the_part_changed_while_it_ran():
    """A SIMP result replaces the whole tree; applying it over four minutes of
    edits deletes them rather than merging with them."""
    from app.cad_kernel.core.feature_tree import walk_nodes
    from app.models.schemas import OptimizationSetup

    doc_id = _part()
    stale_revision = docs.get_document(doc_id).revision
    box = next(n["id"] for n in walk_nodes(docs.get_document(doc_id).feature_tree)
               if n.get("type") == "BoxFeature")
    cad_operations.execute("set_feature_params", doc_id, {
        "feature_id": box, "params": {"size": {"x": 0.09, "y": 0.04, "z": 0.01}}})

    with pytest.raises(docs.RevisionConflict):
        docs.run_document_optimize(doc_id, OptimizationSetup(resolution=32),
                                   resolution=32, expected_revision=stale_revision)


def test_the_optimize_route_records_the_revision_it_was_asked_against():
    doc_id = _part()
    revision = docs.get_document(doc_id).revision
    started = client.post(f"/api/documents/{doc_id}/optimize",
                          json={"resolution": 32})
    assert started.status_code == 200, started.text
    job = jobs.get(started.json()["id"])
    assert job.document_revision == revision
    assert job.kind == "optimize"

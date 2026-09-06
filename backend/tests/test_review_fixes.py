"""The three review findings against the workflow/performance refactor.

Each was a correctness hole rather than a crash, and each is the same shape: a
guarantee that held on one path and not on the one beside it.

- **FIX-01** — the operation route serialized its writes and the direct `PUT`
  did not, so the write most able to clobber was the one without the lock.
- **FIX-02** — handlers grew useful completion messages and the runner
  overwrote every one of them with `"<kind> complete"`, so an optimize that ate
  a mounting boss reported a green tick and no warning.
- **FIX-03** — the optimize handler's own comment said a stale result was "not
  worth retrying" and then raised the exception type the runner retries.
"""

from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor

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


def _part() -> str:
    return http_part(client, (0.06, 0.04, 0.01), resolution=32)


def _box_id(doc_id: str) -> str:
    from app.cad_kernel.core.feature_tree import walk_nodes

    for node in walk_nodes(docs.get_document(doc_id).feature_tree):
        if node.get("type") == "BoxFeature":
            return node["id"]
    raise AssertionError("no box in that tree")


# ---------------------------------------------------------------------------
# FIX-01: the direct update path takes the document's write lock
# ---------------------------------------------------------------------------

def _hold_inside_the_write(monkeypatch, doc_id: str):
    """Pause a write while it is between the revision check and the commit.

    Two threads racing on their own do not reliably interleave — the window is
    microseconds and the GIL usually hands one of them the whole thing, so a
    plain "fire two requests" test passes with the lock *removed*, which makes
    it a smoke test rather than a regression test (verified: it did).

    So the interleave is forced. `_persist` is where the revision is bumped, so
    a writer paused just before it has passed its check and not yet committed —
    exactly the window a second writer must not be able to enter. Returns the
    events that open and close it.
    """
    inside, release = __import__("threading").Event(), __import__("threading").Event()
    real_persist = docs._persist

    def paused(doc, **kwargs):
        if doc.id == doc_id:
            inside.set()
            release.wait(10.0)
        return real_persist(doc, **kwargs)

    monkeypatch.setattr(docs, "_persist", paused)
    return inside, release


def test_a_second_writer_cannot_enter_while_the_first_is_between_check_and_commit(monkeypatch):
    """The invariant FIX-01 adds, asserted directly.

    Without the lock the second thread reads the same revision, passes the same
    check, and commits over the first — a lost update with two 200s. With it,
    the second waits, then sees the revision the first produced and is refused.
    """
    import threading

    doc_id = _part()
    revision = docs.get_document(doc_id).revision
    inside, release = _hold_inside_the_write(monkeypatch, doc_id)
    outcomes: dict = {}

    def put(key: str, name: str):
        outcomes[key] = client.put(f"/api/documents/{doc_id}", json={
            "name": name, "resolution": 32, "expected_revision": revision}).status_code

    first = threading.Thread(target=put, args=("first", "left"))
    first.start()
    assert inside.wait(10.0), "the first write never reached the commit"

    # The first writer is parked mid-write. The second is issued now, so it
    # cannot have raced past — it either blocks on the lock or walks straight in.
    second = threading.Thread(target=put, args=("second", "right"))
    second.start()
    second.join(timeout=2.0)
    entered_anyway = not second.is_alive()

    release.set()
    first.join(timeout=10.0)
    second.join(timeout=10.0)

    assert not entered_anyway or outcomes.get("second") == 409, (
        "a second writer committed while the first was mid-write: "
        f"outcomes={outcomes}")
    assert outcomes["first"] == 200
    assert outcomes["second"] == 409


def test_a_tree_put_and_a_rename_are_both_inside_the_lock(monkeypatch):
    """A metadata-only write is the cheaper path and races just as well.

    It skips the evaluation, so it is the fastest way through
    `update_document` — a narrower window, not an absent one, and a narrower
    race is one that shows up in production rather than in a test.
    """
    import threading

    doc_id = _part()
    revision = docs.get_document(doc_id).revision
    tree = copy.deepcopy(docs.get_document(doc_id).feature_tree)
    _set_box_width(tree, 0.09)

    inside, release = _hold_inside_the_write(monkeypatch, doc_id)
    outcomes: dict = {}

    def put_tree():
        outcomes["tree"] = client.put(f"/api/documents/{doc_id}", json={
            "feature_tree": tree, "resolution": 32,
            "expected_revision": revision}).status_code

    def rename():
        outcomes["rename"] = client.put(f"/api/documents/{doc_id}", json={
            "name": "renamed", "resolution": 32,
            "expected_revision": revision}).status_code

    writer = threading.Thread(target=put_tree)
    writer.start()
    assert inside.wait(10.0)

    late = threading.Thread(target=rename)
    late.start()
    late.join(timeout=2.0)

    release.set()
    writer.join(timeout=10.0)
    late.join(timeout=10.0)

    assert outcomes["tree"] == 200
    assert outcomes["rename"] == 409


def test_two_concurrent_puts_leave_exactly_one_winner():
    """The plain shape from the plan, kept as a smoke test.

    It cannot fail deterministically — see `_hold_inside_the_write` — so it is
    here to catch a lock that broke outright rather than one that is merely
    misplaced.
    """
    doc_id = _part()
    revision = docs.get_document(doc_id).revision
    tree = client.get(f"/api/documents/{doc_id}").json()["feature_tree"]

    def put(width: float):
        edited = copy.deepcopy(tree)
        _set_box_width(edited, width)
        return client.put(f"/api/documents/{doc_id}", json={
            "feature_tree": edited, "resolution": 32,
            "expected_revision": revision})

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = sorted(f.result().status_code
                       for f in [pool.submit(put, 0.07), pool.submit(put, 0.08)])

    assert codes == [200, 409]


def test_the_lock_is_held_in_the_service_not_only_at_the_router():
    """Every caller gets the guarantee, not only the HTTP one.

    `restore_version`, the operation rollback path and the test helpers all
    reach `update_document` directly; a lock installed at the route would leave
    each of them racing.
    """
    doc_id = _part()
    revision = docs.get_document(doc_id).revision
    docs.update_document(doc_id, name="first", expected_revision=revision)

    with pytest.raises(docs.RevisionConflict):
        docs.update_document(doc_id, name="second", expected_revision=revision)


def test_a_tree_changing_update_that_evaluates_does_not_deadlock():
    """`evaluate_document` runs inside the lock; the lock is reentrant, so a
    handler already holding it is unaffected."""
    doc_id = _part()
    tree = copy.deepcopy(docs.get_document(doc_id).feature_tree)
    _set_box_width(tree, 0.09)

    updated = docs.update_document(doc_id, feature_tree=tree, resolution=32)
    assert updated is not None
    assert abs((updated.derived.bbox.max.x - updated.derived.bbox.min.x) - 0.09) < 1e-3

    # And an operation, which takes the same lock before calling in here.
    env = cad_operations.execute("set_feature_params", doc_id, {
        "feature_id": _box_id(doc_id),
        "params": {"size": {"x": 0.1, "y": 0.04, "z": 0.01}}})
    assert env["ok"] is True, env.get("error")


def test_the_operation_route_optimistic_concurrency_still_holds():
    """The existing guarantee must survive the new lock rather than replace it."""
    doc_id = _part()
    stale = docs.get_document(doc_id).revision
    cad_operations.execute("set_feature_params", doc_id, {
        "feature_id": _box_id(doc_id), "params": {"size": {"x": 0.07, "y": 0.04, "z": 0.01}}})

    env = cad_operations.execute("set_feature_params", doc_id, {
        "feature_id": _box_id(doc_id), "params": {"size": {"x": 0.08, "y": 0.04, "z": 0.01}}},
        expected_revision=stale)
    assert env["ok"] is False
    assert env["error"]["code"] == "PRECONDITION"


# ---------------------------------------------------------------------------
# FIX-02: a handler's success message reaches the poller
# ---------------------------------------------------------------------------

@jobs.handler("test.speaks")
def _speaks(_context: jobs.Context) -> dict:
    return {"message": "removed Boss A, Boss B — mark them keep and re-run",
            "removed_features": [{"name": "Boss A"}]}


@jobs.handler("test.silent")
def _silent(_context: jobs.Context) -> dict:
    return {"value": 1}


def test_a_handlers_own_message_is_what_the_job_reports():
    job = jobs.enqueue("test.speaks", "doc-fix02")
    jobs.run_pending_inline()
    finished = jobs.get(job.id)
    assert finished.status == jobs.SUCCEEDED
    assert "mark them keep" in finished.public()["message"]


def test_a_handler_that_says_nothing_still_gets_a_generic_message():
    job = jobs.enqueue("test.silent", "doc-fix02")
    jobs.run_pending_inline()
    assert jobs.get(job.id).public()["message"] == "test.silent complete"


def test_the_message_stays_in_the_result_as_well_as_on_the_job():
    """Lifting it must not move it: clients already read `result.message`, and a
    field that relocates is a client that breaks on a deploy for no gain."""
    job = jobs.enqueue("test.speaks", "doc-fix02")
    jobs.run_pending_inline()
    finished = jobs.get(job.id)
    assert finished.result["message"] == finished.public()["message"]


def test_a_release_package_reports_the_revision_it_built():
    doc_id = _part()
    assert cad_operations.execute("release_revision", doc_id, {})["ok"] is True
    started = client.post(f"/api/documents/{doc_id}/package", json={})
    job_id = started.json()["id"]
    jobs.run_pending_inline()

    assert "Revision A packaged" in jobs.get(job_id).public()["message"]
    # And through the legacy three-state shape the panel reads.
    assert client.get(f"/api/documents/{doc_id}/package/{job_id}"
                      ).json()["message"] == "Revision A packaged"


# ---------------------------------------------------------------------------
# FIX-03: a stale optimization fails once
# ---------------------------------------------------------------------------

@jobs.handler("test.stale")
def _stale(_context: jobs.Context) -> dict:
    raise jobs.NonRetryable("the inputs moved", code="REVISION_CONFLICT",
                            detail={"submitted_revision": 3, "current_revision": 5})


@jobs.handler("test.crashes")
def _crashes(_context: jobs.Context) -> dict:
    raise RuntimeError("the solver fell over")


def test_a_non_retryable_failure_is_terminal_on_the_first_detection():
    job = jobs.enqueue("test.stale", "doc-fix03")
    jobs.run_pending_inline(limit=1)

    failed = jobs.get(job.id)
    assert failed.status == jobs.FAILED
    assert failed.attempts == 1                    # not retried
    assert jobs.claim("w") is None                 # and not back on the queue


def test_the_error_distinguishes_changed_inputs_from_a_crash():
    stale = jobs.enqueue("test.stale", "doc-fix03")
    jobs.run_pending_inline(limit=1)
    assert jobs.get(stale.id).error["code"] == "REVISION_CONFLICT"
    assert "current document" in jobs.get(stale.id).error["remedy"]

    crash = jobs.enqueue("test.crashes", "doc-fix03")
    jobs.run_pending_inline(limit=1)
    # A genuine runtime failure still gets its retry — that path is for a
    # worker that died, and this is what tells the two apart.
    assert jobs.get(crash.id).status == jobs.QUEUED


def test_a_non_retryable_error_carries_both_revisions():
    """What lets a client offer 're-run against the current part' rather than
    just reporting that something failed."""
    job = jobs.enqueue("test.stale", "doc-fix03")
    jobs.run_pending_inline(limit=1)
    error = jobs.get(job.id).error
    assert error["submitted_revision"] == 3
    assert error["current_revision"] == 5


def test_worker_loss_retry_behaviour_is_unchanged():
    """The retry exists for a killed process, and must survive this."""
    from datetime import datetime, timedelta, timezone

    job = jobs.enqueue("test.silent", "doc-fix03")
    jobs.claim("worker-that-dies")
    stale_beat = (datetime.now(timezone.utc)
                  - timedelta(seconds=jobs.LEASE_SECONDS + 60)).isoformat()
    jobs._mutate(job.id, lambda j: setattr(j, "heartbeat_at", stale_beat))

    assert jobs.sweep()["recovered"] == 1
    assert jobs.get(job.id).status == jobs.QUEUED


def test_a_stale_optimization_job_fails_once_with_a_rerun_remedy():
    """End to end through the real handler, not a stand-in."""
    from app.models.schemas import OptimizationSetup
    from app.services import job_handlers

    doc_id = _part()
    submitted = docs.get_document(doc_id).revision
    cad_operations.execute("set_feature_params", doc_id, {
        "feature_id": _box_id(doc_id), "params": {"size": {"x": 0.09, "y": 0.04, "z": 0.01}}})

    job = jobs.enqueue(job_handlers.OPTIMIZE, doc_id,
                       {"setup": OptimizationSetup(resolution=32).model_dump(),
                        "resolution": 32},
                       document_revision=submitted)
    jobs.run_pending_inline(limit=1)

    failed = jobs.get(job.id)
    assert failed.status == jobs.FAILED
    assert failed.attempts == 1
    assert failed.error["code"] == "REVISION_CONFLICT"
    assert failed.error["submitted_revision"] == submitted
    assert failed.error["current_revision"] > submitted
    assert "current part" in failed.error["remedy"]


def _set_box_width(tree: dict, width: float) -> None:
    from app.cad_kernel.core.feature_tree import walk_nodes

    for node in walk_nodes(tree):
        if node.get("type") == "BoxFeature":
            node["params"]["size"]["x"] = width
            return
    raise AssertionError("no box in that tree")

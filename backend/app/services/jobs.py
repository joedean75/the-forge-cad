"""Work that outlives the request that asked for it.

A SIMP solve is minutes and a release package is a fan-out of every slow thing
in the system. Both already ran off the request path — as a FastAPI
`BackgroundTask` writing into a bounded process-local dict — and that shape has
three failures that only show in production:

- **A restart loses the job and the fact that it existed.** The browser goes on
  polling an id nothing knows about, so the panel spins until the tab closes. A
  404 there is indistinguishable from "the job aged out of the LRU", which is
  the other way that dict forgets.
- **A worker that dies mid-solve leaves nothing behind.** No failure, no retry,
  no record: the job is simply `running` forever from the client's point of
  view, which is the one state a poller cannot recover from.
- **A result cannot tell whether it is still wanted.** Ninety seconds is plenty
  of time for the document to be edited twice, and the natural code writes the
  answer in anyway.

So a job is a **row**. Status, progress and result survive a restart because
they were never only in memory. The queue is the same table, claimed with a
compare-and-set, and a lease that expires returns the job for another attempt —
which is what makes a killed worker recoverable rather than permanently
`running`.

**Why not Redis.** JOB-01 asks for a maintained queue implementation and names
Redis-backed workers as a candidate. The datastore is already transactional,
already the thing that has to be backed up, and already the boundary a second
API worker will contend on; putting the queue in it costs no new infrastructure
and no new way for a deployment to be half-configured — which is the rule every
other optional piece here follows (OCP, the LLM, Stripe all degrade to something
that still works, and a queue that is *absent* cannot). What that buys is
durability and cross-process visibility, not throughput: this is a
handful-of-jobs-per-minute system whose unit of work is minutes long. The point
at which it stops being enough is fan-out across machines, and the seam for that
is `claim` plus `HANDLERS` — a Redis or SQS adapter replaces the claim and
nothing else.

**Cancellation is cooperative and says so.** `request_cancel` moves the job to
`cancel_requested`; the handler sees it at a checkpoint it chooses and stops.
Nothing here pretends a running native call is interruptible — a `spsolve` in
the middle of a SIMP iteration is not, and a UI that offers a cancel button
which does nothing for forty seconds is worse than one that says "stopping".
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from app.services import persistence

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------

QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCEL_REQUESTED = "cancel_requested"
CANCELLED = "cancelled"

#: A job in one of these is finished and will never move again. Anything polling
#: stops here; anything sweeping skips them.
TERMINAL = frozenset({SUCCEEDED, FAILED, CANCELLED})

#: How long a claimed job may go without a heartbeat before the sweeper decides
#: its worker is gone. Generous, because the unit of work is minutes and a
#: heartbeat is only written at a progress checkpoint: reclaiming a job that is
#: merely slow would run it twice.
LEASE_SECONDS = float(os.getenv("FORGE_JOB_LEASE_SECONDS", "180"))

#: A job that has failed this many times stops being retried and stays failed.
#: One retry, not five: the failures this recovers from are a killed process,
#: and a solve that reliably crashes should say so rather than burn the worker
#: pool discovering it four more times.
MAX_ATTEMPTS = int(os.getenv("FORGE_JOB_MAX_ATTEMPTS", "2"))

#: Finished jobs are kept this long so a client that was away can still read the
#: outcome, then pruned. The *artifact* is the durable thing and it is addressed
#: by its own hash, so nothing is lost when the job row goes.
RETENTION = timedelta(hours=float(os.getenv("FORGE_JOB_RETENTION_HOURS", "24")))

#: Bounded concurrency. Every job here is CPU-bound geometry or a linear solve,
#: so more workers than cores makes each one slower without finishing any sooner
#: — and the thing being protected is the *modelling* request sharing the box.
DEFAULT_WORKERS = int(os.getenv("FORGE_JOB_WORKERS", "2"))

#: Refuse new work past this many queued jobs. Backpressure, so a caller learns
#: immediately rather than joining a queue it will time out waiting on.
MAX_QUEUED = int(os.getenv("FORGE_JOB_MAX_QUEUED", "64"))


class QueueFull(RuntimeError):
    """Backpressure: too much work already waiting."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

#: kind -> the function that does the work. Registered by the services that own
#: each job type, the same way operations register into the catalog, so adding a
#: job type is one decorator rather than an entry in a dispatch table here.
HANDLERS: Dict[str, Callable[["Context"], dict]] = {}


def handler(kind: str):
    def register(fn: Callable[["Context"], dict]):
        HANDLERS[kind] = fn
        return fn

    return register


class Cancelled(Exception):
    """Raised by a handler that noticed `context.cancelled` and stopped."""


class Context:
    """What a running handler is given, and how it reports back.

    Deliberately not the job row: a handler that could write the row directly
    would be able to declare itself succeeded, and the one thing a worker must
    own is the transition to a terminal state.
    """

    def __init__(self, job: "Job"):
        self.job = job
        self.id = job.id
        self.kind = job.kind
        self.subject_id = job.subject_id
        self.owner_id = job.owner_id
        self.inputs = job.inputs
        self.fingerprint = job.input_fingerprint
        self._last_beat = 0.0

    def progress(self, fraction: float, message: str = "") -> None:
        """Report progress, and take the pulse of the lease at the same time.

        Throttled: a handler reporting every iteration would write the row
        hundreds of times, and the sweeper only needs to know the worker is
        alive. One write a second is far inside a three-minute lease.
        """
        now = time.monotonic()
        if now - self._last_beat < 1.0 and 0.0 < fraction < 1.0:
            return
        self._last_beat = now
        _mutate(self.id, lambda j: j.observe(fraction, message))

    @property
    def cancelled(self) -> bool:
        """Whether somebody has asked for this to stop.

        Read at whatever checkpoint the handler considers safe. Nothing forces
        one: a native call in flight is not interruptible, and pretending
        otherwise is how a cancel button comes to mean nothing.
        """
        current = get(self.id)
        return current is not None and current.status == CANCEL_REQUESTED

    def stop_if_cancelled(self) -> None:
        if self.cancelled:
            raise Cancelled()


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------

class Job:
    """One unit of durable work.

    `inputs` is immutable once enqueued. A worker that loaded the *document*
    instead would be reading whatever the document says when it happens to get
    scheduled, so two runs of the same job could compute different things — and
    a retry could compute a third. `input_fingerprint` records what the document
    looked like when the work was asked for, which is what makes the result
    publishable (or refusable) when it lands.
    """

    __slots__ = ("id", "kind", "subject_id", "owner_id", "status", "progress_fraction",
                 "message", "inputs", "input_fingerprint", "document_revision",
                 "result", "error", "attempts", "worker", "created_at", "started_at",
                 "finished_at", "heartbeat_at", "applied")

    def __init__(self, **kw):
        self.id: str = kw.get("id") or str(uuid.uuid4())
        self.kind: str = kw["kind"]
        self.subject_id: str = kw["subject_id"]
        self.owner_id: Optional[str] = kw.get("owner_id")
        self.status: str = kw.get("status", QUEUED)
        self.progress_fraction: float = float(kw.get("progress_fraction", 0.0))
        self.message: str = kw.get("message", "")
        self.inputs: dict = kw.get("inputs") or {}
        self.input_fingerprint: Optional[str] = kw.get("input_fingerprint")
        self.document_revision: Optional[int] = kw.get("document_revision")
        self.result: Optional[dict] = kw.get("result")
        self.error: Optional[dict] = kw.get("error")
        self.attempts: int = int(kw.get("attempts", 0))
        self.worker: Optional[str] = kw.get("worker")
        self.created_at: str = kw.get("created_at") or _now()
        self.started_at: Optional[str] = kw.get("started_at")
        self.finished_at: Optional[str] = kw.get("finished_at")
        self.heartbeat_at: Optional[str] = kw.get("heartbeat_at")
        # Whether the result has been folded back into the document. The guard
        # against a retried delivery applying an optimization twice.
        self.applied: bool = bool(kw.get("applied", False))

    # -- serialization ------------------------------------------------------

    def to_dict(self) -> dict:
        return {slot: getattr(self, slot) for slot in self.__slots__}

    @classmethod
    def from_dict(cls, data: dict) -> "Job":
        return cls(**data)

    # -- transitions --------------------------------------------------------

    def observe(self, fraction: float, message: str) -> None:
        self.progress_fraction = max(0.0, min(1.0, float(fraction)))
        if message:
            self.message = message
        self.heartbeat_at = _now()

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL

    def public(self) -> dict:
        """What a client is told. Inputs are not in it.

        A job's inputs can carry a whole optimization setup, and the client that
        submitted it already has them. Echoing them back on every poll is the
        payload mistake this codebase keeps undoing.
        """
        return {
            "id": self.id,
            "kind": self.kind,
            "subject_id": self.subject_id,
            "status": self.status,
            "progress": round(self.progress_fraction, 3),
            "message": self.message,
            "result": self.result,
            "error": self.error,
            "attempts": self.attempts,
            "input_fingerprint": self.input_fingerprint,
            "document_revision": self.document_revision,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

# Serializes this process's read-modify-write cycles on a job row. Not a
# distributed lock and not pretending to be one — `claim` carries the
# cross-process guard. What this removes is the far commoner race of two threads
# in one worker pool touching the same row.
_STORE_LOCK = threading.RLock()


def save(job: Job) -> Job:
    persistence.save_job(job.id, job.to_dict(), owner_id=job.owner_id)
    return job


def get(job_id: str) -> Optional[Job]:
    data = persistence.load_job(job_id)
    return Job.from_dict(data) if data else None


def _mutate(job_id: str, change: Callable[[Job], Any]) -> Optional[Job]:
    with _STORE_LOCK:
        job = get(job_id)
        if job is None:
            return None
        change(job)
        return save(job)


def for_subject(subject_id: str, *, limit: int = 20) -> List[Job]:
    """Every job against one document or assembly, newest first."""
    rows = persistence.list_jobs_for_subject(subject_id)
    jobs = [Job.from_dict(r) for r in rows]
    jobs.sort(key=lambda j: j.created_at, reverse=True)
    return jobs[:limit]


def active_for_subject(subject_id: str, kind: Optional[str] = None) -> Optional[Job]:
    """The job a reloading client should reconnect to, if there is one.

    This is what makes a page refresh during a four-minute solve recoverable:
    the browser no longer has the id, but the document does.
    """
    for job in for_subject(subject_id):
        if not job.terminal and (kind is None or job.kind == kind):
            return job
    return None


# ---------------------------------------------------------------------------
# Submitting and claiming
# ---------------------------------------------------------------------------

def enqueue(kind: str, subject_id: str, inputs: Optional[dict] = None, *,
            owner_id: Optional[str] = None,
            input_fingerprint: Optional[str] = None,
            document_revision: Optional[int] = None,
            message: str = "") -> Job:
    """Put work on the queue. Raises `QueueFull` rather than accepting forever."""
    if kind not in HANDLERS:
        raise KeyError(f"no job handler for {kind!r}; known: {', '.join(sorted(HANDLERS))}")
    waiting = sum(1 for row in persistence.list_job_rows()
                  if row.get("status") == QUEUED)
    if waiting >= MAX_QUEUED:
        raise QueueFull(
            f"{waiting} jobs are already queued; try again once the queue drains")
    job = Job(kind=kind, subject_id=subject_id, owner_id=owner_id,
              inputs=inputs or {}, input_fingerprint=input_fingerprint,
              document_revision=document_revision,
              message=message or f"Queued {kind}")
    return save(job)


def request_cancel(job_id: str) -> Optional[Job]:
    """Ask a job to stop. Immediate for a queued one, cooperative for a running one."""
    with _STORE_LOCK:
        job = get(job_id)
        if job is None or job.terminal:
            return job
        if job.status == QUEUED:
            # Nothing has started, so this is not a request — it is the outcome.
            job.status = CANCELLED
            job.finished_at = _now()
            job.message = "Cancelled before it started"
        else:
            job.status = CANCEL_REQUESTED
            job.message = "Stopping at the next checkpoint"
        return save(job)


def claim(worker: str) -> Optional[Job]:
    """Take the oldest queued job, if this process wins the race for it.

    Compare-and-set on the row rather than a queue server: read the candidate,
    write it back only if it is still `queued`. `persistence.claim_job` does the
    check and the write inside one transaction, so two workers cannot both see
    it queued and both write — the second finds it `running` and moves on.

    Under SQLite that is enforced by the single-writer lock; under Postgres by
    `SELECT … FOR UPDATE`. Both are in `persistence`, because which one applies
    is a property of the dialect and not of this file.
    """
    rows = [r for r in persistence.list_job_rows() if r.get("status") == QUEUED]
    rows.sort(key=lambda r: r.get("created_at") or "")
    for row in rows:
        taken = persistence.claim_job(row["id"], expect_status=QUEUED, patch={
            "status": RUNNING,
            "worker": worker,
            "started_at": _now(),
            "heartbeat_at": _now(),
            "attempts": int(row.get("attempts", 0)) + 1,
            "message": f"Running {row.get('kind')}",
        })
        if taken is not None:
            return Job.from_dict(taken)
    return None


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

def run(job: Job) -> Job:
    """Execute a claimed job and record how it ended. Never raises."""
    from app.services import instrumentation

    fn = HANDLERS.get(job.kind)
    if fn is None:
        return _finish(job.id, FAILED, error={
            "code": "INTERNAL", "message": f"no handler for {job.kind!r}",
            "remedy": "This job kind is not served by this deployment."})
    context = Context(job)
    try:
        with instrumentation.span(f"job.{job.kind}"):
            result = fn(context)
    except Cancelled:
        return _finish(job.id, CANCELLED, message="Cancelled")
    except Exception as exc:  # noqa: BLE001 — the message is the deliverable
        _LOGGER.warning("job %s (%s) failed: %s", job.id, job.kind, exc, exc_info=True)
        return _retry_or_fail(job.id, exc)
    # A handler that returns after noticing cancellation is still cancelled: the
    # alternative is a job reported as succeeded whose work was cut short.
    if context.cancelled:
        return _finish(job.id, CANCELLED, message="Cancelled")
    return _finish(job.id, SUCCEEDED, result=result if isinstance(result, dict) else {},
                   message=f"{job.kind} complete")


def _finish(job_id: str, status: str, *, result: Optional[dict] = None,
            error: Optional[dict] = None, message: str = "") -> Optional[Job]:
    def apply(job: Job):
        job.status = status
        job.finished_at = _now()
        job.progress_fraction = 1.0 if status == SUCCEEDED else job.progress_fraction
        if result is not None:
            job.result = result
        if error is not None:
            job.error = error
        if message:
            job.message = message

    return _mutate(job_id, apply)


def _retry_or_fail(job_id: str, exc: BaseException) -> Optional[Job]:
    job = get(job_id)
    if job is None:
        return None
    detail = {"code": type(exc).__name__, "message": str(exc),
              "remedy": "Re-run the job; if it fails the same way the inputs are the problem."}
    if job.attempts < MAX_ATTEMPTS:
        # Back to the queue, keeping the attempt count and the last error, so a
        # client can see that it is being retried rather than stuck.
        def requeue(j: Job):
            j.status = QUEUED
            j.worker = None
            j.started_at = None
            j.error = detail
            j.message = f"Retrying after: {exc}"

        return _mutate(job_id, requeue)
    return _finish(job_id, FAILED, error=detail, message=str(exc))


# ---------------------------------------------------------------------------
# Recovery and retention
# ---------------------------------------------------------------------------

def sweep(now: Optional[datetime] = None) -> Dict[str, int]:
    """Recover abandoned jobs and forget finished ones.

    The abandoned case is the one that matters: a worker killed mid-solve leaves
    a row saying `running` that nothing will ever move, and a client polling it
    waits forever. A lease that has gone unheard from for `LEASE_SECONDS` is
    treated as a dead worker — requeued if it has attempts left, failed
    explicitly if not. **Never left running**, which is the state with no
    recovery.
    """
    now = now or datetime.now(timezone.utc)
    recovered = failed = pruned = 0
    for row in persistence.list_job_rows():
        job = Job.from_dict(row)
        if job.status in (RUNNING, CANCEL_REQUESTED):
            beat = _parse(job.heartbeat_at) or _parse(job.started_at)
            if beat is None or (now - beat).total_seconds() <= LEASE_SECONDS:
                continue
            if job.attempts < MAX_ATTEMPTS:
                _mutate(job.id, lambda j: (
                    setattr(j, "status", QUEUED), setattr(j, "worker", None),
                    setattr(j, "started_at", None),
                    setattr(j, "message", "Worker went away; queued for another attempt")))
                recovered += 1
            else:
                _finish(job.id, FAILED, error={
                    "code": "WORKER_LOST",
                    "message": "the worker running this job stopped reporting",
                    "remedy": "Submit it again; the inputs were not the problem."},
                    message="Worker lost")
                failed += 1
        elif job.terminal:
            finished = _parse(job.finished_at)
            if finished and now - finished > RETENTION:
                persistence.delete_job(job.id)
                pruned += 1
    return {"recovered": recovered, "failed": failed, "pruned": pruned}


# ---------------------------------------------------------------------------
# The in-process worker pool
# ---------------------------------------------------------------------------

class Workers:
    """Threads that claim and run queued jobs.

    Threads rather than processes because the work releases the GIL where it
    matters — numpy, scipy and OpenCascade all do — and a process pool would
    have to ship the document across a pipe, which is the coupling the row-based
    queue exists to avoid.

    The loop polls. A queue server would push, and this is the honest cost of
    not adding one: a job waits up to `idle_poll` seconds before a free worker
    notices it. At 0.25 s that is invisible next to a solve, and the poll is one
    indexed read against a table with tens of rows in it.
    """

    def __init__(self, count: int = DEFAULT_WORKERS, idle_poll: float = 0.25):
        self.count = count
        self.idle_poll = idle_poll
        self._threads: List[threading.Thread] = []
        self._stop = threading.Event()
        self._sweep_at = 0.0

    def start(self) -> "Workers":
        if self._threads:
            return self
        self._stop.clear()
        for n in range(self.count):
            name = f"forge-worker-{os.getpid()}-{n}"
            thread = threading.Thread(target=self._loop, name=name, args=(name,), daemon=True)
            thread.start()
            self._threads.append(thread)
        _LOGGER.info("started %d job workers", self.count)
        return self

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=timeout)
        self._threads = []

    def _loop(self, name: str) -> None:
        while not self._stop.is_set():
            try:
                self._maybe_sweep()
                job = claim(name)
                if job is None:
                    self._stop.wait(self.idle_poll)
                    continue
                run(job)
            except Exception:  # noqa: BLE001 — a worker thread must not die
                _LOGGER.exception("job worker %s hit an unexpected error", name)
                self._stop.wait(self.idle_poll)

    def _maybe_sweep(self) -> None:
        now = time.monotonic()
        if now - self._sweep_at < 30.0:
            return
        self._sweep_at = now
        try:
            sweep()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("job sweep failed")


_POOL: Optional[Workers] = None


def start_workers(count: int = DEFAULT_WORKERS) -> Workers:
    """Start the pool for this process. Idempotent."""
    global _POOL
    if _POOL is None:
        _POOL = Workers(count).start()
    return _POOL


def stop_workers() -> None:
    global _POOL
    if _POOL is not None:
        _POOL.stop()
        _POOL = None


def run_pending_inline(limit: int = 32) -> int:
    """Drain the queue on this thread. For tests and one-shot tooling.

    Not a fallback for a deployment with no workers: it runs the job on whatever
    thread called it, which for a request handler is exactly the thing the queue
    exists to stop.
    """
    done = 0
    while done < limit:
        job = claim(f"inline-{os.getpid()}")
        if job is None:
            return done
        run(job)
        done += 1
    return done


def summary() -> dict:
    """Queue depth by status — for a health endpoint and the load test."""
    counts: Dict[str, int] = {}
    for row in persistence.list_job_rows():
        counts[row.get("status", "?")] = counts.get(row.get("status", "?"), 0) + 1
    return {"counts": counts, "workers": _POOL.count if _POOL else 0,
            "max_queued": MAX_QUEUED}


def _json_safe(value: Any) -> Any:
    """Anything a handler returns has to survive a round trip through the row."""
    return json.loads(json.dumps(value, default=str))


# Registration by import, at the bottom because `job_handlers` imports `handler`
# from this module: by the time this line runs everything it needs exists. The
# same trick `cad_operations` uses for `assembly_operations`, and for the same
# reason — without it, whether `enqueue` knows about a kind depends on which
# module happened to be imported first, so a job that submits fine through the
# router raises `KeyError` from a script or a test.
from app.services import job_handlers  # noqa: E402,F401  (registers the handlers)

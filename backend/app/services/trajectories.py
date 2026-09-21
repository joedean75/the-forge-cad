"""Recording what an agent did, so it can be learned from.

Nothing in this system kept any record of the sequence of operations applied to
a document. The copilot built exactly the right structure — per step the op, its
arguments, whether it worked and why not — and then threw it away: the transcript
lived in a process-local dict capped at 200 entries and died with the process.
The originating prompt was discarded too, surviving only as a truncated document
name. Version history is manual, whole-tree, and capped.

So the one thing a fine-tune needs — (goal, observation, action, result)* — was
the one thing never written down. This module records it.

Recording hangs off :func:`record_step`, called from ``cad_operations.execute``,
which is the single chokepoint every caller passes through: REST, MCP, the
copilot, the UI and the tests. Storage is a new ``kind`` in the existing generic
records table, so there is no migration.

Recording is opt-in per session (`start`), and every failure here is swallowed —
a telemetry problem must never break a CAD edit.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import time
import uuid
from typing import Any, Dict, Iterator, List, Optional

from app.services import persistence

_LOGGER = logging.getLogger(__name__)

# The session a step belongs to. A ContextVar rather than a parameter threaded
# through every layer: execute() is called from HTTP handlers, background jobs
# and tests, and none of those want to pass a session id they do not care about.
# ContextVars are also task-local, so concurrent requests never interleave.
_SESSION: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "forge_trajectory_session", default=None)
_TOKENS = contextvars.ContextVar("forge_trajectory_tokens", default=())

# Bound a single trajectory. A runaway agent loop should not write an unbounded
# row; 2000 steps is far beyond any real session.
MAX_STEPS = 2000


def _now() -> float:
    return time.time()


def start(
    doc_id: Optional[str] = None,
    *,
    goal: str = "",
    kind: str = "session",
    actor: str = "agent",
    owner_id: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> str:
    """Begin recording. Returns the trajectory id; pass it to :func:`finish`."""
    trajectory_id = str(uuid.uuid4())
    record = {
        "id": trajectory_id,
        "doc_id": doc_id,
        "owner_id": owner_id,
        # What the agent was asked to do. Without this a step sequence is not a
        # training example, just a log.
        "goal": goal,
        "kind": kind,          # session | copilot | text_to_cad | decompiled
        "actor": actor,        # agent | user | copilot | system
        "started_at": _now(),
        "finished_at": None,
        "outcome": None,
        "steps": [],
        "events": [],
        "parent_id": _SESSION.get(),
        **(metadata or {}),
    }
    try:
        _write(record)
    except Exception:
        _LOGGER.warning("failed to start trajectory", exc_info=True)
    token = _SESSION.set(trajectory_id)
    _TOKENS.set((*_TOKENS.get(), (trajectory_id, token)))
    return trajectory_id


def finish(trajectory_id: Optional[str] = None, *, outcome: str = "done",
           summary: str = "") -> Optional[dict]:
    """Close a trajectory and stop recording."""
    trajectory_id = trajectory_id or _SESSION.get()
    if not trajectory_id:
        return None
    record = None
    try:
        record = _read(trajectory_id)
        if record is not None:
            record["finished_at"] = _now()
            record["outcome"] = outcome
            record["summary"] = summary
            _write(record)
    except Exception:
        _LOGGER.warning("failed to finish trajectory", exc_info=True)
    finally:
        tokens = _TOKENS.get()
        if tokens and tokens[-1][0] == trajectory_id:
            _SESSION.reset(tokens[-1][1])
            _TOKENS.set(tokens[:-1])
    return record


def event(event_kind: str, **detail) -> None:
    """Bounded diagnostic metadata; callers never pass credentials/image bytes."""
    if not current():
        return
    try:
        record = _read(current())
        if record is not None:
            events = record.setdefault("events", [])
            if len(events) < MAX_STEPS:
                events.append({"i": len(events), "ts": _now(), "event": event_kind, **detail})
                _write(record)
    except Exception:
        _LOGGER.warning("dropped trajectory event %s", event_kind, exc_info=True)


@contextlib.contextmanager
def recording(doc_id: Optional[str] = None, **kwargs) -> Iterator[str]:
    """Record everything done inside this block as one trajectory."""
    trajectory_id = start(doc_id, **kwargs)
    try:
        yield trajectory_id
    except Exception:
        finish(trajectory_id, outcome="error")
        raise
    else:
        finish(trajectory_id, outcome="done")


def current() -> Optional[str]:
    return _SESSION.get()


def record_step(
    op: str,
    doc_id: str,
    args: Optional[dict],
    envelope: dict,
    *,
    obs_before: Optional[dict] = None,
    latency_ms: Optional[float] = None,
    rationale: str = "",
) -> None:
    """Append one (action, result) pair to the active trajectory.

    A no-op when no session is recording, which is the default — the cost on the
    normal edit path is one ContextVar read.
    """
    trajectory_id = _SESSION.get()
    if not trajectory_id:
        return
    try:
        record = _read(trajectory_id)
        if record is None or len(record["steps"]) >= MAX_STEPS:
            return
        record["steps"].append({
            "i": len(record["steps"]),
            "ts": _now(),
            "op": op,
            "args": args or {},
            "rationale": rationale,
            "ok": envelope.get("ok"),
            "error": envelope.get("error"),
            "warnings": envelope.get("warnings") or [],
            "result": envelope.get("result") or {},
            # Both sides of the transition: an example needs the state the action
            # was chosen from, not only the state it produced.
            "obs_before": obs_before,
            "obs_after": envelope.get("state"),
            "delta": envelope.get("delta"),
            "latency_ms": latency_ms,
        })
        _write(record)
    except Exception:  # noqa: BLE001 — telemetry must never break an edit
        _LOGGER.warning("failed to record trajectory step %s", op, exc_info=True)


# ------------------------------------------------------------------ storage --

def _write(record: dict) -> None:
    persistence.save_trajectory(record["id"], record)


def _read(trajectory_id: str) -> Optional[dict]:
    return persistence.load_trajectory(trajectory_id)


def get(trajectory_id: str) -> Optional[dict]:
    return _read(trajectory_id)


def list_for_document(doc_id: str) -> List[dict]:
    return persistence.list_trajectories_for_document(doc_id)


def list_all() -> List[dict]:
    out = [_read(t) for t in persistence.list_trajectories()]
    return sorted((r for r in out if r), key=lambda r: r.get("started_at") or 0)


def delete(trajectory_id: str) -> bool:
    return persistence.delete_trajectory(trajectory_id)


def corpus_stats(records: Optional[List[dict]] = None) -> Dict[str, Any]:
    """What the recorded corpus is made of, and what went wrong in it.

    The counterpart to `decompiler.corpus_report`, which asks whether a *tree*
    can be recovered. This asks what the *agent* did: how many calls it took,
    how many failed, and — the column that matters — under which code.

    The failures are the valuable half. A corpus of only successes never shows a
    model what recovering from a `PRECONDITION` looks like, and recovery is most
    of what an agent does; `by_error_code` is how you see whether the corpus
    contains any, and which kind. Nothing is written here: measuring must not
    also record.
    """
    records = list_all() if records is None else records
    codes: Dict[str, int] = {}
    outcomes: Dict[str, int] = {}
    kinds: Dict[str, int] = {}
    steps = failures = warned = 0

    for record in records:
        outcomes[record.get("outcome") or "open"] = \
            outcomes.get(record.get("outcome") or "open", 0) + 1
        kinds[record.get("kind") or "edit"] = kinds.get(record.get("kind") or "edit", 0) + 1
        for step in record.get("steps") or []:
            steps += 1
            if step.get("warnings"):
                warned += 1
            error = step.get("error") or {}
            code = error.get("code")
            if code:
                failures += 1
                codes[code] = codes.get(code, 0) + 1

    return {
        "trajectories": len(records),
        "steps": steps,
        "failed_steps": failures,
        "failure_rate": round(failures / steps, 4) if steps else 0.0,
        "warned_steps": warned,
        "mean_steps": round(steps / len(records), 2) if records else 0.0,
        "by_error_code": dict(sorted(codes.items(), key=lambda kv: -kv[1])),
        "by_outcome": outcomes,
        "by_kind": kinds,
    }


# ------------------------------------------------------------------ export --

def export_jsonl(records: Optional[List[dict]] = None, *, include_catalog: bool = True,
                 min_steps: int = 1) -> str:
    """Recorded runs as JSONL training lines, one **planner turn** per line.

    Delegates to `datasets.corpus`, which builds each line with
    `copilot.planner_request` — the function a live turn uses. This used to
    assemble its own: the prose system prompt and an alternating tool-call
    conversation, where a turn is one rebuilt user message and one tool call. A
    corpus in a shape inference never sends teaches an API that does not exist,
    which is the mistake CLAUDE.md records for the three action-space generators.

    Each line carries its `meta` (source, model, whether the state was recorded
    exactly), which a trainer that rejects unknown keys should strip —
    `python -m datasets.build` writes the training lines and the provenance as
    separate files for exactly that reason, and is the path to prefer.

    ``include_catalog`` is accepted and ignored: the system prompt is part of the
    turn now, not an optional preamble.
    """
    from datasets import corpus

    lines: List[str] = []
    for record in (records if records is not None else list_all()):
        if len(record.get("steps") or []) < min_steps:
            continue  # a trajectory with no actions teaches nothing
        for sample in corpus.samples_from_record(record, include_unfinished=True):
            body = sample.line()
            body["meta"] = sample.meta
            lines.append(json.dumps(body))
    return "\n".join(lines)


def _catalog_text() -> str:
    """The action space as a system prompt.

    The *same* generator the copilot's prompt uses (`services.action_space`).
    This used to build its own, so the corpus described a different action space
    from the one at inference — training a model to drive an API that does not
    exist. The two agreed only by coincidence, and both listed parameter names
    with no types, no units and no required list.
    """
    from app.services import action_space

    return action_space.system_prompt(with_tools=False)

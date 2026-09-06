"""What each mode needs, and whether what it is showing is still true.

The editor has six modes on one document — Model, Optimize, Analysis, Make,
Validate, Machine — and until now each decided for itself what "ready" meant and
what to do with a result. That produced six different answers to the same four
questions, and the differences were invisible until somebody hit one:

- **Nothing distinguished "no result yet" from "no inputs yet".** An Analysis
  panel with no load cases and an Analysis panel nobody had run looked
  identical: empty. One of them is a button press away and the other is not,
  and the panel could not say which.
- **Freshness was per-panel and lived in the browser.** Clearing a result on the
  next edit is right for the tab that made the edit, and useless after a reload
  or for a second viewer — who saw a stress figure with no way to tell what body
  it was about.
- **A queued job looked like nothing happening.** The only state a panel could
  show was its own `busy` flag, which dies with the tab.

So each mode declares its prerequisites and which derived product it displays,
and `status()` answers with one vocabulary for all of them:

    blocked   — a prerequisite is missing; name it, because it is the next action
    ready     — everything it needs is there and nothing has been computed
    queued    — work is waiting for a worker
    running   — work is in progress, with a fraction
    current   — the held result was computed from exactly these inputs
    stale     — a result is held but the inputs have moved
    failed    — the last attempt failed, with the reason

`current` and `stale` come from `derived_cache` fingerprints, which is what
makes them survive a reload; `queued`/`running`/`failed` come from the job rows,
which is what makes them survive the tab. Neither is a flag anybody has to
remember to set.

**Model has no product and that is not an omission.** It displays the body
itself, which is the document — there is nothing downstream of it to go stale.
Giving it a fake product so the table looks uniform would mean inventing a
freshness answer for the one thing that is always current by definition.
"""

from __future__ import annotations

from typing import Callable, Dict, List, NamedTuple, Optional

from app.services import derived_cache, jobs


class Requirement(NamedTuple):
    """A prerequisite, and what to say when it is missing.

    The message is the point: "blocked" alone sends someone hunting. Every one
    of these names the step that unblocks it, which is the same contract
    `op_errors` remedies follow.
    """

    name: str
    holds: Callable[[object], bool]
    remedy: str


def _has_body(doc) -> bool:
    from app.services import feature_ops

    return feature_ops.has_solid(doc.feature_tree) and not doc.derived.error


def _has_material(doc) -> bool:
    return bool(doc.material_id)


def _has_loads(doc) -> bool:
    setup = getattr(doc, "optimization", None)
    return bool(setup and setup.load_cases)


def _has_spec(doc) -> bool:
    return bool((doc.spec or {}).get("requirements"))


def _has_release(doc) -> bool:
    return bool(doc.revisions)


BODY = Requirement("body", _has_body,
                   "Add a feature — the document has no solid yet.")
MATERIAL = Requirement("material", _has_material,
                       "Assign a material with set_material; stress and mass depend on it.")
LOADS = Requirement("loads", _has_loads,
                    "Define at least one load case and support on the optimization setup.")
SPEC = Requirement("spec", _has_spec,
                   "Write the requirements with set_spec; there is nothing to score against.")
RELEASE = Requirement("release", _has_release,
                      "Cut a revision with release_revision before packaging.")


class Mode(NamedTuple):
    name: str
    requires: tuple
    #: The derived product this mode displays, if any. `derived_cache.PRODUCTS`
    #: owns what invalidates it.
    product: Optional[str]
    #: Job kinds whose progress belongs to this mode.
    job_kinds: tuple
    description: str


MODES: Dict[str, Mode] = {m.name: m for m in [
    Mode("model", (), None, (),
         "the body itself — nothing downstream of it to go stale"),
    Mode("optimize", (BODY, MATERIAL, LOADS), None, ("optimize",),
         "a generative study, which rewrites the tree"),
    Mode("analysis", (BODY, MATERIAL, LOADS), "analysis", ("analysis",),
         "linear-static FEA: stress, displacement, safety factor"),
    Mode("make", (BODY,), "dfm", (),
         "manufacturability for the selected process"),
    Mode("validate", (BODY, SPEC), "spec", (),
         "the score against the document's requirements"),
    Mode("machine", (BODY,), "cam", (),
         "the toolpath and the program written from it"),
]}

#: Not a mode but the same shape, and the one place a release is a prerequisite.
#:
#: The material is a prerequisite rather than a nicety: the manifest states the
#: mass and the one-line BOM is priced from it, so a package without one is a
#: handover document with a blank where the part's weight goes. It carries the
#: `package` product, so a package built and then edited past reports `stale`
#: rather than sitting at `ready` looking like nothing has been done.
PACKAGE = Mode("package", (BODY, MATERIAL, RELEASE), "package", ("release_package",),
               "the handover zip for a released revision")


class Status(NamedTuple):
    mode: str
    state: str
    detail: str
    #: Populated only when a job is in flight.
    job_id: Optional[str] = None
    progress: Optional[float] = None
    #: The fingerprint the held result was computed under, when there is one.
    fingerprint: Optional[str] = None
    computed_at: Optional[str] = None
    missing: tuple = ()

    def as_dict(self) -> dict:
        out = {"mode": self.mode, "state": self.state, "detail": self.detail}
        for key, value in (("job_id", self.job_id), ("progress", self.progress),
                           ("fingerprint", self.fingerprint),
                           ("computed_at", self.computed_at)):
            if value is not None:
                out[key] = value
        if self.missing:
            out["missing"] = list(self.missing)
        return out


BLOCKED, READY, QUEUED, RUNNING, CURRENT, STALE, FAILED = (
    "blocked", "ready", "queued", "running", "current", "stale", "failed")


def status(doc, mode: Mode, settings: Optional[dict] = None) -> Status:
    """One mode's state on one document.

    Order matters and is not arbitrary. Prerequisites first, because a running
    job on a document whose material was since cleared is still going to produce
    something nobody can use. Then a live job, because "it is being computed" is
    the most actionable thing to say. Then the held result's freshness. A recent
    failure is reported only when there is nothing better — a failed attempt
    followed by a successful one is not news.
    """
    missing = tuple(req.name for req in mode.requires if not req.holds(doc))
    if missing:
        remedies = "; ".join(req.remedy for req in mode.requires
                             if req.name in missing)
        return Status(mode.name, BLOCKED, remedies, missing=missing)

    job = _live_job(doc.id, mode.job_kinds)
    if job is not None:
        state = QUEUED if job.status == jobs.QUEUED else RUNNING
        return Status(mode.name, state, job.message, job_id=job.id,
                      progress=job.progress_fraction)

    if mode.product is None:
        return Status(mode.name, READY, mode.description)

    freshness = derived_cache.freshness(doc, mode.product, settings)
    held = derived_cache.held(doc, mode.product)
    if freshness == "current":
        return Status(mode.name, CURRENT, f"{mode.product} matches the current body",
                      fingerprint=held.fingerprint, computed_at=held.computed_at)
    if freshness == "stale":
        return Status(mode.name, STALE,
                      "the inputs moved since this was computed — run it again",
                      fingerprint=held.fingerprint, computed_at=held.computed_at)

    failure = _recent_failure(doc.id, mode.job_kinds)
    if failure is not None:
        return Status(mode.name, FAILED, failure.message or "the last attempt failed",
                      job_id=failure.id)
    return Status(mode.name, READY, mode.description)


def _live_job(doc_id: str, kinds: tuple):
    if not kinds:
        return None
    for job in jobs.for_subject(doc_id):
        if job.kind in kinds and not job.terminal:
            return job
    return None


def _recent_failure(doc_id: str, kinds: tuple):
    """The most recent job for this mode, but only if it failed.

    Scanning past a failure to an older success would be wrong in the other
    direction: the newest attempt is the one that describes the current state.
    """
    if not kinds:
        return None
    for job in jobs.for_subject(doc_id):
        if job.kind in kinds:
            return job if job.status in (jobs.FAILED, jobs.CANCELLED) else None
    return None


def overview(doc, settings_by_mode: Optional[Dict[str, dict]] = None) -> List[dict]:
    """Every mode's state, in the order the editor lays them out."""
    settings_by_mode = settings_by_mode or {}
    return [status(doc, mode, settings_by_mode.get(name)).as_dict()
            for name, mode in MODES.items()]

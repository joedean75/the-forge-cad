"""The long jobs, moved off the request path onto the durable queue.

Both already ran in the background — as FastAPI `BackgroundTask`s writing into a
process-local dict — so what is new is not asynchrony. It is that the job
survives the process, that a worker dying is recoverable rather than a permanent
`running`, and that a result which lands into a document that has since moved is
**refused rather than applied**.

That last one is the part worth reading. A SIMP solve replaces the whole feature
tree with an `OptimizeFeature` wrapping the body it started from. Folding that in
on top of edits made during the four minutes it ran does not merge with them —
it deletes them, silently, and the user's next undo restores a tree they never
saw. So the revision is carried on the job and checked under the document's
write lock immediately before the tree is replaced.

Handlers take a `jobs.Context` and return a plain dict. They do not touch the
job row: the worker owns the transition to a terminal state, and a handler that
could declare itself succeeded would be able to do so having done nothing.
"""

from __future__ import annotations

import logging
from typing import Optional

from app.services import jobs

_LOGGER = logging.getLogger(__name__)

OPTIMIZE = "optimize"
RELEASE_PACKAGE = "release_package"
ANALYSIS = "analysis"


# ---------------------------------------------------------------------------
# Generative optimization
# ---------------------------------------------------------------------------

def _progress_fraction(state: dict) -> tuple[float, str]:
    """The staged progress the UI has always shown, read off the solver state.

    The stages are not equal fractions of the wall clock and the split is not a
    guess: meshing is a voxelisation, the SIMP loop is `max_iter` linear solves,
    and post-processing is one marching-cubes pass. A linear bar over iterations
    alone would sit at zero through the meshing and jump at the end.
    """
    stage = state.get("stage", "optimizing")
    iteration = int(state.get("iteration", 0))
    max_iter = max(int(state.get("max_iter", 1)), 1)
    message = state.get("message", "Optimizing")
    if stage == "meshing":
        return 0.15, message
    if stage == "optimizing":
        return 0.2 + 0.6 * min(1.0, iteration / max_iter), message
    if stage == "postprocessing":
        return 0.9, message
    return 0.0, message


@jobs.handler(OPTIMIZE)
def run_optimize(context: jobs.Context) -> dict:
    """A SIMP solve, folded back into the document it was asked about."""
    import threading

    from app.models.schemas import OptimizationSetup
    from app.services import document_service as docs, simp_optimizer

    setup = OptimizationSetup(**context.inputs["setup"])
    resolution = int(context.inputs.get("resolution", 128))
    expected_revision: Optional[int] = context.job.document_revision

    progress = simp_optimizer.ProgressState()
    stop_polling = threading.Event()

    def relay():
        """Carry the solver's own progress onto the job row.

        A thread rather than a callback because `run_simp_from_solid` reports
        into a `ProgressState` it owns and polling it is the interface that
        already exists. It is also where cancellation crosses over: the job's
        `cancel_requested` lives in the datastore and the solver reads a flag,
        so something has to be looking at both.
        """
        while not stop_polling.wait(0.5):
            fraction, message = _progress_fraction(progress.read())
            context.progress(fraction, message)
            if context.cancelled:
                progress.cancel()

    poller = threading.Thread(target=relay, name=f"optimize-progress-{context.id}",
                              daemon=True)
    poller.start()
    try:
        doc = docs.run_document_optimize(
            context.subject_id, setup, resolution, progress,
            expected_revision=expected_revision)
    except simp_optimizer.SolveCancelled as stopped:
        _LOGGER.info("optimize job %s cancelled: %s", context.id, stopped)
        raise jobs.Cancelled() from stopped
    except docs.RevisionConflict as conflict:
        # Not a crash and not worth retrying: the answer was computed correctly
        # and the question changed. It used to raise `RuntimeError`, which the
        # runner treats as transient — so a stale result was recomputed from the
        # *same* submitted revision, found the same conflict, and burned a
        # worker slot doing it. Typed as non-retryable, it is terminal on the
        # first detection.
        #
        # Both revisions are in the detail, which is what lets a client offer
        # "re-run against the current part" rather than just reporting a failure.
        raise jobs.NonRetryable(
            f"the part was edited while the study ran, so the result was not "
            f"applied ({conflict})",
            code="REVISION_CONFLICT",
            remedy="Re-run the study against the current part; the geometry it "
                   "was computed from no longer exists.",
            detail={"submitted_revision": conflict.expected,
                    "current_revision": conflict.actual},
        ) from conflict
    finally:
        stop_polling.set()
        poller.join(timeout=2.0)

    removed = (doc.feature_tree.get("root", {}).get("params", {})
               .get("setup", {}).get("removed_features", []))
    message = "Optimization complete"
    if removed:
        names = ", ".join(r["name"] for r in removed[:3])
        more = f" +{len(removed) - 3} more" if len(removed) > 3 else ""
        message = (f"Optimization complete — removed {names}{more}. "
                   f"Mark features to keep and re-run.")
    return {"message": message, "removed_features": removed,
            "document_revision": doc.revision}


# ---------------------------------------------------------------------------
# Release packages
# ---------------------------------------------------------------------------

@jobs.handler(RELEASE_PACKAGE)
def run_release_package(context: jobs.Context) -> dict:
    """Build the handover zip for a released revision.

    No revision check, deliberately, and it is not an oversight: a package is
    built from the *revision's* feature tree, materialised as an ephemeral
    document, so it is pinned to a snapshot that cannot move. The manifest
    already states `document_matches_revision`, and a document that has moved on
    since the release is the normal case rather than a conflict.
    """
    from app.services import release_package

    context.progress(0.1, "Evaluating the released revision")
    result = release_package.build(
        context.subject_id,
        revision=context.inputs.get("revision"),
        drawing_spec=context.inputs.get("drawing_spec"))
    message = f"Revision {result['revision']} packaged"
    context.progress(1.0, message)
    # Named in the result, because that is where `jobs.run` reads the message it
    # stores on the job. A progress note is overwritten by the terminal
    # transition; only the returned dict survives it.
    return {**result, "message": message}


# ---------------------------------------------------------------------------
# Linear-static FEA
# ---------------------------------------------------------------------------

@jobs.handler(ANALYSIS)
def run_analysis(context: jobs.Context) -> dict:
    """A solve, published against the fingerprint it was asked about.

    `simulate` stays inline and that is deliberate: at the default element
    budget it is well under a second on the fixtures, and moving a fast call off
    the request path buys a poll loop and nothing else — the roadmap's own rule
    is to retain fast operations inline where the evidence supports it. What
    this exists for is the other end: `elements` raises the mesh budget, and
    above about 20k free DOFs the direct solve is abandoned for CG and the wall
    clock goes to minutes.

    The publication is the interesting half. The fingerprint is taken *before*
    the solve; `derived_cache.publish` recomputes it afterwards and refuses the
    result if the part, the material or the load cases moved while it ran. A
    stress figure from a body that has since been thinned is the single most
    dangerous stale number in the system.
    """
    from app.services import derived_cache, document_analysis, document_service as docs

    doc = docs.get_document(context.subject_id)
    if doc is None:
        raise RuntimeError(f"document {context.subject_id} no longer exists")

    resolution = int(context.inputs.get("resolution", 128))
    elements = context.inputs.get("elements")
    settings = {"elements": elements,
                "load_cases": [lc.model_dump()
                               for lc in ((doc.optimization.load_cases if doc.optimization
                                           else None) or [])]}

    context.progress(0.1, "Meshing the body for the solve")
    report = document_analysis.analyse_document(
        context.subject_id, resolution=resolution,
        elements=int(elements) if elements else None)
    context.stop_if_cancelled()

    context.progress(0.95, "Publishing the result")
    # Re-read: the solve took time, and the document object this started from
    # may have been replaced by an edit.
    doc = docs.get_document(context.subject_id)
    kept = derived_cache.publish(
        doc, "analysis", report, settings=settings, fingerprint=context.fingerprint)
    if kept:
        docs.persist_derived(doc)
    return {**report, "published": kept,
            "note": None if kept else
                    "the part changed while the solve ran, so this result "
                    "describes an older body and was not filed against the document"}

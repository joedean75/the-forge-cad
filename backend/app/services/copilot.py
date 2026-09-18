"""Forge Copilot — an agentic loop over the operation API.

Given a natural-language goal, the copilot repeatedly: summarizes the document
state, asks the LLM for the single next operation to run (or "done"), and
dispatches it through ``cad_operations`` — the same serializable op path the UI
and MCP server use. This turns text-to-CAD from single-shot generation into a
verify-and-iterate engineering loop (roadmap feature 01).

The planner (the LLM call) is injected so the loop is testable without a key: pass
a scripted ``planner`` to :func:`run`. Access control + quota metering are the
caller's responsibility via the ``meter`` callback and pre-checked edit rights.
"""

from __future__ import annotations

import json
import logging
import time
import functools
from copy import deepcopy
from contextvars import ContextVar
from typing import Any, Callable, Dict, List, Optional

from app.services import cad_operations, copilot_context, document_service, llm_client
from app.services.llm_errors import LLMFailure

_LOGGER = logging.getLogger(__name__)

MAX_STEPS_DEFAULT = 8
MAX_STEPS_CAP = 16

#: An assembly run is a different size of job. One part is a whole nested
#: copilot run, so "design four parts, place them, mate them and check" is
#: already a dozen steps before anything goes wrong — budgets tuned for editing
#: one body stop it halfway through and report it as out of budget, which looks
#: exactly like a loop that gave up.
ASSEMBLY_MAX_STEPS_DEFAULT = 48
ASSEMBLY_MAX_STEPS_CAP = 120

#: Wall-clock limits, per subject for the same reason. Doubling an assembly's
#: step budget changes nothing if a build of that length hits the ten-minute
#: limit first, and each `build_part` is a whole nested run of LLM turns. The
#: assembly figure stays under the client's twenty-minute polling deadline, or
#: the browser would abandon a run the server is still executing.
MAX_SECONDS_DEFAULT = 600
ASSEMBLY_MAX_SECONDS_DEFAULT = 1080

#: The image types a vision model is sent. Anything else is refused at the
#: route rather than forwarded to a provider that answers with its own error.
IMAGE_MIME_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif")

#: How many reference images one goal may carry. Every image is re-sent on every
#: step, so the count multiplies the cost of the whole run, not of one call.
MAX_IMAGES = 6


def step_cap(subject: str) -> int:
    return ASSEMBLY_MAX_STEPS_CAP if subject == "assembly" else MAX_STEPS_CAP


def step_default(subject: str) -> int:
    return ASSEMBLY_MAX_STEPS_DEFAULT if subject == "assembly" else MAX_STEPS_DEFAULT


def seconds_default(subject: str) -> float:
    return ASSEMBLY_MAX_SECONDS_DEFAULT if subject == "assembly" else MAX_SECONDS_DEFAULT

# A decision is either {"op", "args", "rationale"} or {"done": true, "summary"}.
Planner = Callable[[str, List[dict], dict], dict]

#: The quota meter of the run in progress, so a *nested* run is charged to the
#: same account. `build_part` runs a whole part copilot inside one assembly
#: step; without this every operation it performs would be free, and the cheapest
#: way to spend someone else's quota would be to ask for an assembly. A
#: ContextVar rather than an argument for the reason `services/caller` is one:
#: threading it through `execute` would put metering inside every handler.
_ACTIVE_METER: ContextVar[Optional[Callable[[], None]]] = ContextVar(
    "forge_copilot_meter", default=None)


def nested_meter() -> Optional[Callable[[], None]]:
    """The meter of the enclosing run, for a sub-run to charge against."""
    return _ACTIVE_METER.get()


#: The model this run was asked to use, carried the same way and for the same
#: reason: a build that planned the assembly with one model and designed its
#: parts with another is not a comparison of anything.
_ACTIVE_MODEL: ContextVar[Optional[str]] = ContextVar(
    "forge_copilot_model", default=None)


def nested_model() -> Optional[str]:
    """The model of the enclosing run, for a sub-run to use."""
    return _ACTIVE_MODEL.get()


_ACTIVE_CONTEXT: ContextVar[Optional[dict]] = ContextVar("forge_copilot_context", default=None)


def nested_context() -> Optional[dict]:
    """The authorised assembly brief and references for a nested part run."""
    return _ACTIVE_CONTEXT.get()


def _doc_summary(doc) -> dict:
    """Compact, LLM-friendly snapshot of the current body.

    Lives in ``services.observation`` now: the same summary is what every
    operation returns as ``state``, so the planner and the API cannot drift.
    """
    from app.services import observation

    return observation.summarize(doc)


def _fetch(subject: str, subject_id: str):
    """The thing this run is editing — a part document or an assembly.

    `cad_operations.execute` already routes operations through a subject table
    rather than special-casing the one caller that noticed; this is the same
    idea for the half of the loop that lives here.
    """
    from app.services import assembly_service

    if subject == "assembly":
        return assembly_service.get_assembly(subject_id)
    return document_service.get_document(subject_id)


def _summary(subject: str, obj) -> dict:
    """The compact state the planner reads, in that subject's own shape.

    `summarize_assembly` states `kind: "assembly"` for the reason the catalog
    publishes a subject at all — an agent that infers which it is holding from
    the presence of a key will eventually infer wrong.
    """
    from app.services import observation

    return (observation.summarize_assembly(obj) if subject == "assembly"
            else observation.summarize(obj))


def _plan_next(goal: str, transcript: List[dict], summary: dict,
               subject: str = "document", model: Optional[str] = None,
               images: Optional[List[str]] = None, history: str = "",
               enabled_tools: Optional[set[str]] = None,
               renders: Optional[List[dict]] = None) -> dict:
    """Ask the LLM for the next operation. Returns a decision dict.

    Two paths, and which one ran is recorded on the decision as ``_via`` the
    same way `AnalysisReport.solver` marks a closed-form fallback: a prompt is
    not a measurement, and a benchmark comparing runs has to be able to tell a
    schema-constrained call from a parsed one.
    """
    from app.services import action_space

    if not llm_client.is_configured():
        return {"done": True, "summary": "No LLM configured — the copilot needs an LLM key.", "_unconfigured": True}

    steps = [
        {
            "op": s.get("op"),
            "args": s.get("args"),
            "ok": s.get("ok"),
            "error": s.get("error"),
            "warnings": s.get("warnings"),
            "result": s.get("result"),
        }
        for s in transcript
        if s.get("kind") in ("op", "verification", "plan", "discovery")
    ]
    user = (
        (f"Previous conversation about this {subject}:\n{history}\n\n"
         "Use this as context for the new request, including unfinished work. "
         "Past operations already ran; do not repeat them blindly. The current "
         "state below is authoritative if it differs from the conversation.\n\n"
         if history else "") +
        f"Goal: {goal}\n\n"
        f"Current document:\n{json.dumps(summary)}\n\n"
        f"Steps so far in this run:\n{json.dumps(steps) if steps else '(none yet)'}\n\n"
        "Take the next single action."
    )
    with_tools = llm_client.supports_tools()
    system = action_space.system_prompt(with_tools=with_tools, subject=subject, names=enabled_tools)
    definitions = action_space.tools(subject)
    if enabled_tools is not None:
        definitions = [t for t in definitions if t["function"]["name"] in enabled_tools]
        definitions += copilot_context.control_tools()
        system += copilot_context.control_rules(subject)
        if not with_tools:
            # Prose providers need nesting and descriptions too, not just a
            # flat parameter-name list. Send the same selected JSON schemas.
            system = (action_space.system_prompt(with_tools=False, subject=subject, names=set())
                      + copilot_context.control_rules(subject)
                      + "\nAvailable operation schemas (respond with op/args JSON):\n"
                      + json.dumps(definitions))
        user += "\n\nAdditional operation directory (load schemas with discover_operations):\n" + json.dumps(
            copilot_context.tool_directory(subject, enabled_tools))
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": _user_content(user, images, renders)},
    ]
    message = llm_client._post_chat(
        messages, timeout=60.0, max_tokens=2400,
        tools=definitions if with_tools else None,
        model=model)

    calls = message.get("tool_calls") or []
    if len(calls) > 1:
        raise LLMFailure("PROVIDER_PROTOCOL", "The planner returned multiple actions.",
                         "Retry with one action at a time.", retryable=True)
    if calls:
        function = (calls[0] or {}).get("function") or {}
        raw_args = function.get("arguments") or "{}"
        try:
            parsed = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
        except json.JSONDecodeError as exc:
            raise ValueError(f"tool call arguments were not JSON: {raw_args[:200]}") from exc
        if not isinstance(parsed, dict):
            raise LLMFailure("PROVIDER_PROTOCOL", "Tool arguments must be an object.", "Retry one action.", retryable=True)
        if function.get("name") == "finish_task":
            if parsed.get("outcome") not in ("completed", "answered", "needs_input", "paused") or not str(parsed.get("summary") or "").strip():
                raise LLMFailure("PROVIDER_PROTOCOL", "The completion result was incomplete.", "Report the outcome and summary.", retryable=True)
            return {"done": True, "summary": parsed["summary"], "result_kind": parsed["outcome"]}
        return {"op": function.get("name"), "args": parsed,
                "rationale": (message.get("content") or "").strip()[:200], "_via": "tools"}

    content = message.get("content") or ""
    if with_tools:
        if not content.strip():
            raise LLMFailure("PROVIDER_EMPTY", "The planner returned an empty response.", "Retry the next step.", retryable=True)
        # Text can answer a question; only an explicit completion claims a build.
        return {"done": True, "summary": content.strip()[:2000], "result_kind": "answered", "_via": "tools"}

    raw = llm_client._extract_json(content)
    if raw is None:
        raise ValueError(f"planner returned no JSON: {content[:200]}")
    return {**json.loads(raw), "_via": "prose"}


def _user_content(text: str, images: Optional[List[str]],
                  renders: Optional[List[dict]] = None):
    """The user turn, with the reference images attached when there are any.

    Sent on **every** turn, not only the first. The planner is stateless between
    steps, since each call is rebuilt from the goal, the summary and the
    transcript. An image sent once is a reference the model has lost by step two.
    The client downscales before upload, which is what bounds the repeated cost.
    """
    images, renders = list(images or []), list(renders or [])
    if not images and not renders:
        return text
    # **Every image is labelled, and the labels are interleaved.** Once a render
    # of what was built travels beside the user's reference photo, an unlabelled
    # list makes the two indistinguishable — and telling them apart is the whole
    # point of the comparison the review exists to make.
    parts: list = [{"type": "text", "text": text}]
    for index, uri in enumerate(images, start=1):
        parts.append({"type": "text",
                      "text": f"Image {index}: the user's reference for this goal"
                              + (f" ({index} of {len(images)}, in the order they gave them)."
                                 if len(images) > 1 else ".")})
        parts.append({"type": "image_url", "image_url": {"url": uri}})
    for offset, render in enumerate(renders, start=len(images) + 1):
        parts.append({"type": "text",
                      "text": f"Image {offset}: a render of what you have built as it "
                              f"stands, {render.get('camera', 'iso')} view."})
        parts.append({"type": "image_url", "image_url": {"url": render["uri"]}})
    if renders:
        parts.append({"type": "text", "text": _REVIEW_RULE})
    return parts


#: What to do with a render. Attached with the pictures rather than kept in the
#: standing rules, because it is only ever actionable when a render is present
#: and a rule that fires with nothing to look at trains a planner to ignore it.
_REVIEW_RULE = (
    "These renders show the body you have actually built. Compare them with the "
    "goal and with any reference image above. If they match, finish. If they do "
    "not, say which specific difference you see — a missing hole, a feature on "
    "the wrong face, a part facing the wrong way — and fix that before "
    "finishing. Do not report success for a body that does not look like what "
    "was asked for.")


def _visual_fingerprint(subject: str, subject_id: str) -> Optional[str]:
    """What a review was a review *of*, so one is not repeated unchanged.

    A part's mesh hash moves when its geometry moves and stays put when a rename
    or a material change does; an assembly's revision counts persisted writes,
    which is what placing and mating a component are. Re-rendering is therefore
    keyed to the body having changed rather than to a counter — the same rule
    that stops a passing review from being spent as a repair attempt.
    """
    thing = _fetch(subject, subject_id)
    if thing is None:
        return None
    if subject == "assembly":
        return f"rev:{getattr(thing, 'revision', None)}"
    derived = getattr(thing, "derived", None)
    return getattr(derived, "mesh_hash", None)


def _verify(subject: str, subject_id: str, note: Callable[[dict], None]) -> List[str]:
    """Check the work before claiming it is done, in that subject's own terms.

    A part is checked for a valid solid. An assembly is checked for parts
    passing through each other — which is the failure mode that looks perfectly
    correct in every other reading the assembly has, and so the one worth
    spending a step on before reporting success.
    """
    op = "check_interference" if subject == "assembly" else "check_geometry"
    env = cad_operations.execute(op, subject_id, {}, include_state=False)
    result = env.get("result") or {}
    if subject == "assembly":
        # **Interferences only, never the clearance warnings.** `clear` is false
        # for a near miss as well as a clash, and mating two parts is what puts
        # their faces in contact — a coincident mate holds them at a 0 mm gap by
        # definition. Gating on `clear` therefore failed every correctly
        # assembled stack, and said "0 pair(s) of parts intersect" while doing
        # it. A near miss is worth reporting and is not a reason to refuse.
        clashes = int(result.get("interferences") or 0)
        near = int(result.get("clearance_warnings") or 0)
        if not env["ok"]:
            failures = [f"Interference check failed: {env.get('error')}"]
        elif clashes:
            # Named, with the depth. "1 pair(s) intersect" is a fact the planner
            # cannot act on: it does not say which two parts, or whether it is
            # out by half a millimetre or by the whole thickness of a plate.
            worst = sorted((p for p in (result.get("pairs") or [])
                            if p.get("status") == "interference"),
                           key=lambda p: -(p.get("penetration_mm") or 0))[:3]
            detail = "; ".join(
                f"{p.get('a')} into {p.get('b')} by {p.get('penetration_mm', 0):.1f} mm"
                for p in worst)
            failures = [f"{clashes} pair(s) of parts occupy the same space"
                        + (f": {detail}" if detail else "")
                        + ". Adjust the mate that positions them — its faces, "
                          "axis or offset_mm — rather than deleting it."]
        else:
            failures = []
        passed = ("No parts intersect" if not near else
                  f"No parts intersect ({near} pair(s) touching or within the "
                  f"clearance threshold, which a mate normally causes)")
    else:
        failures = [] if env["ok"] and result.get("valid") else [
            "Geometry validation failed: " + str(result.get("failed") or env.get("error"))]
        passed = "Geometry check passed"
    note({"kind": "verification", "op": op, "ok": not failures,
          "result": {"summary": passed if not failures else failures[0]}})
    return failures


def _run_scope(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        from app.services import trajectories
        parent = trajectories.current()
        inherited = llm_client.CALL_CONTEXT.get() or {}
        seconds = kwargs.get("max_seconds") or seconds_default(kwargs.get("subject", "document"))
        token = llm_client.CALL_CONTEXT.set({
            "deadline": min(time.monotonic() + seconds, inherited.get("deadline", float("inf"))),
            "cancelled": kwargs.get("cancelled") or inherited.get("cancelled"),
            "model": kwargs.get("model") or inherited.get("model"),
        })
        try:
            return fn(*args, **kwargs)
        finally:
            if trajectories.current() != parent and trajectories.current():
                trajectories.finish(outcome="interrupted", summary="Run interrupted; inspect current state before continuing.")
            llm_client.CALL_CONTEXT.reset(token)
    return wrapped


@_run_scope
def run(
    subject_id: str,
    goal: str,
    max_steps: int = MAX_STEPS_DEFAULT,
    planner: Optional[Planner] = None,
    meter: Optional[Callable[[], None]] = None,
    on_step: Optional[Callable[[dict], None]] = None,
    cancelled: Optional[Callable[[], bool]] = None,
    max_seconds: Optional[float] = None,
    subject: str = "document",
    model: Optional[str] = None,
    images: Optional[List[str]] = None,
    history: str = "",
    context: Optional[dict] = None,
    parent_brief: Optional[dict] = None,
    on_plan: Optional[Callable[[dict], None]] = None,
    on_action: Optional[Callable[[dict], None]] = None,
    review: bool = True,
    run_metadata: Optional[dict] = None,
) -> dict:
    """Run the agentic loop until the planner says done or the step budget runs out.

    ``subject`` is ``"document"`` (a part) or ``"assembly"``, and it decides
    three things: what ``subject_id`` names, which half of the operation catalog
    the planner is offered, and what checking your work means at the end.

    ``planner(goal, transcript, doc_summary) -> decision`` is injectable for tests.
    ``history`` is the selected conversation's compact text transcript. Every
    planner call receives it alongside fresh state; it does not consume this
    run's step budget or become part of its operation/trajectory transcript.
    ``meter()`` is called before each *mutating* op (raise to stop on quota).
    ``images`` are ``data:image/...;base64,`` URIs shown to the planner on every
    turn, in order. Nested part runs may inherit selected reference images;
    their parent brief explicitly identifies them as assembly references.
    ``review`` renders the finished body and shows it to the planner for one
    more turn before the run reports success — the check every deterministic
    verifier is blind to, since a valid, well-formed, correctly-measured body
    can still be the wrong shape. Off for nested part runs, where the assembly's
    own review covers the placed result.
    ``on_step(entry)`` fires as each transcript entry lands, so a caller running
    the loop as a background job can expose live progress.
    Returns ``{steps, document, summary}``.
    """
    from app.services import trajectories

    custom_planner = planner is not None
    # **Mutable, and read at call time.** The default planner is built once, so
    # a render appended to a list captured by value would never be seen; the
    # closure reads this list on every turn instead.
    evidence: List[dict] = []
    planner = planner or (
        lambda g, t, summary: _plan_next(g, t, summary, subject=subject, model=model,
                                         images=images, history=history, enabled_tools=enabled_tools,
                                         renders=evidence))
    max_steps = max(1, min(int(max_steps), step_cap(subject)))
    transcript: List[dict] = []
    final_summary: Optional[str] = None
    if max_seconds is None:
        max_seconds = seconds_default(subject)
    deadline = time.monotonic() + max(1, min(max_seconds, 1800))
    stop_reason = "budget"
    failure_counts: Dict[str, int] = {}
    verification_attempts = 0
    reviewed_fingerprint: Optional[str] = None
    review_skipped = False
    # The review turn is *not* charged to the build. A document run defaults to
    # eight steps and live runs already stop on `budget`; taking one of those to
    # look at the result would buy the check by shortening the work.
    review_allowance = 1 if (review and not custom_planner) else 0
    review_turns_taken = 0
    mutated = False
    result_kind = "paused"
    error_detail = None
    reads = {}
    families = {}
    protocol_failures = 0
    geometry_failures: List[str] = []

    # A copilot run is the archetypal training example: a natural-language goal
    # and the operation sequence that satisfied it. It was previously discarded
    # when the process ended. `execute` records each step into this trajectory.
    doc0 = _fetch(subject, subject_id)
    # What the body looked like before this run touched anything, so "did the
    # geometry change" is a measurement rather than an inference from which
    # operations ran.
    start_fingerprint = _visual_fingerprint(subject, subject_id)
    request_context = copilot_context.RequestContext.model_validate(context or {})
    initial_summary = _summary(subject, doc0) if doc0 else {}
    selection = (copilot_context.resolve_selection(subject, doc0, request_context.selection, initial_summary)
                 if doc0 else {})
    task_state = copilot_context.initial_plan(goal, request_context.task_state, initial_summary)
    managed_plan = request_context.task_state is not None
    enabled_tools = copilot_context.initial_tools(subject, goal + " " + task_state["objective"],
                                                  {**initial_summary, "selection": selection})

    def publish_plan():
        if on_plan:
            try:
                on_plan(deepcopy(task_state))
            except Exception:  # progress reporting must never kill the run
                pass

    publish_plan()
    trajectory_id = trajectories.start(
        subject_id, goal=goal, kind="copilot", actor="copilot",
        owner_id=getattr(doc0, "owner_id", None),
        metadata={"model": model or llm_client.LLM_MODEL, "subject": subject,
                  "context_version": 2, **(run_metadata or {})})

    def note(entry: dict) -> None:
        if entry.get("kind") != "op":
            trajectories.event("planner", **entry)
        transcript.append(entry)
        if on_step is not None:
            try:
                on_step(entry)
            except Exception:  # progress reporting must never kill the loop
                pass

    finished = False
    for turn_index in range(max_steps + review_allowance):
        # **The spare slot is only usable once a review has been issued.** The
        # extended range must not quietly hand an ordinary run an extra action
        # turn: a run asking for two steps has to take two, whatever the review
        # policy is.
        if turn_index >= max_steps + review_turns_taken:
            stop_reason = "budget"
            break
        if cancelled and cancelled():
            stop_reason = "cancelled"
            break
        if time.monotonic() >= deadline:
            stop_reason = "timeout"
            break
        doc = _fetch(subject, subject_id)
        if doc is None:
            stop_reason = f"missing_{subject}"
            break
        try:
            observed_revision = getattr(doc, "revision", None)
            current_summary = _summary(subject, doc)
            current_summary["request_context"] = {
                "selection": selection,
                "viewport": request_context.viewport.model_dump() if request_context.viewport else None,
                "task_state": deepcopy(task_state),
                "budget": {"planner_turns_total": max_steps,
                           "planner_turns_remaining": max(0, max_steps - turn_index),
                           "seconds_remaining": max(0, round(deadline - time.monotonic(), 1)),
                           "reserve_verification_turns": 2},
                "recent_outcomes": [{"op": s.get("op"), "ok": s.get("ok"), "error": s.get("error")}
                                    for s in transcript if s.get("kind") == "op"][-8:],
                "verification_feedback": [s.get("result") for s in transcript
                                      if s.get("kind") == "verification" and not s.get("ok")][-3:],
            }
            if parent_brief:
                current_summary["parent_assembly_brief"] = parent_brief
            decision = planner(goal, transcript, current_summary)
        except Exception as exc:  # LLM / parse failure — surface and stop
            _LOGGER.warning("copilot planner failed: %s", exc)
            note({"kind": "error", "error": f"planner error: {exc}"})
            error_detail = exc.to_dict() if isinstance(exc, LLMFailure) else {
                "code": "PLANNER_ERROR", "message": "The planner returned an invalid decision.", "remedy": "Continue with a smaller next step."}
            if isinstance(exc, LLMFailure) and exc.retryable and protocol_failures < 1:
                protocol_failures += 1
                continue
            stop_reason = "planner_error"
            result_kind = "failed"
            break

        # Cancellation is cooperative: a running kernel call finishes, but a
        # response from a slow model must not authorize another mutation.
        if cancelled and cancelled():
            stop_reason = "cancelled"
            break
        if time.monotonic() >= deadline:
            stop_reason = "timeout"
            break
        if not isinstance(decision, dict):
            stop_reason = "planner_error"
            note({"kind": "error", "error": "Planner decision must be an object"})
            break
        if decision.get("_unconfigured"):
            stop_reason = "unconfigured"
            break
        if decision.get("done"):
            result_kind = decision.get("result_kind") or ("completed" if mutated else "answered")
            if result_kind in ("paused", "needs_input"):
                final_summary = decision.get("summary")
                stop_reason = result_kind
                break
            final_summary = (decision or {}).get("summary") if isinstance(decision, dict) else None
            # **"Something mutated" is not "the body changed".** `set_spec` is a
            # mutate too, so a run that only recorded a brief would otherwise be
            # checked for geometry it never built, rendered from a body that is
            # not there, and told it finished without a spec it had just
            # written. The fingerprint answers the question that was being
            # inferred from the operation kind.
            geometry_changed = bool(
                mutated and _visual_fingerprint(subject, subject_id) != start_fingerprint)
            checking = geometry_changed or result_kind == "completed"
            # The spec is read on *every* claim of done, changed body or not: it
            # describes the part, not this run's edits, so a continuation that
            # declares a half-finished part complete is caught by the same gate
            # as the run that built it. Only the three checks below are about
            # the body having moved.
            unmet = (_unmet_requirements(subject_id) if subject == "document"
                     else _unmated_components(subject_id))
            if checking:
                geometry_failures = _verify(subject, subject_id, note)
                unmet += geometry_failures
            if unmet:
                # Allow one repair attempt, then require a fresh passing check.
                verification_attempts += 1
                note({"kind": "verification", "op": "completion_check", "args": {}, "ok": False,
                      "rationale": "self-check before finishing",
                      "result": {"unmet": unmet, "summary": "; ".join(unmet)}})
                # One repair attempt is right for an edit to one body. A build
                # that is assembling six parts legitimately finds work left on
                # several passes — mate the part it just made, then the next —
                # and cutting it off after one makes a working loop look like a
                # loop that gave up.
                if verification_attempts >= (5 if subject == "assembly" else 2):
                    stop_reason = "verification_failed"
                    break
                continue
            # **Look at it before saying it is done.** Every check above says the
            # body is valid, well formed and measures correctly; none of them
            # asks whether it is the shape that was requested. A live run
            # reported "a 120 x 80 x 6 plate with four holes" over a body
            # measuring 50 x 30 x 6 and passed every one of them.
            if review_allowance and checking:
                fingerprint = _visual_fingerprint(subject, subject_id)
                if fingerprint and fingerprint != reviewed_fingerprint:
                    # A self-render sent to a text-only model is a provider 400
                    # in the middle of a run. The route only checks vision
                    # support for images the *user* attached.
                    if llm_client.accepts_images(model or llm_client.LLM_MODEL) is False:
                        if not review_skipped:
                            review_skipped = True
                            note({"kind": "verification", "op": "visual_review", "args": {}, "ok": True,
                                  "result": {"summary": "visual review skipped: the chosen "
                                                        "model does not accept images"}})
                    else:
                        from app.services import snapshot

                        views = snapshot.packet(subject, subject_id)
                        if views:
                            # Not a verification_attempt: that counter is the
                            # repair cap, and a review that has not failed is
                            # not a failed repair.
                            reviewed_fingerprint = fingerprint
                            review_turns_taken += 1
                            evidence[:] = [{"camera": v["camera"], "uri": snapshot.data_uri(v["png"])}
                                           for v in views]
                            note({"kind": "verification", "op": "visual_review", "args": {}, "ok": True,
                                  "rationale": "look at the result before reporting success",
                                  "result": {"summary": "Rendered " + ", ".join(
                                      v["camera"] for v in views) + " — comparing with the goal",
                                      "views": snapshot.store_views(subject, subject_id, views)}})
                            continue
            # **A build with no brief says so.** `_unmet_requirements` is empty
            # when nothing was ever declared, so a run that never wrote a spec
            # passes the spec gate by having nothing to fail — "done" then means
            # only that the body is a valid solid. This is a note rather than a
            # refusal: plenty of real requests (a fillet, a rename) have nothing
            # measurable in them, and failing those would be a loop.
            if subject == "document" and result_kind == "completed" and geometry_changed:
                built = _fetch(subject, subject_id)
                if not ((getattr(built, "spec", None) or {}).get("requirements")):
                    note({"kind": "verification", "op": "spec_check", "args": {}, "ok": True,
                          "result": {"summary": "finished without a design brief: the body "
                                                "was not scored against the request"}})
            finished = True
            # A status/report request can finish while the saved design plan
            # still has work. Only the planner may clear an explicit plan.
            if not managed_plan:
                task_state["remaining"] = []
            publish_plan()
            stop_reason = "done"
            break

        op = decision.get("op")
        args = decision.get("args") or {}
        rationale = decision.get("rationale")

        if op in ("update_work_plan", "discover_operations"):
            try:
                if op == "update_work_plan":
                    if not isinstance(args, dict):
                        raise ValueError("work plan fields must be an object")
                    task_state = copilot_context.WorkPlan.model_validate({**task_state, **args}).model_dump()
                    managed_plan = True
                    publish_plan()
                    result = {"summary": "Updated work plan", "task_state": deepcopy(task_state)}
                else:
                    found = copilot_context.discover(subject, args)
                    enabled_tools.update(found)
                    result = {"summary": "Loaded operations: " + ", ".join(found), "operations": found}
                note({"kind": "plan" if op == "update_work_plan" else "discovery",
                      "op": op, "ok": True, "result": result})
            except (ValueError, TypeError) as exc:
                note({"kind": "error", "op": op, "error": str(exc)})
            continue

        signature = json.dumps([op, args], sort_keys=True, default=str)
        # Successful queries do not reset failure families or prove progress.
        if families.get("catalog", 0) >= 2 and op == "insert_standard_part":
            note({"kind": "error", "error": "Search the catalog before trying another key.", "op": op})
            stop_reason = "no_progress"
            break
        if reads.get(signature, 0) >= 3:
            note({"kind": "error", "error": "Repeated reads without a geometry change; saved the plan for continuation."})
            stop_reason = "no_progress"
            break
        if failure_counts.get(signature, 0) >= 3:
            stop_reason = "repeated_failure"
            note({"kind": "error", "error": "Stopped after three identical failed actions; revise the operation or arguments."})
            break

        if not isinstance(op, str) or op not in cad_operations.OPERATIONS:
            note({"kind": "op", "op": op, "args": args, "ok": False,
                               "error": f"unknown operation '{op}'", "rationale": rationale})
            failure_counts[signature] = failure_counts.get(signature, 0) + 1
            continue

        # An operation for the *other* kind of id would be dispatched against
        # this one, and `execute` would refuse it as a missing document — a
        # confusing answer to a real mistake. Say which it is instead.
        if cad_operations.OPERATIONS[op].subject != subject:
            note({"kind": "op", "op": op, "args": args, "ok": False,
                  "error": f"'{op}' operates on a "
                           f"{cad_operations.OPERATIONS[op].subject}, not on the "
                           f"{subject} this run is editing",
                  "rationale": rationale})
            failure_counts[signature] = failure_counts.get(signature, 0) + 1
            continue

        if not custom_planner and op not in enabled_tools:
            note({"kind": "discovery", "op": op, "ok": False,
                  "result": {"summary": "Load this operation with discover_operations before executing it."}})
            continue

        if cad_operations.OPERATIONS[op].kind == "mutate" and meter is not None:
            try:
                meter()
            except Exception as exc:  # quota / permission — stop cleanly
                note({"kind": "error", "error": f"stopped: {exc}"})
                stop_reason = "permission_denied"
                break

        # Scoped around the dispatch only: `build_part` runs a whole part
        # copilot inside this one step, and that sub-run charges the same meter.
        meter_token = _ACTIVE_METER.set(meter)
        model_token = _ACTIVE_MODEL.set(model)
        context_token = _ACTIVE_CONTEXT.set({
            "goal": goal, "history": history, "task_state": deepcopy(task_state),
            "selection": selection, "summary": current_summary,
            "images": images or [], "deadline": deadline, "cancelled": cancelled,
            "on_action": on_action, "run_metadata": run_metadata,
        })
        try:
            if on_action:
                on_action({"phase": "intent", "op": op, "args": args, "subject_id": subject_id,
                           "revision": getattr(doc, "revision", None)})
            env = cad_operations.execute(op, subject_id, args, rationale=rationale or "",
                                         expected_revision=observed_revision)
            if on_action:
                on_action({"phase": "result", "op": op, "ok": env["ok"],
                           "revision": env.get("revision"), "result": _trim_result(env.get("result") or {})})
        finally:
            _ACTIVE_METER.reset(meter_token)
            _ACTIVE_MODEL.reset(model_token)
            _ACTIVE_CONTEXT.reset(context_token)
        entry = {"kind": "op", "op": op, "args": args, "ok": env["ok"],
                 "rationale": rationale, "result": _trim_result(env.get("result") or {})}
        if env.get("error"):
            # The planner is told to react to failures, so give it the typed code
            # and message rather than a stringified exception.
            entry["error"] = f"{env['error']['code']}: {env['error']['message']}"
            entry["error_detail"] = _trim_result(env["error"])
        if env["ok"]:
            mutated = mutated or cad_operations.OPERATIONS[op].kind == "mutate"
            if cad_operations.OPERATIONS[op].kind == "query":
                reads[signature] = reads.get(signature, 0) + 1
            if op == "search_assembly_parts":
                families.pop("catalog", None)
            if cad_operations.OPERATIONS[op].kind == "mutate":
                reads.clear()
                failure_counts.clear()
                # **A render stops being true the moment the body changes.** Left
                # attached, the next turn shows the planner the *old* picture
                # still captioned "what you have built as it stands" — the exact
                # confusion the labels exist to prevent, and an invitation to cut
                # the same hole twice. Dropping it here also re-arms the review:
                # the fingerprint has moved, so the next `done` renders again.
                evidence.clear()
                task_state["completed"] = (task_state["completed"] + [
                    f"{op}: {rationale or (env.get('result') or {}).get('message') or 'operation succeeded'}"[:1000]
                ])[-30:]
                publish_plan()
        else:
            failure_counts[signature] = failure_counts.get(signature, 0) + 1
            detail = env.get("error") or {}
            if "no catalog part" in detail.get("message", ""):
                families["catalog"] = families.get("catalog", 0) + 1
            if detail.get("code", "").startswith("PROVIDER_") or "changed since you read" in detail.get("message", "") or detail.get("code") in ("REVISION_CONFLICT", "STALE_REVISION"):
                error_detail = detail
                stop_reason = "provider_error" if detail["code"].startswith("PROVIDER_") else "conflict"
                note(entry)
                break
        if env.get("warnings"):
            entry["warnings"] = env["warnings"]
        if env.get("delta"):
            entry["delta"] = env["delta"]
        note(entry)

    doc = _fetch(subject, subject_id)
    if final_summary is None:
        n_ok = sum(1 for s in transcript if s.get("kind") == "op" and s.get("ok"))
        final_summary = f"Ran {n_ok} operation(s) toward: {goal}"
    # Say so rather than implying the goal was met. A run that stops with
    # requirements outstanding is a different outcome from one that finished,
    # and a summary that reads the same for both is the silent success this
    # whole surface is built to avoid.
    outstanding = ((_unmet_requirements(subject_id) if subject == "document"
                    else _unmated_components(subject_id)) + geometry_failures)
    if outstanding:
        finished = False
        task_state["remaining"] = list(dict.fromkeys(task_state["remaining"] + [s[:1000] for s in outstanding]))[:30]
        publish_plan()
        if stop_reason == "done":
            stop_reason = "verification_failed"
    if outstanding:
        final_summary = (f"{final_summary} — {len(outstanding)} requirement(s) "
                         f"still unmet: {'; '.join(outstanding[:3])}")
    if not finished:
        final_summary = f"Stopped ({stop_reason}). Goal not confirmed complete. {final_summary}"
    if error_detail:
        final_summary += " " + error_detail["message"] + " " + error_detail.get("remedy", "")
    if not finished and result_kind not in ("needs_input", "failed"):
        result_kind = "paused"
    trajectories.finish(trajectory_id, outcome=stop_reason,
                        summary=final_summary)
    return {"steps": transcript,
            # Under its own name rather than one polymorphic key: a caller that
            # has to infer which kind it was handed from the shape of the value
            # will eventually infer wrong.
            "document": doc if subject == "document" else None,
            "assembly": doc if subject == "assembly" else None,
            "summary": final_summary,
            # Whether the planner *said* it was done, rather than inferred from
            # the step count — a run that used its last step to finish and one
            # that was cut off there are different outcomes.
            "finished": finished, "unmet": outstanding,
            "stop_reason": stop_reason,
            # Which model produced this run, so two runs of one goal can be told
            # apart afterwards — the whole point of being able to pick one.
            "model": model or llm_client.LLM_MODEL,
            "has_image": bool(images),
            "image_count": len(images or []),
            "task_state": task_state,
            "trajectory_id": trajectory_id, "result_kind": result_kind, "error_detail": error_detail}


def _unmated_components(asm_id: str) -> List[str]:
    """Components no mate holds — which is what stops a build calling itself done.

    A part that was inserted and never constrained sits wherever it was dropped,
    usually at the origin and usually inside something else. In a finished
    assembly every component is named by at least one mate, including the one
    held fixed: it is a mate's `base`. So the rule needs no "except the first"
    exception, and a single-component assembly has nothing to constrain against.
    """
    from app.services import assembly_service

    asm = assembly_service.get_assembly(asm_id)
    if asm is None or len(asm.components) < 2:
        return []
    held = {m.base for m in asm.mates} | {m.moved for m in asm.mates}
    loose = [c for c in asm.components if c.id not in held]
    if not loose:
        return []
    names = ", ".join((c.name or c.id) for c in loose[:6])
    return [f"{len(loose)} component(s) are not held by any mate: {names}. "
            f"Add a mate for each, or say why it is meant to float."]


def _unmet_requirements(doc_id: str) -> List[str]:
    """Which of the document's own spec requirements the body currently fails.

    Empty when there is no spec, which is the common case and must not turn into
    a phantom failure — a document with nothing declared has nothing to fail.
    """
    doc = document_service.get_document(doc_id)
    if doc is None or not ((getattr(doc, "spec", None) or {}).get("requirements")):
        return []
    env = cad_operations.execute("verify_against_spec", doc_id, {}, include_state=False)
    if not env["ok"]:
        # A spec that *cannot* be scored is not a spec that passed. The usual
        # cause is the one that matters: the run declared itself done without
        # building a body, and a new document has none.
        return [f"the spec could not be checked: {(env.get('error') or {}).get('message')}"]
    return [f"{row.get('label') or row.get('id')}: {row.get('detail')}"
            for row in (env["result"] or {}).get("checks") or []
            if row.get("status") != "pass"]


_HEAVY_KEYS = {"document", "mesh_url", "feature_tree", "profile", "sketch"}


def _trim_result(result: Any, depth: int = 0) -> Any:
    """Keep the transcript light: drop heavy payloads (embedded doc/mesh/tree)."""
    if depth > 5:
        return "[nested output omitted; query the document for details]"
    if isinstance(result, dict):
        trimmed = {k: _trim_result(v, depth + 1) for k, v in list(result.items())[:40]
                   if k not in _HEAVY_KEYS}
    elif isinstance(result, list):
        trimmed = [_trim_result(v, depth + 1) for v in result[:40]]
        if len(result) > 40:
            trimmed.append({"omitted_items": len(result) - 40})
    elif isinstance(result, str):
        trimmed = result[:2000]
    else:
        trimmed = result
    if depth == 0 and len(json.dumps(trimmed, default=str)) > 12000:
        return {"truncated": True, "preview": json.dumps(trimmed, default=str)[:10000],
                "note": "Full operation details are in the trajectory record."}
    return trimmed

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
from contextvars import ContextVar
from typing import Any, Callable, Dict, List, Optional

from app.services import cad_operations, document_service, llm_client

_LOGGER = logging.getLogger(__name__)

MAX_STEPS_DEFAULT = 8
MAX_STEPS_CAP = 16

#: An assembly run is a different size of job. One part is a whole nested
#: copilot run, so "design four parts, place them, mate them and check" is
#: already a dozen steps before anything goes wrong — budgets tuned for editing
#: one body stop it halfway through and report it as out of budget, which looks
#: exactly like a loop that gave up.
ASSEMBLY_MAX_STEPS_DEFAULT = 24
ASSEMBLY_MAX_STEPS_CAP = 60


def step_cap(subject: str) -> int:
    return ASSEMBLY_MAX_STEPS_CAP if subject == "assembly" else MAX_STEPS_CAP


def step_default(subject: str) -> int:
    return ASSEMBLY_MAX_STEPS_DEFAULT if subject == "assembly" else MAX_STEPS_DEFAULT

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
               subject: str = "document", model: Optional[str] = None) -> dict:
    """Ask the LLM for the next operation. Returns a decision dict.

    Two paths, and which one ran is recorded on the decision as ``_via`` the
    same way `AnalysisReport.solver` marks a closed-form fallback: a prompt is
    not a measurement, and a benchmark comparing runs has to be able to tell a
    schema-constrained call from a parsed one.
    """
    from app.services import action_space

    if not llm_client.is_configured():
        return {"done": True, "summary": "No LLM configured — the copilot needs an LLM key.", "_unconfigured": True}

    history = [
        {
            "op": s.get("op"),
            "args": s.get("args"),
            "ok": s.get("ok"),
            "error": s.get("error"),
            "warnings": s.get("warnings"),
            "result": s.get("result"),
        }
        for s in transcript
        if s.get("kind") in ("op", "verification")
    ]
    user = (
        f"Goal: {goal}\n\n"
        f"Current document:\n{json.dumps(summary)}\n\n"
        f"Steps so far:\n{json.dumps(history) if history else '(none yet)'}\n\n"
        "Take the next single action."
    )
    with_tools = llm_client.supports_tools()
    messages = [
        {"role": "system", "content": action_space.system_prompt(
            with_tools=with_tools, subject=subject)},
        {"role": "user", "content": user},
    ]
    message = llm_client._post_chat(
        messages, timeout=60.0, max_tokens=1200,
        tools=action_space.tools(subject) if with_tools else None,
        model=model)

    calls = message.get("tool_calls") or []
    if calls:
        function = (calls[0] or {}).get("function") or {}
        raw_args = function.get("arguments") or "{}"
        try:
            parsed = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
        except json.JSONDecodeError as exc:
            raise ValueError(f"tool call arguments were not JSON: {raw_args[:200]}") from exc
        return {"op": function.get("name"), "args": parsed,
                "rationale": (message.get("content") or "").strip()[:200], "_via": "tools"}

    content = message.get("content") or ""
    if with_tools:
        # Tools were offered and none was called: on this protocol that is how a
        # model says it is finished, and the text is the summary.
        return {"done": True, "summary": content.strip()[:500] or "done", "_via": "tools"}

    raw = llm_client._extract_json(content)
    if raw is None:
        raise ValueError(f"planner returned no JSON: {content[:200]}")
    return {**json.loads(raw), "_via": "prose"}


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


def run(
    subject_id: str,
    goal: str,
    max_steps: int = MAX_STEPS_DEFAULT,
    planner: Optional[Planner] = None,
    meter: Optional[Callable[[], None]] = None,
    on_step: Optional[Callable[[dict], None]] = None,
    cancelled: Optional[Callable[[], bool]] = None,
    max_seconds: float = 600,
    subject: str = "document",
    model: Optional[str] = None,
) -> dict:
    """Run the agentic loop until the planner says done or the step budget runs out.

    ``subject`` is ``"document"`` (a part) or ``"assembly"``, and it decides
    three things: what ``subject_id`` names, which half of the operation catalog
    the planner is offered, and what checking your work means at the end.

    ``planner(goal, transcript, doc_summary) -> decision`` is injectable for tests.
    ``meter()`` is called before each *mutating* op (raise to stop on quota).
    ``on_step(entry)`` fires as each transcript entry lands, so a caller running
    the loop as a background job can expose live progress.
    Returns ``{steps, document, summary}``.
    """
    from app.services import trajectories

    planner = planner or (
        lambda g, t, summary: _plan_next(g, t, summary, subject=subject, model=model))
    max_steps = max(1, min(int(max_steps), step_cap(subject)))
    transcript: List[dict] = []
    final_summary: Optional[str] = None
    deadline = time.monotonic() + max(1, min(max_seconds, 1800))
    stop_reason = "budget"
    failure_counts: Dict[str, int] = {}
    verification_attempts = 0
    mutated = False
    geometry_failures: List[str] = []

    # A copilot run is the archetypal training example: a natural-language goal
    # and the operation sequence that satisfied it. It was previously discarded
    # when the process ended. `execute` records each step into this trajectory.
    doc0 = _fetch(subject, subject_id)
    trajectory_id = trajectories.start(
        subject_id, goal=goal, kind="copilot", actor="copilot",
        owner_id=getattr(doc0, "owner_id", None))

    def note(entry: dict) -> None:
        transcript.append(entry)
        if on_step is not None:
            try:
                on_step(entry)
            except Exception:  # progress reporting must never kill the loop
                pass

    finished = False
    for _ in range(max_steps):
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
            decision = planner(goal, transcript, _summary(subject, doc))
        except Exception as exc:  # LLM / parse failure — surface and stop
            _LOGGER.warning("copilot planner failed: %s", exc)
            note({"kind": "error", "error": f"planner error: {exc}"})
            stop_reason = "planner_error"
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
            final_summary = (decision or {}).get("summary") if isinstance(decision, dict) else None
            unmet = (_unmet_requirements(subject_id) if subject == "document"
                     else _unmated_components(subject_id))
            if mutated:
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
            finished = True
            stop_reason = "done"
            break

        op = decision.get("op")
        args = decision.get("args") or {}
        rationale = decision.get("rationale")

        signature = json.dumps([op, args], sort_keys=True, default=str)
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
        try:
            env = cad_operations.execute(op, subject_id, args, rationale=rationale or "")
        finally:
            _ACTIVE_METER.reset(meter_token)
            _ACTIVE_MODEL.reset(model_token)
        entry = {"kind": "op", "op": op, "args": args, "ok": env["ok"],
                 "rationale": rationale, "result": _trim_result(env.get("result") or {})}
        if env.get("error"):
            # The planner is told to react to failures, so give it the typed code
            # and message rather than a stringified exception.
            entry["error"] = f"{env['error']['code']}: {env['error']['message']}"
            entry["error_detail"] = _trim_result(env["error"])
        if env["ok"]:
            mutated = mutated or cad_operations.OPERATIONS[op].kind == "mutate"
            failure_counts.clear()
        else:
            failure_counts[signature] = failure_counts.get(signature, 0) + 1
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
        if stop_reason == "done":
            stop_reason = "verification_failed"
    if outstanding:
        final_summary = (f"{final_summary} — {len(outstanding)} requirement(s) "
                         f"still unmet: {'; '.join(outstanding[:3])}")
    if not finished:
        final_summary = f"Stopped ({stop_reason}). Goal not confirmed complete. {final_summary}"
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
            "trajectory_id": trajectory_id}


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

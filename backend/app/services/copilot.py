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
from typing import Any, Callable, Dict, List, Optional

from app.services import cad_operations, document_service, llm_client

_LOGGER = logging.getLogger(__name__)

MAX_STEPS_DEFAULT = 8
MAX_STEPS_CAP = 16

# A decision is either {"op", "args", "rationale"} or {"done": true, "summary"}.
Planner = Callable[[str, List[dict], dict], dict]


def _doc_summary(doc) -> dict:
    """Compact, LLM-friendly snapshot of the current body.

    Lives in ``services.observation`` now: the same summary is what every
    operation returns as ``state``, so the planner and the API cannot drift.
    """
    from app.services import observation

    return observation.summarize(doc)


def _plan_next(goal: str, transcript: List[dict], summary: dict) -> dict:
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
        if s.get("kind") == "op"
    ]
    user = (
        f"Goal: {goal}\n\n"
        f"Current document:\n{json.dumps(summary)}\n\n"
        f"Steps so far:\n{json.dumps(history) if history else '(none yet)'}\n\n"
        "Take the next single action."
    )
    with_tools = llm_client.supports_tools()
    messages = [
        {"role": "system", "content": action_space.system_prompt(with_tools=with_tools)},
        {"role": "user", "content": user},
    ]
    message = llm_client._post_chat(
        messages, timeout=60.0, max_tokens=1200,
        tools=action_space.tools() if with_tools else None)

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


def run(
    doc_id: str,
    goal: str,
    max_steps: int = MAX_STEPS_DEFAULT,
    planner: Optional[Planner] = None,
    meter: Optional[Callable[[], None]] = None,
    on_step: Optional[Callable[[dict], None]] = None,
) -> dict:
    """Run the agentic loop until the planner says done or the step budget runs out.

    ``planner(goal, transcript, doc_summary) -> decision`` is injectable for tests.
    ``meter()`` is called before each *mutating* op (raise to stop on quota).
    ``on_step(entry)`` fires as each transcript entry lands, so a caller running
    the loop as a background job can expose live progress.
    Returns ``{steps, document, summary}``.
    """
    from app.services import trajectories

    planner = planner or _plan_next
    max_steps = max(1, min(int(max_steps), MAX_STEPS_CAP))
    transcript: List[dict] = []
    final_summary: Optional[str] = None
    checked = False

    # A copilot run is the archetypal training example: a natural-language goal
    # and the operation sequence that satisfied it. It was previously discarded
    # when the process ended. `execute` records each step into this trajectory.
    doc0 = document_service.get_document(doc_id)
    trajectory_id = trajectories.start(
        doc_id, goal=goal, kind="copilot", actor="copilot",
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
        doc = document_service.get_document(doc_id)
        if doc is None:
            break
        try:
            decision = planner(goal, transcript, _doc_summary(doc))
        except Exception as exc:  # LLM / parse failure — surface and stop
            _LOGGER.warning("copilot planner failed: %s", exc)
            note({"kind": "error", "error": f"planner error: {exc}"})
            break

        if not isinstance(decision, dict) or decision.get("done"):
            final_summary = (decision or {}).get("summary") if isinstance(decision, dict) else None
            unmet = _unmet_requirements(doc_id) if not checked else []
            if unmet:
                # The spec is the reward function, and a loop that never reads it
                # is optimising nothing. Checked once, on the transition to done
                # — re-checking after every edit would spend the whole budget
                # checking, and a second refusal would be a loop rather than a
                # correction.
                checked = True
                # Recorded as the operation it is, not as a private note: the
                # planner's history is built from `kind == "op"` entries, so
                # anything else would be invisible to the model it is meant to
                # inform, and the run would spend a step to learn nothing.
                note({"kind": "op", "op": "verify_against_spec", "args": {}, "ok": True,
                      "rationale": "self-check before finishing",
                      "result": {"unmet": unmet}})
                continue
            finished = True
            break

        op = decision.get("op")
        args = decision.get("args") or {}
        rationale = decision.get("rationale")

        if op not in cad_operations.OPERATIONS:
            note({"kind": "op", "op": op, "args": args, "ok": False,
                               "error": f"unknown operation '{op}'", "rationale": rationale})
            continue

        if cad_operations.OPERATIONS[op].kind == "mutate" and meter is not None:
            try:
                meter()
            except Exception as exc:  # quota / permission — stop cleanly
                note({"kind": "error", "error": f"stopped: {exc}"})
                break

        env = cad_operations.execute(op, doc_id, args, rationale=rationale or "")
        entry = {"kind": "op", "op": op, "args": args, "ok": env["ok"],
                 "rationale": rationale, "result": _trim_result(env.get("result") or {})}
        if env.get("error"):
            # The planner is told to react to failures, so give it the typed code
            # and message rather than a stringified exception.
            entry["error"] = f"{env['error']['code']}: {env['error']['message']}"
        if env.get("warnings"):
            entry["warnings"] = env["warnings"]
        if env.get("delta"):
            entry["delta"] = env["delta"]
        note(entry)

    doc = document_service.get_document(doc_id)
    if final_summary is None:
        n_ok = sum(1 for s in transcript if s.get("kind") == "op" and s.get("ok"))
        final_summary = f"Ran {n_ok} operation(s) toward: {goal}"
    # Say so rather than implying the goal was met. A run that stops with
    # requirements outstanding is a different outcome from one that finished,
    # and a summary that reads the same for both is the silent success this
    # whole surface is built to avoid.
    outstanding = _unmet_requirements(doc_id)
    if outstanding:
        final_summary = (f"{final_summary} — {len(outstanding)} requirement(s) "
                         f"still unmet: {'; '.join(outstanding[:3])}")
    trajectories.finish(trajectory_id, outcome="done" if finished else "budget",
                        summary=final_summary)
    return {"steps": transcript, "document": doc, "summary": final_summary,
            # Whether the planner *said* it was done, rather than inferred from
            # the step count — a run that used its last step to finish and one
            # that was cut off there are different outcomes.
            "finished": finished, "unmet": outstanding,
            "trajectory_id": trajectory_id}


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
        return []
    return [f"{row.get('label') or row.get('id')}: {row.get('detail')}"
            for row in (env["result"] or {}).get("checks") or []
            if row.get("status") != "pass"]


_HEAVY_KEYS = {"document", "mesh_url", "feature_tree", "profile", "sketch"}


def _trim_result(result: Any) -> Any:
    """Keep the transcript light: drop heavy payloads (embedded doc/mesh/tree)."""
    if isinstance(result, dict):
        return {k: v for k, v in result.items() if k not in _HEAVY_KEYS}
    return result

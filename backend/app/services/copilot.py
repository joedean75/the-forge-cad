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


def _catalog_text() -> str:
    lines = []
    for op in cad_operations.catalog():
        params = ", ".join(op["params"].keys()) or "—"
        lines.append(f"- {op['name']} ({op['kind']}): {op['description']} · params: {params}")
    return "\n".join(lines)


_SYSTEM_PROMPT = """You are Forge Copilot, an engineering agent that builds and edits a CAD part by calling operations one at a time.

Each turn respond with EXACTLY ONE JSON object, nothing else:
  {"op": "<operation_name>", "args": { ... }, "rationale": "<one short sentence>"}
or, when the goal is fully satisfied:
  {"done": true, "summary": "<what you accomplished>"}

Rules:
- ALL lengths are in METRES (100 mm = 0.1). Center parts near the origin.
- Use ONLY the operations listed below, with their exact parameter names.
- Take the minimum number of purposeful steps. Prefer add_primitive, add_sketch, add_modifier, and set_feature_params.
- To change an existing feature's size/position, read its CURRENT params in the document summary and pass the SAME structure to set_feature_params (e.g. a box uses {"size":{"x","y","z"},"center":{...}} in metres). Do not invent parameter names.
- Use query operations (bounding_box, list_features) to verify when unsure; they don't change the part.
- If an operation returned an error last turn, fix the arguments or try a different approach — do not repeat the same failing call.
- When the part matches the goal, return {"done": true, ...}. Do not keep adding features unnecessarily.

Available operations:
%s
"""


def _plan_next(goal: str, transcript: List[dict], summary: dict) -> dict:
    """Ask the LLM for the next operation. Returns a decision dict."""
    if not llm_client.is_configured():
        return {"done": True, "summary": "No LLM configured — the copilot needs an LLM key.", "_unconfigured": True}

    history = [
        {
            "op": s.get("op"),
            "args": s.get("args"),
            "ok": s.get("ok"),
            "error": s.get("error"),
            "result": s.get("result"),
        }
        for s in transcript
        if s.get("kind") == "op"
    ]
    user = (
        f"Goal: {goal}\n\n"
        f"Current document:\n{json.dumps(summary)}\n\n"
        f"Steps so far:\n{json.dumps(history) if history else '(none yet)'}\n\n"
        "Return the next single JSON action."
    )
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT % _catalog_text()},
        {"role": "user", "content": user},
    ]
    content = llm_client._call_llm(messages, timeout=60.0, max_tokens=1200)
    raw = llm_client._extract_json(content)
    if raw is None:
        raise ValueError(f"planner returned no JSON: {content[:200]}")
    return json.loads(raw)


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
    trajectories.finish(trajectory_id, outcome="done", summary=final_summary)
    return {"steps": transcript, "document": doc, "summary": final_summary,
            "trajectory_id": trajectory_id}


_HEAVY_KEYS = {"document", "mesh_url", "feature_tree", "profile", "sketch"}


def _trim_result(result: Any) -> Any:
    """Keep the transcript light: drop heavy payloads (embedded doc/mesh/tree)."""
    if isinstance(result, dict):
        return {k: v for k, v in result.items() if k not in _HEAVY_KEYS}
    return result

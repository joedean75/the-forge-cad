"""AI-native operation API surface.

- GET  /api/operations                     -> self-describing tool catalog
- POST /api/documents/{doc_id}/op          -> dispatch one operation

This is the substrate the MCP server wraps so any agent can drive Forge.
"""

import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel

from app.models.schemas import DesignDocument
from app.security import current_account, meter_operation
from app.services import accounts, cad_operations, copilot, document_service
from app.services import op_errors

router = APIRouter(tags=["operations"])

# Error code -> HTTP status. Kept next to the route rather than inside the error
# classes' users so the whole HTTP contract is readable in one place.
_STATUS_BY_CODE = {
    cls.code: cls.http_status
    for cls in (
        op_errors.UnknownOperation, op_errors.MissingArgument, op_errors.BadArgument,
        op_errors.NoSuchDocument, op_errors.NoSuchFeature, op_errors.PreconditionFailed,
        op_errors.EvaluationFailed, op_errors.InternalError,
    )
}


def _http_status(error: Optional[Dict[str, Any]]) -> int:
    return _STATUS_BY_CODE.get((error or {}).get("code"), 422)


def prune_jobs(store: Dict[str, Any], cap: int = 200) -> None:
    """Bound an in-memory job dict: drop oldest entries past the cap."""
    while len(store) > cap:
        store.pop(next(iter(store)), None)


class OperationRequest(BaseModel):
    op: str
    args: Optional[Dict[str, Any]] = None


class OperationError(BaseModel):
    """A failure the caller can branch on without parsing prose."""

    code: str
    message: str
    arg: Optional[str] = None


class OperationResponse(BaseModel):
    ok: bool
    op: str
    result: Dict[str, Any] = {}
    error: Optional[OperationError] = None
    # What the operation changed: volume/mass/bbox before and after, plus the ids
    # of features added and removed. Saves a `list_features` round-trip to learn
    # the id of the thing you just created.
    delta: Optional[Dict[str, Any]] = None
    # The compact document observation — features with their current params, size
    # and mass. Small enough to put in a prompt, unlike the full document.
    state: Optional[Dict[str, Any]] = None
    # Non-fatal conditions, chiefly EVAL_FAILED: the edit applied but the body
    # does not currently evaluate (e.g. it references a parameter not yet
    # defined). Visible without being reported as a failed call.
    warnings: List[Dict[str, Any]] = []
    document: Optional[DesignDocument] = None


@router.get("/api/operations")
def list_operations():
    """Self-describing catalog of CAD operations for agents / MCP."""
    return {"operations": cad_operations.catalog()}


@router.get("/api/platform")
def platform_manifest():
    """Discovery manifest for the agent platform: how to authenticate, what to call.

    Public by design — it describes the surface, never data. Agents (or humans)
    hit this first, then /api/operations for the op catalog and /docs for OpenAPI.
    """
    return {
        "name": "The Forge — agent-native CAD platform",
        "version": "1",
        "auth": {
            "api_key": "Send 'X-Api-Key: <key>'. Create keys under Account → API keys (Pro plan).",
            "session": "Send 'Authorization: Bearer <jwt>' from /api/auth/login.",
        },
        "endpoints": {
            "operation_catalog": "GET /api/operations",
            "run_operation": "POST /api/documents/{id}/op {op, args}",
            "copilot": "POST /api/documents/{id}/copilot {goal, max_steps} — starts a background job; poll GET /api/documents/{id}/copilot/{job_id} until status != 'running'",
            "create_document": "POST /api/documents {prompt?, name?} — text-to-CAD or blank",
            "create_assembly": "POST /api/assemblies/from-prompt {prompt} — text-to-assembly background job; poll GET /api/assemblies/from-prompt/{job_id}",
            "import": "POST /api/documents/import (multipart: file, units, resolution)",
            "quotes": "POST /api/documents/{id}/quotes {quantity}",
            "orders": "POST /api/documents/{id}/orders {process, quantity, notes}",
            "export_step": "GET /api/documents/{id}/step",
            "slice": "POST /api/documents/{id}/slice {layer_height?, infill?, …} — FDM G-code; download GET /api/documents/{id}/gcode",
            "print": "run_operation print_part {printer_url, start} — send G-code to a Moonraker/Klipper printer",
            "openapi": "GET /openapi.json (interactive: /docs)",
        },
        "mcp": {
            "server": "backend/mcp_server.py (stdio)",
            "env": {"FORGE_API": "backend base URL + /api", "FORGE_API_KEY": "an account API key"},
            "tools": [
                "list_operations", "create_part", "list_parts", "get_part", "run_operation",
                "copilot", "import_part", "quote_part", "order_part", "export_part_step",
            ],
        },
        "conventions": {"units": "metres in op args; UI displays mm", "mesh": "derived.mesh_url is a binary-STL data URI"},
    }


@router.post(
    "/api/documents/{doc_id}/op",
    response_model=OperationResponse,
    # The full version history (up to 100 snapshot trees) has no business riding
    # along on every edit; the history drawer fetches /versions explicitly.
    response_model_exclude={"document": {"version_history"}},
)
def run_operation(
    doc_id: str,
    body: OperationRequest,
    acc: accounts.Account = Depends(current_account),
    envelope: bool = Query(
        False,
        description="Return 200 with ok=false instead of raising on operation failure. "
                    "For agent/rollout harnesses that treat a failure as an observation.",
    ),
):
    """Run one catalog operation against a document.

    Failures come back as a typed ``{code, message, arg}`` — `MISSING_ARG` for a
    malformed call, `NO_SUCH_FEATURE` for a bad id, `PRECONDITION` when the
    document is in the wrong shape for the operation. Previously all of these
    arrived as one of two prose strings, and a missing argument was reported as
    ``404 Not found``.

    A successful response also carries ``delta`` (what the operation changed) and
    ``state`` (the compact document observation), plus ``warnings`` — notably
    `EVAL_FAILED`, when an edit applied but the body does not currently evaluate.

    By default a failure raises, so browser clients keep throwing on error. Pass
    ``?envelope=1`` to get the same information as a 200 body instead.
    """
    op = cad_operations.OPERATIONS.get(body.op)
    if op is None:
        raise HTTPException(status_code=400, detail={
            "code": "UNKNOWN_OP", "message": f"unknown operation: {body.op}", "arg": "op"})
    # Enforce tenant isolation before doing any work (404, never 403, to avoid
    # leaking the existence of other tenants' documents).
    doc = document_service.get_document(doc_id)
    if doc is None or not document_service.can_access(doc, acc.id):
        raise HTTPException(status_code=404, detail={
            "code": "NO_SUCH_DOCUMENT", "message": "Document not found"})
    # Mutating ops require editor access and are metered against the plan quota;
    # read-only queries only require view access (free).
    if op.kind == "mutate":
        if not document_service.can_edit(doc, acc.id):
            raise HTTPException(status_code=403, detail={
                "code": "FORBIDDEN", "message": "this action requires editor access"})
        meter_operation(acc)

    env = cad_operations.execute(body.op, doc_id, body.args)

    if not env["ok"] and not envelope:
        raise HTTPException(status_code=_http_status(env["error"]), detail=env["error"])

    document = document_service.get_document(doc_id) if op.kind == "mutate" else None
    return OperationResponse(
        ok=env["ok"],
        op=env["op"],
        result=env.get("result") or {},
        error=env.get("error"),
        delta=env.get("delta"),
        state=env.get("state"),
        warnings=env.get("warnings") or [],
        document=document,
    )


class CopilotRequest(BaseModel):
    goal: str
    max_steps: int = copilot.MAX_STEPS_DEFAULT


class CopilotJobState(BaseModel):
    ok: bool
    job_id: str
    status: str  # "running" | "done" | "error"
    steps: list[Dict[str, Any]]
    summary: Optional[str] = None
    document: Optional[DesignDocument] = None


# In-memory copilot jobs (single-instance, like the optimize job store).
_COPILOT_JOBS: Dict[str, Dict[str, Any]] = {}


def _copilot_state(job: Dict[str, Any], document: Optional[DesignDocument]) -> CopilotJobState:
    return CopilotJobState(
        ok=job["status"] != "error",
        job_id=job["id"],
        status=job["status"],
        steps=list(job["steps"]),
        summary=job.get("summary"),
        document=document,
    )


def _run_copilot_job(job: Dict[str, Any], doc_id: str, goal: str, max_steps: int, acc: accounts.Account) -> None:
    try:
        outcome = copilot.run(
            doc_id,
            goal,
            max_steps=max_steps,
            meter=lambda: meter_operation(acc),
            on_step=lambda entry: job["steps"].append(entry),
        )
        job["summary"] = outcome["summary"]
        job["status"] = "done"
    except Exception as exc:  # noqa: BLE001 — job must always resolve
        job["summary"] = f"Copilot failed: {exc}"
        job["status"] = "error"


def _copilot_access(doc_id: str, acc: accounts.Account) -> None:
    doc = document_service.get_document(doc_id)
    if doc is None or not document_service.can_access(doc, acc.id):
        raise HTTPException(status_code=404, detail="Document not found")
    if not document_service.can_edit(doc, acc.id):
        raise HTTPException(status_code=403, detail="this action requires editor access")


@router.post(
    "/api/documents/{doc_id}/copilot",
    response_model=CopilotJobState,
    response_model_exclude={"document": {"version_history"}},
)
def run_copilot(
    doc_id: str,
    body: CopilotRequest,
    background_tasks: BackgroundTasks,
    acc: accounts.Account = Depends(current_account),
):
    """Forge Copilot: starts an agentic loop as a background job; poll the returned
    job id for live steps. (A run can span many LLM turns — minutes — so holding
    one HTTP request open would hit proxy/browser timeouts.)

    Editor access required (it mutates the part). Each mutating sub-step is metered
    against the plan quota via the ``meter`` callback, so the agent can't bypass limits.
    """
    _copilot_access(doc_id, acc)
    if not (body.goal or "").strip():
        raise HTTPException(status_code=422, detail="goal is required")

    job = {"id": str(uuid.uuid4()), "doc_id": doc_id, "status": "running", "steps": [], "summary": None}
    _COPILOT_JOBS[job["id"]] = job
    prune_jobs(_COPILOT_JOBS)
    background_tasks.add_task(_run_copilot_job, job, doc_id, body.goal.strip(), body.max_steps, acc)
    return _copilot_state(job, document=None)


@router.get(
    "/api/documents/{doc_id}/copilot/{job_id}",
    response_model=CopilotJobState,
    response_model_exclude={"document": {"version_history"}},
)
def copilot_job(doc_id: str, job_id: str, acc: accounts.Account = Depends(current_account)):
    """Poll a copilot job: live transcript while running, summary + document when done."""
    _copilot_access(doc_id, acc)
    job = _COPILOT_JOBS.get(job_id)
    if job is None or job["doc_id"] != doc_id:
        raise HTTPException(status_code=404, detail="Copilot job not found")
    document = document_service.get_document(doc_id) if job["status"] == "done" else None
    return _copilot_state(job, document)

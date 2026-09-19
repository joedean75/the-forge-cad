"""AI-native operation API surface.

- GET  /api/operations                     -> self-describing tool catalog
- POST /api/documents/{doc_id}/op          -> dispatch one operation

This is the substrate the MCP server wraps so any agent can drive Forge.
"""

import base64
import binascii
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from app.services.copilot_context import RequestContext, WorkPlan

from app.models.schemas import AssemblyDoc, DesignDocument
from app.rate_limit import rate_limit
from app.security import current_account, meter_operation
from app.services import (accounts, assembly_service, cad_operations, caller, copilot,
                          document_service, llm_client)
from app.services import op_errors

router = APIRouter(tags=["operations"])

# Flood control on the operation surface.
#
# Quota metering already bounds *mutating* calls per account, and does nothing
# about the rest: a query op is free, and an unauthenticated deployment attributes
# every caller to one shared dev account. Both leave a CPU-bound endpoint — some
# of these evaluate geometry — open to a flood from one client. The limits are
# well above any real session (an editing burst is a handful of ops a second,
# and a drag commits once on release, not per frame) and low enough to bound the
# damage. Buckets key on the route *template*, so walking documents does not mint
# a fresh one.
_op_limit = rate_limit(max_hits=300, window_seconds=60.0)
# A copilot run is many LLM turns and many evaluations; an optimize is a SIMP
# solve. Neither is something a human starts hundreds of times a minute.
_job_limit = rate_limit(max_hits=20, window_seconds=300.0)

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
    # Optimistic concurrency, opt-in. A caller that passes the revision it read
    # gets a 409 instead of silently overwriting a change it never saw; one that
    # omits it keeps the old behaviour, which is right for the single-tab case
    # that is most of the traffic.
    expected_revision: Optional[int] = None


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
    # The document's revision after this call. Pass it back as
    # `expected_revision` on the next write to chain edits without a re-read.
    revision: Optional[int] = None
    document: Optional[DesignDocument] = None


@router.get("/api/operations")
def list_operations():
    """Self-describing catalog of CAD operations for agents / MCP.

    Each entry carries both the descriptor block and a strict JSON Schema under
    ``schema``. For a function-calling API, ``/api/operations/tools`` returns the
    same thing already in tool-definition form.
    """
    return {"operations": cad_operations.catalog()}


@router.get("/api/operations/tools")
def list_operation_tools():
    """The action space as OpenAI-style tool definitions.

    What you hand to a function-calling API, or compile into a grammar to
    constrain decoding. Generated from the live registry, so it cannot drift from
    what the server will actually accept.
    """
    from app.services import op_schema

    return {"tools": op_schema.tool_definitions()}


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
    _rl: None = Depends(_op_limit),
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
    if op.subject != "document":
        # Pointed at the right door rather than 404ing on a real assembly id.
        raise HTTPException(status_code=400, detail={
            "code": "BAD_ARG",
            "message": f"{body.op} operates on an assembly; "
                       f"call POST /api/assemblies/{{id}}/op",
            "arg": "op"})
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

    # Attributed to the caller, not to the document's owner. One operation's
    # answer depends on who asked — `list_standard_parts` includes the caller's
    # own imported supplier parts — and on a shared document those are two
    # different accounts. See services/caller.
    with caller.acting_as(acc.id):
        env = cad_operations.execute(body.op, doc_id, body.args,
                                     expected_revision=body.expected_revision)

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
        revision=env.get("revision"),
        document=document,
    )


class CopilotRequest(BaseModel):
    goal: str
    context: RequestContext = Field(default_factory=RequestContext)
    # Browser-supplied transcript for the selected conversation. Kept separate
    # from this run's goal and step budget, and never treated as system policy.
    history: str = Field(default="", max_length=60000)
    #: Left unset the route picks the default for its own subject — an assembly
    #: build is many steps larger than an edit to one body, and one shared
    #: default would either starve the build or over-budget the edit.
    max_steps: Optional[int] = None
    #: Which model to plan with, for this run only. `POST /api/llm/model` is the
    #: other door and it is process-global: comparing two models on one goal
    #: must not change what every other session's next run uses.
    model: Optional[str] = None
    #: Reference images as `data:image/...;base64,` URIs, shown to the planner on
    #: every turn in this order. Only data URIs: an http URL would have the
    #: provider fetch whatever it names, server-side.
    images: Optional[List[str]] = None
    #: The single-image form, merged in front of `images`.
    image: Optional[str] = None


class CopilotJobState(BaseModel):
    ok: bool
    job_id: str
    status: str  # "running" | "done" | "error"
    steps: list[Dict[str, Any]]
    summary: Optional[str] = None
    #: The model this run planned with, so two runs of one goal can be told apart.
    model: Optional[str] = None
    #: Whether a reference image went with the goal. The image itself is not kept
    #: on the job: the store holds two hundred of them in process memory.
    has_image: bool = False
    image_count: int = 0
    document: Optional[DesignDocument] = None
    #: Set instead of `document` when the run was against an assembly. Named
    #: rather than polymorphic, for the reason `summarize_assembly` states its
    #: kind: a client that infers which it got will eventually infer wrong.
    assembly: Optional[AssemblyDoc] = None
    finished: bool = False
    stop_reason: Optional[str] = None
    task_state: Optional[WorkPlan] = None
    #: What the run has spent so far — calls, tokens (and how many were served
    #: from the provider's cache) and dollars where the provider reports them.
    #: Live while the run is going; nested part runs are included.
    usage: Optional[Dict[str, Any]] = None


# In-memory copilot jobs (single-instance, like the optimize job store).
_COPILOT_JOBS: Dict[str, Dict[str, Any]] = {}


def _copilot_state(job: Dict[str, Any], document: Optional[DesignDocument],
                   assembly: Optional[AssemblyDoc] = None) -> CopilotJobState:
    return CopilotJobState(
        ok=job["status"] != "error" and (job["status"] == "running" or job.get("finished", False)),
        job_id=job["id"],
        model=job.get("model"),
        has_image=bool(job.get("has_image")),
        image_count=int(job.get("image_count") or 0),
        status=job["status"],
        steps=list(job["steps"]),
        summary=job.get("summary"),
        document=document,
        assembly=assembly,
        finished=job.get("finished", False),
        stop_reason=job.get("stop_reason"),
        task_state=job.get("task_state"),
        usage=dict(job["usage"]) if job.get("usage") else None,
    )


def _run_copilot_job(job: Dict[str, Any], subject_id: str, goal: str, max_steps: int,
                     acc: accounts.Account, subject: str = "document",
                     model: Optional[str] = None,
                     images: Optional[List[str]] = None,
                     history: str = "", context: Optional[dict] = None) -> None:
    from app.services import llm_client

    job["usage"] = llm_client.new_ledger()
    try:
        outcome = copilot.run(
            subject_id,
            goal,
            ledger=job["usage"],
            max_steps=max_steps,
            meter=lambda: meter_operation(acc),
            on_step=lambda entry: job["steps"].append(entry),
            cancelled=lambda: job.get("cancel_requested", False),
            subject=subject,
            model=model,
            images=images,
            history=history,
            context=context,
            on_plan=lambda plan: job.update(task_state=plan),
        )
        job["model"] = outcome.get("model")
        job["summary"] = outcome["summary"]
        job["finished"] = outcome["finished"]
        job["stop_reason"] = outcome["stop_reason"]
        job["task_state"] = outcome.get("task_state")
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


@router.post("/api/documents/{doc_id}/copilot/{job_id}/cancel")
def cancel_copilot(doc_id: str, job_id: str, acc: accounts.Account = Depends(current_account)):
    _copilot_access(doc_id, acc)
    job = _COPILOT_JOBS.get(job_id)
    if job is None or job["subject_id"] != doc_id:
        raise HTTPException(status_code=404, detail="Copilot job not found")
    if job["status"] == "running":
        job["cancel_requested"] = True
    return {"ok": True, "cancel_requested": job.get("cancel_requested", False)}


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
    _rl: None = Depends(_job_limit),
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

    model = _checked_model(body.model)
    images = _checked_images(body, model)
    job = {"id": str(uuid.uuid4()), "subject_id": doc_id, "subject": "document",
           "status": "running", "steps": [], "summary": None, "model": model,
           "has_image": bool(images), "image_count": len(images)}
    _COPILOT_JOBS[job["id"]] = job
    prune_jobs(_COPILOT_JOBS)
    steps = body.max_steps or copilot.step_default("document")
    background_tasks.add_task(_run_copilot_job, job, doc_id, body.goal.strip(), steps,
                              acc, "document", model, images, body.history, body.context.model_dump())
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
    if job is None or job["subject_id"] != doc_id:
        raise HTTPException(status_code=404, detail="Copilot job not found")
    document = document_service.get_document(doc_id) if job["status"] == "done" else None
    return _copilot_state(job, document)


def _checked_model(requested: Optional[str]) -> Optional[str]:
    """A model this provider actually offers, or a 422 naming the ones it does.

    The same rule `POST /api/llm/model` applies, for the same reason: an
    arbitrary string here is a run that fails at the first planner call with a
    provider error nobody can act on.
    """
    if not requested:
        return None
    allowed = llm_client.available_models()
    if requested not in allowed:
        raise HTTPException(
            status_code=422,
            detail=f"unknown model {requested!r}; this provider offers: "
                   f"{', '.join(allowed)}")
    return requested


#: Decoded bytes across every image on one request. The JSON body limit is
#: 8 MB and base64 adds a third, so 5.5 MB decoded leaves room for the rest of
#: the body. The panel downscales, so six photos normally come to well under it.
MAX_TOTAL_IMAGE_BYTES = 5_500_000


def _checked_image_uri(image: str, which: str) -> int:
    """Decoded size of one well-formed image data URI, or a 422 naming which one."""
    header, sep, payload = image.partition(",")
    mime = header[5:].split(";", 1)[0] if header.startswith("data:") else ""
    if not sep or not header.endswith(";base64") or mime not in copilot.IMAGE_MIME_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"{which} must be a data URI of type "
                   + ", ".join(copilot.IMAGE_MIME_TYPES) + " (data:image/png;base64,...)")
    try:
        size = len(base64.b64decode(payload, validate=True))
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=422, detail=f"{which} is not valid base64")
    if size == 0:
        raise HTTPException(status_code=422, detail=f"{which} is empty")
    return size


def _checked_images(body: "CopilotRequest", model: Optional[str]) -> List[str]:
    """Well-formed image data URIs a vision model can take, or a 422 saying why.

    Refused here rather than forwarded, because the provider's answer to a bad
    image or a text-only model arrives mid-run as a planner error. `image` is the
    single-image form and is merged in front rather than ignored: pydantic drops
    fields it does not know, so a client still sending it would lose its image
    without any error at all.
    """
    images = ([body.image] if body.image else []) + [i for i in (body.images or []) if i]
    if body.context.viewport and body.context.viewport.image_index >= len(images):
        raise HTTPException(status_code=422, detail="viewport.image_index must name an attached image")
    if not images:
        return []
    if len(images) > copilot.MAX_IMAGES:
        raise HTTPException(
            status_code=422,
            detail=f"at most {copilot.MAX_IMAGES} images per goal; got {len(images)}")
    total = sum(_checked_image_uri(uri, f"image {i + 1}") for i, uri in enumerate(images))
    if total > MAX_TOTAL_IMAGE_BYTES:
        raise HTTPException(
            status_code=422,
            detail=f"images total {total / 1e6:.1f} MB; the limit is "
                   f"{MAX_TOTAL_IMAGE_BYTES / 1e6:.1f} MB. Send fewer or smaller images")
    resolved = model or llm_client.LLM_MODEL
    if llm_client.accepts_images(resolved) is False:
        vision = llm_client.vision_models() or []
        raise HTTPException(
            status_code=422,
            detail=f"{resolved} does not accept images; pick one that does: "
                   + (", ".join(vision) or "none offered"))
    return images


def _assembly_copilot_access(assembly_id: str, acc: accounts.Account) -> AssemblyDoc:
    """The assembly, if this account may drive it.

    `can_access` is the whole check here because an assembly has one role —
    owner or nothing — unlike a document, which can be shared read-only. It is
    the same boundary `add_component` applies to a part, and for the same
    reason: a copilot run reads every component's geometry back out through the
    interference check and the BOM.
    """
    a = assembly_service.get_assembly(assembly_id)
    if a is None or not assembly_service.can_access(a, acc.id):
        raise HTTPException(status_code=404, detail="Assembly not found")
    return a


def _assembly_copilot_job(job_id: str, assembly_id: str) -> Dict[str, Any]:
    job = _COPILOT_JOBS.get(job_id)
    # The subject check is the access check: the caller has already been cleared
    # for *this* assembly, and a job id belonging to another subject must not
    # resolve through it.
    if job is None or job["subject_id"] != assembly_id or job.get("subject") != "assembly":
        raise HTTPException(status_code=404, detail="Copilot job not found")
    return job


@router.post(
    "/api/assemblies/{assembly_id}/copilot",
    response_model=CopilotJobState,
)
def run_assembly_copilot(
    assembly_id: str,
    body: CopilotRequest,
    background_tasks: BackgroundTasks,
    acc: accounts.Account = Depends(current_account),
    _rl: None = Depends(_job_limit),
):
    """Forge Copilot against an assembly: place parts, mate them, measure the result.

    The same agentic loop the part editor runs, pointed at the other half of the
    operation catalog — the planner is offered only the fifteen operations that
    take an assembly id, because handing it the ninety-six that take a document
    id spends the step budget discovering they do not apply here.
    """
    _assembly_copilot_access(assembly_id, acc)
    if not (body.goal or "").strip():
        raise HTTPException(status_code=422, detail="goal is required")

    model = _checked_model(body.model)
    images = _checked_images(body, model)
    job = {"id": str(uuid.uuid4()), "subject_id": assembly_id, "subject": "assembly",
           "status": "running", "steps": [], "summary": None, "model": model,
           "has_image": bool(images), "image_count": len(images)}
    _COPILOT_JOBS[job["id"]] = job
    prune_jobs(_COPILOT_JOBS)
    steps = body.max_steps or copilot.step_default("assembly")
    background_tasks.add_task(_run_copilot_job, job, assembly_id, body.goal.strip(),
                              steps, acc, "assembly", model, images, body.history, body.context.model_dump())
    return _copilot_state(job, document=None)


@router.get(
    "/api/assemblies/{assembly_id}/copilot/{job_id}",
    response_model=CopilotJobState,
)
def assembly_copilot_job(assembly_id: str, job_id: str,
                         acc: accounts.Account = Depends(current_account)):
    """Poll an assembly copilot job: live transcript while running, the assembly when done."""
    _assembly_copilot_access(assembly_id, acc)
    job = _assembly_copilot_job(job_id, assembly_id)
    assembly = (assembly_service.get_assembly(assembly_id)
                if job["status"] == "done" else None)
    return _copilot_state(job, document=None, assembly=assembly)


@router.post("/api/assemblies/{assembly_id}/copilot/{job_id}/cancel")
def cancel_assembly_copilot(assembly_id: str, job_id: str,
                            acc: accounts.Account = Depends(current_account)):
    _assembly_copilot_access(assembly_id, acc)
    job = _assembly_copilot_job(job_id, assembly_id)
    if job["status"] == "running":
        job["cancel_requested"] = True
    return {"ok": True, "cancel_requested": job.get("cancel_requested", False)}


@router.post("/api/assemblies/{assembly_id}/op", response_model=OperationResponse)
def run_assembly_operation(
    assembly_id: str,
    body: OperationRequest,
    acc: accounts.Account = Depends(current_account),
    _rl: None = Depends(_op_limit),
    envelope: bool = Query(
        False,
        description="Return 200 with ok=false instead of raising on operation failure.",
    ),
):
    """Run one catalog operation against an assembly.

    The same contract as the document route — typed failures, `delta`, `state`,
    `?envelope=1` — against the other kind of thing the catalog addresses. An
    operation declares which it takes as `subject` in `GET /api/operations`, so
    a caller knows before it calls rather than after a 404.
    """
    from app.services import assembly_service

    op = cad_operations.OPERATIONS.get(body.op)
    if op is None:
        raise HTTPException(status_code=400, detail={
            "code": "UNKNOWN_OP", "message": f"unknown operation: {body.op}", "arg": "op"})
    if op.subject != "assembly":
        raise HTTPException(status_code=400, detail={
            "code": "BAD_ARG",
            "message": f"{body.op} operates on a part document; "
                       f"call POST /api/documents/{{id}}/op",
            "arg": "op"})

    asm = assembly_service.get_assembly(assembly_id)
    if asm is None or not assembly_service.can_access(asm, acc.id):
        raise HTTPException(status_code=404, detail={
            "code": "NO_SUCH_DOCUMENT", "message": "Assembly not found"})
    if op.kind == "mutate":
        # Assemblies carry no separate editor role today; access is ownership.
        meter_operation(acc)

    with caller.acting_as(acc.id):
        env = cad_operations.execute(body.op, assembly_id, body.args)
    if not env["ok"] and not envelope:
        raise HTTPException(status_code=_http_status(env["error"]), detail=env["error"])
    return OperationResponse(
        ok=env["ok"],
        op=env["op"],
        result=env.get("result") or {},
        error=env.get("error"),
        warnings=env.get("warnings") or [],
        delta=env.get("delta"),
        state=env.get("state"),
        document=None,
    )

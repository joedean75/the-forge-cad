"""AI-native operation API surface.

- GET  /api/operations                     -> self-describing tool catalog
- POST /api/documents/{doc_id}/op          -> dispatch one operation

This is the substrate the MCP server wraps so any agent can drive Forge.
"""

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.models.schemas import DesignDocument
from app.security import current_account, meter_operation
from app.services import accounts, cad_operations, copilot, document_service

router = APIRouter(tags=["operations"])


class OperationRequest(BaseModel):
    op: str
    args: Optional[Dict[str, Any]] = None


class OperationResponse(BaseModel):
    ok: bool
    op: str
    result: Dict[str, Any]
    document: Optional[DesignDocument] = None


@router.get("/api/operations")
def list_operations():
    """Self-describing catalog of CAD operations for agents / MCP."""
    return {"operations": cad_operations.catalog()}


@router.post("/api/documents/{doc_id}/op", response_model=OperationResponse)
def run_operation(doc_id: str, body: OperationRequest, acc: accounts.Account = Depends(current_account)):
    op = cad_operations.OPERATIONS.get(body.op)
    if op is None:
        raise HTTPException(status_code=400, detail=f"Unknown operation: {body.op}")
    # Enforce tenant isolation before doing any work (404, never 403, to avoid
    # leaking the existence of other tenants' documents).
    doc = document_service.get_document(doc_id)
    if doc is None or not document_service.can_access(doc, acc.id):
        raise HTTPException(status_code=404, detail="Document not found")
    # Mutating ops require editor access and are metered against the plan quota;
    # read-only queries only require view access (free).
    if op.kind == "mutate":
        if not document_service.can_edit(doc, acc.id):
            raise HTTPException(status_code=403, detail="this action requires editor access")
        meter_operation(acc)
    try:
        result = cad_operations.dispatch(body.op, doc_id, body.args)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Not found: {exc}")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    document = document_service.get_document(doc_id) if op.kind == "mutate" else None
    return OperationResponse(ok=True, op=body.op, result=result, document=document)


class CopilotRequest(BaseModel):
    goal: str
    max_steps: int = copilot.MAX_STEPS_DEFAULT


class CopilotResponse(BaseModel):
    ok: bool
    summary: str
    steps: list[Dict[str, Any]]
    document: Optional[DesignDocument] = None


@router.post("/api/documents/{doc_id}/copilot", response_model=CopilotResponse)
def run_copilot(doc_id: str, body: CopilotRequest, acc: accounts.Account = Depends(current_account)):
    """Forge Copilot: an agentic loop that plans + executes a sequence of operations.

    Editor access required (it mutates the part). Each mutating sub-step is metered
    against the plan quota via the ``meter`` callback, so the agent can't bypass limits.
    """
    doc = document_service.get_document(doc_id)
    if doc is None or not document_service.can_access(doc, acc.id):
        raise HTTPException(status_code=404, detail="Document not found")
    if not document_service.can_edit(doc, acc.id):
        raise HTTPException(status_code=403, detail="this action requires editor access")
    if not (body.goal or "").strip():
        raise HTTPException(status_code=422, detail="goal is required")

    outcome = copilot.run(doc_id, body.goal.strip(), max_steps=body.max_steps, meter=lambda: meter_operation(acc))
    return CopilotResponse(ok=True, summary=outcome["summary"], steps=outcome["steps"], document=outcome["document"])

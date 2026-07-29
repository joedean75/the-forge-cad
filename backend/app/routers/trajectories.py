"""Recorded interaction trajectories — the training-data surface.

Read-only by design: trajectories are produced as a side effect of driving the
operation API, never posted directly, so there is no way to inject a "recording"
that never happened.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse

from app.security import current_account
from app.services import accounts, document_service, trajectories

router = APIRouter(prefix="/api/trajectories", tags=["trajectories"])


def _visible(record: dict, acc: accounts.Account) -> bool:
    """A trajectory is visible to whoever can see the document it built."""
    owner = record.get("owner_id")
    if owner is not None and owner == acc.id:
        return True
    doc_id = record.get("doc_id")
    if not doc_id:
        return owner is None
    doc = document_service.get_document(doc_id)
    return doc is not None and document_service.can_access(doc, acc.id)


def _mine(acc: accounts.Account):
    return [r for r in trajectories.list_all() if _visible(r, acc)]


@router.get("")
def list_trajectories(
    acc: accounts.Account = Depends(current_account),
    doc_id: Optional[str] = Query(None, description="only trajectories for this document"),
    kind: Optional[str] = Query(None, description="session | copilot | text_to_cad | decompiled"),
):
    """Trajectory index — metadata only, without the step payloads."""
    records = _mine(acc)
    if doc_id:
        records = [r for r in records if r.get("doc_id") == doc_id]
    if kind:
        records = [r for r in records if r.get("kind") == kind]
    return {"trajectories": [{
        "id": r["id"], "doc_id": r.get("doc_id"), "goal": r.get("goal"),
        "kind": r.get("kind"), "actor": r.get("actor"), "outcome": r.get("outcome"),
        "n_steps": len(r.get("steps") or []),
        "started_at": r.get("started_at"), "finished_at": r.get("finished_at"),
    } for r in records]}


@router.get("/export.jsonl", response_class=PlainTextResponse)
def export_jsonl(
    acc: accounts.Account = Depends(current_account),
    kind: Optional[str] = Query(None),
    min_steps: int = Query(1, ge=0, description="drop trajectories with fewer steps than this"),
    include_catalog: bool = Query(True, description="prepend the op catalog as a system message"),
):
    """Every visible trajectory as JSONL, one tool-calling example per line.

    The system message is generated from the live operation catalog, so the
    training format cannot drift from the surface the model will actually call.
    """
    records = _mine(acc)
    if kind:
        records = [r for r in records if r.get("kind") == kind]
    body = trajectories.export_jsonl(records, include_catalog=include_catalog,
                                     min_steps=min_steps)
    return PlainTextResponse(body, media_type="application/x-ndjson", headers={
        "Content-Disposition": 'attachment; filename="forge_trajectories.jsonl"'})


@router.post("/decompile")
def decompile(
    acc: accounts.Account = Depends(current_account),
    doc_id: Optional[str] = Query(None, description="one document; omit to convert every visible one"),
    resolution: int = Query(48, ge=16, le=256,
                            description="meshing resolution used to verify the replay"),
):
    """Recover the operation sequence that builds a document, and record it.

    This is how an existing corpus becomes training data: text-to-CAD writes a
    whole feature tree in one shot, so those documents exercise the action space
    not at all. A sequence is only kept if replaying it reproduces the original
    geometry exactly — anything that does not is reported, never exported.
    """
    from app.services import decompiler

    if doc_id:
        doc = document_service.get_document(doc_id)
        if doc is None or not document_service.can_access(doc, acc.id):
            raise HTTPException(status_code=404, detail={
                "code": "NO_SUCH_DOCUMENT", "message": "Document not found"})
        return decompiler.decompile_document(doc_id, resolution=resolution)

    results = []
    for summary in document_service.list_documents(owner_id=acc.id):
        outcome = decompiler.decompile_document(summary.id, resolution=resolution)
        results.append({"doc_id": summary.id, "name": summary.name, "ok": outcome["ok"],
                        "reason": outcome.get("reason"), "n_steps": outcome.get("n_steps", 0),
                        "trajectory_id": outcome.get("trajectory_id")})
    return {"total": len(results), "recovered": sum(1 for r in results if r["ok"]),
            "results": results}


@router.get("/{trajectory_id}")
def get_trajectory(trajectory_id: str, acc: accounts.Account = Depends(current_account)):
    record = trajectories.get(trajectory_id)
    if record is None or not _visible(record, acc):
        raise HTTPException(status_code=404, detail={
            "code": "NO_SUCH_TRAJECTORY", "message": "Trajectory not found"})
    return record

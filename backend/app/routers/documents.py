"""Design-document HTTP surface.

Thin router over `services.document_service`. CRUD + evaluate here; optimize,
simulate, DFM and CAM endpoints are added by their respective phases.
"""

import asyncio
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from app.security import capped_resolution, current_account, require_entitlement
from app.services import accounts
from app.services.plans import ENT_CAM, ENT_OPTIMIZE, ENT_STEP_EXPORT

from app.models.schemas import (
    AddDimensionRequest,
    CamResult,
    Collaborator,
    CreateDocumentRequest,
    DesignDocument,
    Dimension,
    DocumentSummary,
    DfmReport,
    DocumentVersion,
    JobStatus,
    OptimizationSetup,
    OptimizeJob,
    SimulationResult,
    UpdateDocumentRequest,
    VersionSummary,
)
from app.services import document_analysis, document_service as docs
from app.services.simp_optimizer import ProgressState

router = APIRouter(prefix="/api/documents", tags=["documents"])

_ROLE_ORDER = {"viewer": 0, "editor": 1, "owner": 2}


def _require_role(min_role: str):
    """Dependency factory enforcing a minimum role on `{doc_id}`.

    404 when the caller has no access at all (existence not leaked); 403 when they
    can see the document but lack the required role (view vs. edit vs. manage).
    """
    def dep(doc_id: str, acc: accounts.Account = Depends(current_account)) -> DesignDocument:
        doc = docs.get_document(doc_id)
        role = docs.access_role(doc, acc.id) if doc is not None else None
        if doc is None or role is None:
            raise HTTPException(status_code=404, detail="Document not found")
        if _ROLE_ORDER[role] < _ROLE_ORDER[min_role]:
            raise HTTPException(status_code=403, detail=f"this action requires {min_role} access")
        return doc

    return dep


# View (viewer+), mutate geometry (editor+), manage/delete/share (owner).
readable_document = _require_role("viewer")
editable_document = _require_role("editor")
manageable_document = _require_role("owner")


class OptimizeDocumentRequest(BaseModel):
    setup: OptimizationSetup | None = None
    resolution: int = 128


class AiEditRequest(BaseModel):
    instruction: str
    resolution: int = 128


class CamRequest(BaseModel):
    diameter_mm: float = 3.0
    stepdown_mm: float = 1.0
    stepover_mm: float = 1.2
    stock_margin_mm: float = 4.0
    feed_mm_min: float = 600.0
    plunge_mm_min: float = 200.0
    spindle_rpm: int = 12000
    safe_z_mm: float = 5.0


@router.post("", response_model=DesignDocument)
def create_document(body: CreateDocumentRequest, acc: accounts.Account = Depends(current_account)):
    try:
        return docs.create_document(
            name=body.name,
            prompt=body.prompt,
            units=body.units,
            resolution=body.resolution,
            owner_id=acc.id,
        )
    except Exception:
        # Log the detail server-side; don't leak internals to the client.
        import logging
        logging.getLogger(__name__).exception("create_document failed")
        raise HTTPException(status_code=400, detail="Could not create document")


@router.get("", response_model=list[DocumentSummary])
def list_documents(acc: accounts.Account = Depends(current_account)):
    return docs.list_documents(owner_id=acc.id)


@router.post("/import", response_model=DesignDocument)
async def import_document(
    file: UploadFile = File(...),
    name: str | None = Form(None),
    units: str = Form("auto"),
    resolution: int = Form(96),
    acc: accounts.Account = Depends(current_account),
):
    """Universal import: STL/OBJ/PLY/GLB/OFF or faceted STEP → editable document."""
    from app.services import import_service

    data = await file.read()
    try:
        return import_service.import_document(
            filename=file.filename or "part",
            data=data,
            name=name,
            resolution=resolution,
            units=units,
            owner_id=acc.id,
        )
    except import_service.ImportError_ as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception:
        import logging

        logging.getLogger(__name__).exception("import failed")
        raise HTTPException(status_code=400, detail="Could not import this file")


@router.get("/{doc_id}", response_model=DesignDocument)
def get_document(doc: DesignDocument = Depends(readable_document)):
    # Meshes are not persisted; the first open after a restart re-evaluates.
    return docs.ensure_derived(doc)


@router.put("/{doc_id}", response_model=DesignDocument)
def update_document(
    body: UpdateDocumentRequest, doc: DesignDocument = Depends(editable_document)
):
    return docs.update_document(
        doc.id,
        name=body.name,
        feature_tree=body.feature_tree,
        optimization=body.optimization,
        resolution=body.resolution,
    )


@router.post("/{doc_id}/evaluate", response_model=DesignDocument)
def evaluate_document(
    resolution: int = Query(128, ge=32, le=512),
    doc: DesignDocument = Depends(readable_document),
):
    return docs.evaluate_document(doc, resolution=resolution)


@router.delete("/{doc_id}")
def delete_document(doc: DesignDocument = Depends(manageable_document)):
    docs.delete_document(doc.id)
    return {"status": "deleted", "id": doc.id}


@router.post("/{doc_id}/simulate", response_model=SimulationResult)
def simulate_document(
    resolution: int = Query(128, ge=32, le=512),
    doc: DesignDocument = Depends(readable_document),
):
    return document_analysis.simulate_document(doc.id, resolution=resolution)


class QuoteRequest(BaseModel):
    quantity: int = 1


class OrderRequest(BaseModel):
    process: str
    quantity: int = 1
    notes: str = ""


@router.post("/{doc_id}/quotes")
def quote_document(body: QuoteRequest, doc: DesignDocument = Depends(readable_document)):
    """Instant manufacturing quotes across every viable process ("Get it made")."""
    from app.services import quotes

    try:
        return quotes.quote_document(doc.id, quantity=body.quantity)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.post("/{doc_id}/orders")
def place_order(
    body: OrderRequest,
    acc: accounts.Account = Depends(current_account),
    doc: DesignDocument = Depends(editable_document),
):
    """Submit a manufacturing order (RFQ) for a selected process quote."""
    from app.services import quotes

    try:
        return quotes.place_order(doc.id, body.process, body.quantity, owner_id=acc.id, notes=body.notes)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.get("/{doc_id}/orders")
def list_orders(doc: DesignDocument = Depends(readable_document)):
    from app.services import quotes

    return {"orders": quotes.list_orders(doc.id)}


@router.post("/{doc_id}/dfm", response_model=DfmReport)
def dfm_document(
    build_axis: str = Query("auto"),
    resolution: int = Query(128, ge=32, le=512),
    doc: DesignDocument = Depends(readable_document),
):
    return document_analysis.dfm_document(doc.id, build_axis=build_axis, resolution=resolution)


@router.get("/{doc_id}/step", response_class=PlainTextResponse)
def export_document_step(
    resolution: int = Query(128, ge=32, le=512),
    acc: accounts.Account = Depends(require_entitlement(ENT_STEP_EXPORT)),
    doc: DesignDocument = Depends(readable_document),
):
    resolution = capped_resolution(acc, resolution)
    step_text = document_analysis.export_step_document(doc.id, resolution=resolution)
    return PlainTextResponse(
        step_text,
        media_type="application/step",
        headers={"Content-Disposition": f"attachment; filename={doc.id}.step"},
    )


# ── Generative optimization (Phase 3) ──


def _progress_to_job(job_id: str, progress: dict) -> OptimizeJob:
    stage = progress.get("stage", "optimizing")
    iteration = progress.get("iteration", 0)
    max_iter = progress.get("max_iter", 1)
    message = progress.get("message", "Optimizing")
    if stage == "meshing":
        status, pct = JobStatus.MESHING, 0.15
    elif stage == "optimizing":
        status, pct = JobStatus.OPTIMIZING, 0.2 + 0.6 * min(1.0, iteration / max(max_iter, 1))
    elif stage == "postprocessing":
        status, pct = JobStatus.POSTPROCESSING, 0.9
    else:
        status, pct = JobStatus.PENDING, 0.0
    return OptimizeJob(id=job_id, status=status, progress=round(pct, 2), message=message)


@router.post("/{doc_id}/optimize", response_model=OptimizeJob)
async def optimize_document(
    body: OptimizeDocumentRequest,
    background_tasks: BackgroundTasks,
    acc: accounts.Account = Depends(require_entitlement(ENT_OPTIMIZE)),
    doc: DesignDocument = Depends(editable_document),
):
    setup = body.setup or doc.optimization or OptimizationSetup()
    resolution = capped_resolution(acc, body.resolution)
    job_id = str(uuid.uuid4())
    docs._OPTIMIZE_JOBS[job_id] = OptimizeJob(
        id=job_id, status=JobStatus.PENDING, progress=0.0, message="Queued generative study"
    )
    # Bound the job store so a long-lived process can't grow it forever.
    while len(docs._OPTIMIZE_JOBS) > 200:
        docs._OPTIMIZE_JOBS.pop(next(iter(docs._OPTIMIZE_JOBS)), None)
    background_tasks.add_task(_run_optimize_job, job_id, doc.id, setup, resolution)
    return docs._OPTIMIZE_JOBS[job_id]


async def _run_optimize_job(job_id: str, doc_id: str, setup: OptimizationSetup, resolution: int):
    progress = ProgressState()

    async def poll():
        while True:
            await asyncio.sleep(0.4)
            docs._OPTIMIZE_JOBS[job_id] = _progress_to_job(job_id, progress.read())

    poller = asyncio.create_task(poll())
    try:
        await asyncio.to_thread(docs.run_document_optimize, doc_id, setup, resolution, progress)
        docs._OPTIMIZE_JOBS[job_id] = OptimizeJob(
            id=job_id, status=JobStatus.DONE, progress=1.0, message="Optimization complete"
        )
    except Exception as exc:
        docs._OPTIMIZE_JOBS[job_id] = OptimizeJob(
            id=job_id, status=JobStatus.FAILED, progress=0.0, message=f"Optimization failed: {exc}"
        )
    finally:
        poller.cancel()
        try:
            await poller
        except asyncio.CancelledError:
            pass


@router.get("/{doc_id}/optimize/{job_id}", response_model=OptimizeJob)
def get_optimize_job(job_id: str, doc: DesignDocument = Depends(readable_document)):
    job = docs.get_optimize_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Optimization job not found")
    return job


@router.post("/{doc_id}/ai-edit", response_model=DesignDocument)
def ai_edit_document(body: AiEditRequest, doc: DesignDocument = Depends(editable_document)):
    if not body.instruction or not body.instruction.strip():
        raise HTTPException(status_code=400, detail="Instruction is required")
    try:
        return docs.ai_edit_document(doc.id, body.instruction, body.resolution)
    except KeyError:
        raise HTTPException(status_code=404, detail="Document not found")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.post("/{doc_id}/dimensions", response_model=Dimension)
def add_dimension(body: AddDimensionRequest, doc: DesignDocument = Depends(editable_document)):
    dim = docs.add_dimension(doc.id, body)
    if dim is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return dim


@router.delete("/{doc_id}/dimensions/{dim_id}")
def delete_dimension(dim_id: str, doc: DesignDocument = Depends(editable_document)):
    if not docs.remove_dimension(doc.id, dim_id):
        raise HTTPException(status_code=404, detail="Dimension not found")
    return {"status": "deleted", "id": dim_id}


@router.post("/{doc_id}/cam", response_model=CamResult)
def cam_document(body: CamRequest,
                 acc: accounts.Account = Depends(require_entitlement(ENT_CAM)),
                 doc: DesignDocument = Depends(readable_document)):
    from app.services.cam.toolpath import ToolParams

    tool = ToolParams(
        diameter_mm=body.diameter_mm,
        stepdown_mm=body.stepdown_mm,
        stepover_mm=body.stepover_mm,
        stock_margin_mm=body.stock_margin_mm,
        feed_mm_min=body.feed_mm_min,
        plunge_mm_min=body.plunge_mm_min,
        spindle_rpm=body.spindle_rpm,
        safe_z_mm=body.safe_z_mm,
    )
    return document_analysis.cam_document(doc.id, tool)


# ── Version history (snapshots / restore / branch) ──


class SnapshotRequest(BaseModel):
    label: str = ""


class BranchRequest(BaseModel):
    name: str | None = None
    resolution: int = 128


@router.get("/{doc_id}/versions", response_model=list[VersionSummary])
def list_versions(doc: DesignDocument = Depends(readable_document)):
    return [
        VersionSummary(id=v.id, label=v.label, author_id=v.author_id, created_at=v.created_at)
        for v in reversed(doc.version_history)
    ]


@router.post("/{doc_id}/versions", response_model=DocumentVersion)
def create_version(
    body: SnapshotRequest,
    acc: accounts.Account = Depends(current_account),
    doc: DesignDocument = Depends(editable_document),
):
    version = docs.snapshot_version(doc.id, body.label, author_id=acc.id)
    if version is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return version


@router.post("/{doc_id}/versions/{version_id}/restore", response_model=DesignDocument)
def restore_version(
    version_id: str,
    resolution: int = Query(128, ge=32, le=512),
    doc: DesignDocument = Depends(editable_document),
):
    try:
        restored = docs.restore_version(doc.id, version_id, resolution=resolution)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return restored


@router.post("/{doc_id}/versions/{version_id}/branch", response_model=DesignDocument)
def branch_version(
    version_id: str,
    body: BranchRequest,
    acc: accounts.Account = Depends(current_account),
    doc: DesignDocument = Depends(readable_document),
):
    try:
        branch = docs.branch_from_version(
            doc.id, version_id, owner_id=acc.id, name=body.name, resolution=body.resolution
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return branch


# ── Collaboration (sharing) ──


class ShareRequest(BaseModel):
    email: str
    role: str = "viewer"


@router.get("/{doc_id}/collaborators", response_model=list[Collaborator])
def list_collaborators(doc: DesignDocument = Depends(readable_document)):
    return doc.collaborators


@router.post("/{doc_id}/collaborators", response_model=DesignDocument)
def add_collaborator(body: ShareRequest, doc: DesignDocument = Depends(manageable_document)):
    try:
        return docs.add_collaborator(doc.id, body.email, body.role)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/{doc_id}/collaborators/{account_id}", response_model=DesignDocument)
def remove_collaborator(account_id: str, doc: DesignDocument = Depends(manageable_document)):
    return docs.remove_collaborator(doc.id, account_id)

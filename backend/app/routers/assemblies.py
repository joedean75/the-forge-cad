"""Assembly-document HTTP surface.

Thin router over `services.assembly_service`. An assembly composes part documents
(insert parts, place each instance). Owner-scoped like documents: a caller with no
access gets a 404 (existence not leaked).
"""

import logging
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.models.schemas import (
    AddComponentRequest,
    AssemblyDoc,
    AssemblySummary,
    CreateAssemblyRequest,
    UpdateAssemblyRequest,
    UpdateComponentRequest,
)
from app.rate_limit import rate_limit
from app.security import current_account
from app.services import accounts, assembly_service as asm

router = APIRouter(prefix="/api/assemblies", tags=["assemblies"])

# Text-to-assembly is an LLM job that then evaluates every part it invents.
_job_limit = rate_limit(max_hits=20, window_seconds=300.0)
# The same budget part import has: each upload reads and meshes whole files.
_upload_limit = rate_limit(max_hits=30, window_seconds=300.0)


def owned_assembly(assembly_id: str, acc: accounts.Account = Depends(current_account)) -> AssemblyDoc:
    a = asm.get_assembly(assembly_id)
    if a is None or not asm.can_access(a, acc.id):
        raise HTTPException(status_code=404, detail="Assembly not found")
    return a


@router.get("/{assembly_id}/artifact/{digest}")
def get_assembly_artifact(digest: str, a: AssemblyDoc = Depends(owned_assembly)):
    """A file an assembly operation produced, addressed by the hash of its bytes.

    The document endpoint's contract exactly — immutable, `private` because the
    route is access-checked, and a 404 rather than different bytes under a
    digest a client was told to keep. It exists because `render_view` renders an
    *assembly*, and every artifact URL until now named a document; storing an
    assembly's views under a document id would have put them behind an access
    check on the wrong object.
    """
    from fastapi.responses import Response

    from app.services import artifacts

    artifact = artifacts.fetch(a.id, digest)
    if artifact is None:
        raise HTTPException(status_code=404, detail="No artifact with that hash")
    return Response(
        content=artifact.payload,
        media_type=artifact.media_type,
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


@router.post("", response_model=AssemblyDoc)
def create_assembly(body: CreateAssemblyRequest, acc: accounts.Account = Depends(current_account)):
    return asm.create_assembly(name=body.name, owner_id=acc.id)


@router.get("", response_model=list[AssemblySummary])
def list_assemblies(acc: accounts.Account = Depends(current_account)):
    return asm.list_assemblies(owner_id=acc.id)


# ── Text-to-assembly: one prompt → generated parts placed as an assembly ──
# Runs as a background job (one LLM plan + one text-to-CAD run per part can
# span minutes); poll the job id, then open the finished assembly.

_PROMPT_JOBS: Dict[str, Dict[str, Any]] = {}


def _prune_jobs() -> None:
    while len(_PROMPT_JOBS) > 100:
        _PROMPT_JOBS.pop(next(iter(_PROMPT_JOBS)), None)


class AssemblyFromPromptRequest(BaseModel):
    prompt: str
    resolution: int = Field(96, ge=32, le=256)


class AssemblyPromptJob(BaseModel):
    job_id: str
    status: str  # "running" | "done" | "error"
    progress: Optional[str] = None
    assembly_id: Optional[str] = None
    error: Optional[str] = None
    # What to do about the error, when the failure carries one. The commonest
    # one here is "no model configured", which is useless without it.
    remedy: Optional[str] = None


def _run_prompt_job(job: Dict[str, Any], prompt: str, resolution: int, owner_id: Optional[str]) -> None:
    from app.services import text_to_assembly

    try:
        result = text_to_assembly.create_assembly_from_prompt(
            prompt, owner_id=owner_id, resolution=resolution,
            on_progress=lambda msg: job.__setitem__("progress", msg),
        )
        job["assembly_id"] = result.id
        job["status"] = "done"
    except Exception as exc:  # noqa: BLE001 — the job must always resolve
        logging.getLogger(__name__).warning("text-to-assembly failed: %s", exc)
        # `remedy` rides along when the failure has one. Without it the commonest
        # error here — no model configured — reaches the user as a statement of
        # fact with nothing to do about it, while every other typed failure in
        # the product says where to go next.
        job["error"] = str(exc)
        remedy = getattr(exc, "remedy", None)
        if remedy:
            job["remedy"] = remedy
        job["status"] = "error"


@router.post("/from-prompt", response_model=AssemblyPromptJob)
def assembly_from_prompt(
    body: AssemblyFromPromptRequest,
    background_tasks: BackgroundTasks,
    _rl: None = Depends(_job_limit),
    acc: accounts.Account = Depends(current_account),
):
    if not body.prompt.strip():
        raise HTTPException(status_code=422, detail="prompt is required")
    job: Dict[str, Any] = {"id": str(uuid.uuid4()), "owner_id": acc.id, "status": "running",
                           "progress": "queued", "assembly_id": None, "error": None}
    _PROMPT_JOBS[job["id"]] = job
    _prune_jobs()
    background_tasks.add_task(_run_prompt_job, job, body.prompt.strip(), body.resolution, acc.id)
    return AssemblyPromptJob(job_id=job["id"], status=job["status"], progress=job["progress"])


class AssemblyImportResult(BaseModel):
    assembly: AssemblyDoc
    #: False when the file had no assembly structure and each solid became a
    #: component where the file put it. Said, because it was inferred.
    structured: bool
    units: str
    parts_created: int
    instances: int
    #: Parts created as mirror images, because a placement cannot reflect.
    mirrored: int = 0
    #: What did not become a component, with why, rather than dropped silently.
    skipped: List[Dict[str, Any]] = []


@router.post("/import", response_model=AssemblyImportResult)
async def import_assembly(
    _rl: None = Depends(_upload_limit),
    file: UploadFile = File(...),
    name: Optional[str] = Form(None),
    units: str = Form("auto"),
    acc: accounts.Account = Depends(current_account),
):
    """A STEP assembly → one exact part per distinct part, one component per instance.

    Run in the thread pool: a large assembly is many OCC reads and meshes, and
    doing that on the event loop would stall every other request meanwhile.
    """
    from app.services import import_service

    data = await file.read()
    try:
        assembly, report = await run_in_threadpool(
            import_service.import_assembly, file.filename or "assembly.step", data,
            name=name, units=units, owner_id=acc.id)
    except import_service.ImportError_ as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception:
        logging.getLogger(__name__).exception("assembly import failed")
        raise HTTPException(status_code=400, detail="Could not import this assembly")
    return AssemblyImportResult(assembly=assembly, **report)


@router.get("/from-prompt/{job_id}", response_model=AssemblyPromptJob)
def assembly_from_prompt_job(job_id: str, acc: accounts.Account = Depends(current_account)):
    job = _PROMPT_JOBS.get(job_id)
    if job is None or job["owner_id"] != acc.id:
        raise HTTPException(status_code=404, detail="Job not found")
    return AssemblyPromptJob(job_id=job["id"], status=job["status"], progress=job.get("progress"),
                             assembly_id=job.get("assembly_id"), error=job.get("error"),
                             remedy=job.get("remedy"))


@router.get("/{assembly_id}", response_model=AssemblyDoc)
def get_assembly(a: AssemblyDoc = Depends(owned_assembly)):
    return a


@router.put("/{assembly_id}", response_model=AssemblyDoc)
def update_assembly(body: UpdateAssemblyRequest, a: AssemblyDoc = Depends(owned_assembly)):
    if body.name is not None:
        return asm.rename_assembly(a.id, body.name)
    return a


@router.delete("/{assembly_id}")
def delete_assembly(a: AssemblyDoc = Depends(owned_assembly)):
    asm.delete_assembly(a.id)
    return {"ok": True}


@router.post("/{assembly_id}/components", response_model=AssemblyDoc)
def add_component(body: AddComponentRequest, a: AssemblyDoc = Depends(owned_assembly)):
    try:
        updated = asm.add_component(a.id, body.part_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Part not found")
    if updated is None:
        raise HTTPException(status_code=404, detail="Assembly not found")
    return updated


@router.put("/{assembly_id}/components/{component_id}", response_model=AssemblyDoc)
def update_component(
    component_id: str, body: UpdateComponentRequest, a: AssemblyDoc = Depends(owned_assembly)
):
    updated = asm.update_component(a.id, component_id, body.transform)
    if updated is None:
        raise HTTPException(status_code=404, detail="Component not found")
    return updated


@router.delete("/{assembly_id}/components/{component_id}", response_model=AssemblyDoc)
def remove_component(component_id: str, a: AssemblyDoc = Depends(owned_assembly)):
    updated = asm.remove_component(a.id, component_id)
    if updated is None:
        raise HTTPException(status_code=404, detail="Component not found")
    return updated


class AddMateRequest(BaseModel):
    type: str
    base: str
    moved: str
    axis: str = "z"
    base_face: str = "max"
    moved_face: str = "min"
    offset_mm: float = 0.0
    angle_deg: float = 0.0


@router.post("/{assembly_id}/mates", response_model=AssemblyDoc)
def add_mate(body: AddMateRequest, a: AssemblyDoc = Depends(owned_assembly)):
    """Add a mate (fasten/coincident/concentric/distance) and re-solve placement."""
    try:
        updated = asm.add_mate(a.id, {"id": "", **body.model_dump()})
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if updated is None:
        raise HTTPException(status_code=404, detail="Assembly not found")
    return updated


@router.delete("/{assembly_id}/mates/{mate_id}", response_model=AssemblyDoc)
def remove_mate(mate_id: str, a: AssemblyDoc = Depends(owned_assembly)):
    updated = asm.remove_mate(a.id, mate_id)
    if updated is None:
        raise HTTPException(status_code=404, detail="Mate not found")
    return updated


@router.get("/{assembly_id}/interference")
def check_interference(clearance_mm: float = 1.0, a: AssemblyDoc = Depends(owned_assembly)):
    """Pairwise interference + clearance across placed components (feature 08)."""
    from app.services import interference

    return interference.check_assembly(a.id, clearance_mm=clearance_mm)


@router.get("/{assembly_id}/bom")
def bill_of_materials(a: AssemblyDoc = Depends(owned_assembly)):
    """Bill of Materials for the assembly — grouped parts, mass and cost (feature 05)."""
    from app.services import bom

    return bom.bill_of_materials(a.id)


@router.get("/{assembly_id}/bom.csv")
def bom_csv(a: AssemblyDoc = Depends(owned_assembly)):
    from fastapi.responses import PlainTextResponse
    from app.services import bom

    return PlainTextResponse(
        bom.bom_csv(a.id), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={a.name[:40].replace(' ', '_') or 'assembly'}_bom.csv"},
    )

"""Assembly-document HTTP surface.

Thin router over `services.assembly_service`. An assembly composes part documents
(insert parts, place each instance). Owner-scoped like documents: a caller with no
access gets a 404 (existence not leaked).
"""

import logging
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field

from app.models.schemas import (
    AddComponentRequest,
    AssemblyDoc,
    AssemblySummary,
    CreateAssemblyRequest,
    UpdateAssemblyRequest,
    UpdateComponentRequest,
)
from app.security import current_account
from app.services import accounts, assembly_service as asm

router = APIRouter(prefix="/api/assemblies", tags=["assemblies"])


def owned_assembly(assembly_id: str, acc: accounts.Account = Depends(current_account)) -> AssemblyDoc:
    a = asm.get_assembly(assembly_id)
    if a is None or not asm.can_access(a, acc.id):
        raise HTTPException(status_code=404, detail="Assembly not found")
    return a


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
        job["error"] = str(exc)
        job["status"] = "error"


@router.post("/from-prompt", response_model=AssemblyPromptJob)
def assembly_from_prompt(
    body: AssemblyFromPromptRequest,
    background_tasks: BackgroundTasks,
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


@router.get("/from-prompt/{job_id}", response_model=AssemblyPromptJob)
def assembly_from_prompt_job(job_id: str, acc: accounts.Account = Depends(current_account)):
    job = _PROMPT_JOBS.get(job_id)
    if job is None or job["owner_id"] != acc.id:
        raise HTTPException(status_code=404, detail="Job not found")
    return AssemblyPromptJob(job_id=job["id"], status=job["status"], progress=job.get("progress"),
                             assembly_id=job.get("assembly_id"), error=job.get("error"))


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

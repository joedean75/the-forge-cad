"""CAD-level operations on stored candidates and assemblies."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.models.schemas import Candidate, AssemblyResult
from app.cad_kernel.core.feature_tree import FeatureTree
from app.services.mesh_generator import reevaluate_candidate, CANDIDATE_DATA
from app.services.assembly_generator import ASSEMBLY_DATA
from app.services import persistence

router = APIRouter(prefix="/api/cad", tags=["cad"])


class ReevaluateRequest(BaseModel):
    feature_tree: dict
    resolution: int = Field(128, ge=32, le=512)


@router.post("/{candidate_id}/reevaluate", response_model=Candidate)
def reevaluate(candidate_id: str, body: ReevaluateRequest):
    try:
        tree = FeatureTree.from_dict(body.feature_tree)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid feature tree: {exc}")
    try:
        return reevaluate_candidate(candidate_id, tree, resolution=body.resolution)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Candidate not found: {exc}")


@router.get("/candidates", response_model=list[Candidate])
def list_candidates():
    """Return all persisted candidates."""
    results = []
    for candidate_id in persistence.list_candidates():
        data = persistence.load_candidate(candidate_id)
        if not data:
            continue
        try:
            results.append(Candidate(**data["candidate"]))
        except Exception:
            continue
    return results


@router.get("/candidates/{candidate_id}", response_model=Candidate)
def get_candidate(candidate_id: str):
    data = persistence.load_candidate(candidate_id)
    if not data:
        raise HTTPException(status_code=404, detail="Candidate not found")
    try:
        return Candidate(**data["candidate"])
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not load candidate: {exc}")


@router.get("/assemblies", response_model=list[AssemblyResult])
def list_assemblies():
    """Return all persisted assemblies."""
    results = []
    for assembly_id in persistence.list_assemblies():
        data = persistence.load_assembly(assembly_id)
        if not data:
            continue
        try:
            results.append(AssemblyResult(**data))
        except Exception:
            continue
    return results


@router.get("/assemblies/{assembly_id}", response_model=AssemblyResult)
def get_assembly(assembly_id: str):
    data = persistence.load_assembly(assembly_id)
    if not data:
        raise HTTPException(status_code=404, detail="Assembly not found")
    try:
        return AssemblyResult(**data)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not load assembly: {exc}")

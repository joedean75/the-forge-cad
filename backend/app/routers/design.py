"""Unified design generation endpoint.

Accepts a free-form description and returns either a single part (Candidate)
or an assembly (AssemblyResult), ready for export.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.models.schemas import AssemblyResult, Candidate
from app.services.design_intent import classify_and_generate

router = APIRouter(prefix="/api/design", tags=["design"])


class DesignGenerateRequest(BaseModel):
    prompt: str
    resolution: int = Field(128, ge=32, le=512)


class DesignGenerateResponse(BaseModel):
    kind: str = Field(pattern="^(part|assembly)$")
    part: Candidate | None = None
    assembly: AssemblyResult | None = None


@router.post("/generate", response_model=DesignGenerateResponse)
def generate_design(body: DesignGenerateRequest):
    if not body.prompt or not body.prompt.strip():
        raise HTTPException(status_code=400, detail="Prompt is required")
    try:
        result = classify_and_generate(body.prompt, resolution=body.resolution)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not generate design: {exc}")
    return DesignGenerateResponse(
        kind=result["kind"],
        part=result.get("part"),
        assembly=result.get("assembly"),
    )

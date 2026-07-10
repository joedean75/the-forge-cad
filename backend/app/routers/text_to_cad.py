from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.models.schemas import Candidate, DesignSpec
from app.services.text_to_cad import parse_prompt
from app.services.text_to_cad_geometry import parse_geometry_prompt
from app.services.mesh_generator import candidate_from_feature_tree

router = APIRouter(prefix="/api/text-to-cad", tags=["text-to-cad"])


class TextToCadRequest(BaseModel):
    prompt: str
    spec: DesignSpec


class TextToCadGenerateRequest(BaseModel):
    prompt: str
    resolution: int = Field(128, ge=32, le=512)


@router.post("", response_model=DesignSpec)
def text_to_cad(body: TextToCadRequest):
    if not body.prompt or not body.prompt.strip():
        raise HTTPException(status_code=400, detail="Prompt is required")
    return parse_prompt(body.prompt, body.spec)


@router.post("/generate", response_model=Candidate)
def text_to_cad_generate(body: TextToCadGenerateRequest):
    if not body.prompt or not body.prompt.strip():
        raise HTTPException(status_code=400, detail="Prompt is required")
    try:
        tree = parse_geometry_prompt(body.prompt)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not parse geometry prompt: {exc}")
    name = body.prompt.strip()
    if len(name) > 60:
        name = name[:57] + "..."
    return candidate_from_feature_tree(tree, name=name or "Prompt Part", resolution=body.resolution)

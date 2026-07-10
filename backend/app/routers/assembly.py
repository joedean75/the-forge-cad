import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.models.schemas import AssemblyResult, NodePart, Strut, Vector3
from app.services.assembly_generator import generate_assembly

router = APIRouter(prefix="/api/assembly", tags=["assembly"])


class AssemblyGenerateRequest(BaseModel):
    prompt: str
    resolution: int = Field(128, ge=32, le=512)


@router.get("", response_model=AssemblyResult)
def get_assembly():
    return AssemblyResult(
        id=str(uuid.uuid4()),
        nodes=[
            NodePart(id="n1", name="Node A", material="Aluminum 7075", quantity=4, print_time_minutes=45),
            NodePart(id="n2", name="Node B", material="Aluminum 7075", quantity=6, print_time_minutes=38),
            NodePart(id="n3", name="Node C", material="Aluminum 7075", quantity=2, print_time_minutes=62),
        ],
        struts=[
            Strut(id="s1", start=Vector3(x=-1, y=0, z=0), end=Vector3(x=1, y=0, z=0), diameter=0.08),
            Strut(id="s2", start=Vector3(x=-1, y=0, z=0), end=Vector3(x=0, y=0.8, z=0), diameter=0.06),
            Strut(id="s3", start=Vector3(x=1, y=0, z=0), end=Vector3(x=0, y=0.8, z=0), diameter=0.06),
            Strut(id="s4", start=Vector3(x=-1, y=0, z=0), end=Vector3(x=0, y=0, z=0.6), diameter=0.05),
            Strut(id="s5", start=Vector3(x=1, y=0, z=0), end=Vector3(x=0, y=0, z=0.6), diameter=0.05),
        ],
    )


@router.post("/generate", response_model=AssemblyResult)
def generate(body: AssemblyGenerateRequest):
    if not body.prompt or not body.prompt.strip():
        raise HTTPException(status_code=400, detail="Prompt is required")
    try:
        return generate_assembly(body.prompt, resolution=body.resolution)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Assembly generation failed: {exc}")

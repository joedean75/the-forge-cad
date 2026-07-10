"""Sketch constraint-solver surface."""

from typing import Any, Dict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services import sketch_solver

router = APIRouter(prefix="/api/sketch", tags=["sketch"])


class SolveRequest(BaseModel):
    points: list
    lines: list = []
    circles: list = []
    constraints: list = []


@router.post("/solve")
def solve(body: SolveRequest):
    spec: Dict[str, Any] = body.model_dump()
    try:
        result = sketch_solver.solve_sketch(spec)
    except sketch_solver.SketchError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    # best-effort profile loop
    try:
        result["polygon"] = sketch_solver.sketch_to_polygon(spec, result)
    except sketch_solver.SketchError:
        result["polygon"] = None
    return result

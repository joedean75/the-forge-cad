import asyncio
import uuid
from fastapi import APIRouter, BackgroundTasks, HTTPException
from typing import Dict

from app.models.schemas import (
    DesignSpec,
    OptimizeJob,
    OptimizeResult,
    JobStatus,
)
from app.services.mesh_generator import generate_candidates
from app.services.simp_optimizer import ProgressState

router = APIRouter(prefix="/api/optimize", tags=["optimize"])

# In-memory job store
_jobs: Dict[str, OptimizeJob] = {}
_results: Dict[str, OptimizeResult] = {}


def _progress_to_status(progress: dict) -> OptimizeJob:
    stage = progress.get("stage", "optimizing")
    iteration = progress.get("iteration", 0)
    max_iter = progress.get("max_iter", 1)
    message = progress.get("message", "Optimizing")

    if stage == "meshing":
        status = JobStatus.MESHING
        pct = 0.1 + 0.25
    elif stage == "optimizing":
        status = JobStatus.OPTIMIZING
        pct = 0.35 + 0.45 * min(1.0, iteration / max(max_iter, 1))
    elif stage == "postprocessing":
        status = JobStatus.POSTPROCESSING
        pct = 0.85
    else:
        status = JobStatus.PENDING
        pct = 0.0

    return OptimizeJob(
        id=progress.get("job_id", ""),
        status=status,
        progress=round(pct, 2),
        message=message,
    )


@router.post("", response_model=OptimizeJob)
async def start_optimize(spec: DesignSpec, background_tasks: BackgroundTasks):
    job_id = str(uuid.uuid4())
    job = OptimizeJob(
        id=job_id,
        status=JobStatus.PENDING,
        progress=0.0,
        message="Queued generative study",
    )
    _jobs[job_id] = job

    background_tasks.add_task(_run_job, job_id, spec)
    return job


async def _run_job(job_id: str, spec: DesignSpec):
    progress = ProgressState()

    async def progress_polling_task():
        while True:
            await asyncio.sleep(0.5)
            p = progress.read()
            p["job_id"] = job_id
            _jobs[job_id] = _progress_to_status(p)

    poll = asyncio.create_task(progress_polling_task())

    try:
        candidates = await asyncio.to_thread(generate_candidates, spec, 4, progress)
        _results[job_id] = OptimizeResult(job_id=job_id, candidates=candidates)
        _jobs[job_id] = OptimizeJob(
            id=job_id,
            status=JobStatus.DONE,
            progress=1.0,
            message="Generative study complete",
        )
    except Exception as exc:
        _jobs[job_id] = OptimizeJob(
            id=job_id,
            status=JobStatus.FAILED,
            progress=0.0,
            message=f"Optimization failed: {exc}",
        )
    finally:
        poll.cancel()
        try:
            await poll
        except asyncio.CancelledError:
            pass


@router.get("/{job_id}", response_model=OptimizeJob)
def get_job(job_id: str):
    if job_id not in _jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    return _jobs[job_id]


@router.get("/{job_id}/result", response_model=OptimizeResult)
def get_result(job_id: str):
    if job_id not in _results:
        raise HTTPException(status_code=404, detail="Result not ready or not found")
    return _results[job_id]

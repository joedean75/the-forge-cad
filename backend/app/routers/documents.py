"""Design-document HTTP surface.

Thin router over `services.document_service`. CRUD + evaluate here; optimize,
simulate, DFM and CAM endpoints are added by their respective phases.
"""

import asyncio
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, Response, UploadFile
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel

from app.rate_limit import rate_limit
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
from app.cad_kernel.io import mesh_binary
from app.services import document_analysis, document_service as docs, slicer
from app.services.simp_optimizer import ProgressState

router = APIRouter(prefix="/api/documents", tags=["documents"])

# An upload is bounded in size by `app/body_limit.py`; this bounds the *rate*.
# Each one is a parse plus (for a mesh) a voxelisation, so a stream of them is
# CPU and memory pressure from a single client regardless of any one file's size.
_upload_limit = rate_limit(max_hits=30, window_seconds=300.0)
# A SIMP solve and a release package are the two slowest things in the system;
# both fan out across every expensive stage.
_job_limit = rate_limit(max_hits=20, window_seconds=300.0)

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
    _rl: None = Depends(_upload_limit),
    file: UploadFile = File(...),
    name: str | None = Form(None),
    units: str = Form("auto"),
    resolution: int = Form(96),
    acc: accounts.Account = Depends(current_account),
):
    """Universal import → editable document.

    STEP/IGES come in as exact B-rep (addressable faces, exact measurement,
    editable re-export); STL/OBJ/PLY/GLB/OFF are voxelized.
    """
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


@router.post("/from-image", response_model=DesignDocument)
async def create_document_from_image(
    response: Response,
    _rl: None = Depends(_upload_limit),
    file: UploadFile = File(...),
    name: str | None = Form(None),
    prompt: str | None = Form(None),
    width_mm: float = Form(100.0),
    depth_mm: float = Form(4.0),
    resolution: int = Form(96),
    acc: accounts.Account = Depends(current_account),
):
    """Generate a part from an image.

    A vision-capable LLM models it when one is configured; otherwise the
    silhouette is traced into an extruded profile — exact, instant, and
    credential-free. Either way the result is an ordinary editable feature tree.
    """
    from app.services import image_to_cad

    data = await file.read()
    try:
        doc, source = image_to_cad.create_document_from_image(
            data=data,
            filename=file.filename or "image.png",
            name=name,
            prompt=prompt,
            width_mm=width_mm,
            depth_mm=depth_mm,
            resolution=resolution,
            owner_id=acc.id,
        )
    except image_to_cad.ImageError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception:
        import logging

        logging.getLogger(__name__).exception("image-to-CAD failed")
        raise HTTPException(status_code=400, detail="Could not build a part from that image")

    # Which engine produced it is advisory, so it rides on a header rather than
    # in the document — nothing downstream should branch on it.
    response.headers["X-Forge-Source"] = source
    return doc


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
    process: str = Query("additive"),
    doc: DesignDocument = Depends(readable_document),
):
    return document_analysis.dfm_document(
        doc.id, build_axis=build_axis, resolution=resolution, process=process)


@router.get("/{doc_id}/mesh/{mesh_hash}")
def get_document_mesh(
    mesh_hash: str,
    doc: DesignDocument = Depends(readable_document),
):
    """The body's triangles, addressed by the hash of the triangles themselves.

    The mesh used to be base64-inlined into `derived.mesh_url`, which made it
    part of the document body — 99% of it, in fact, and re-downloaded on every
    edit including ones that could not move the geometry. Behind a URL keyed by
    `mesh_hash` it is *immutable by construction*: the same hash is the same
    triangles, so the browser can cache it forever and an edit that leaves the
    body alone costs nothing.

    `private`, not `public`: this is behind an access check, and a shared proxy
    caching it publicly would hand one tenant's geometry to another.
    """
    payload = document_analysis.mesh_payload(doc.id, mesh_hash)
    if payload is None:
        # The hash names geometry this document does not currently have. Almost
        # always a stale client holding a URL from before an edit — 404 rather
        # than quietly serving the *current* mesh under the old hash, which
        # would poison every cache that saw it.
        raise HTTPException(status_code=404, detail="No mesh with that hash on this document")
    return Response(
        content=payload,
        media_type=mesh_binary.MIME,
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


@router.get("/{doc_id}/thumbnail/{mesh_hash}")
def get_document_thumbnail(
    mesh_hash: str,
    doc: DesignDocument = Depends(readable_document),
):
    """A shaded picture of the body, addressed by the hash of its triangles.

    The library showed a generic icon per card, so two parts were
    indistinguishable until one was opened. Keyed on `mesh_hash` for the same
    reason the mesh is: the picture is of a *specific* body, and one keyed on the
    document id would confidently show last week's part.

    404 rather than a placeholder when the hash is not this document's geometry
    — a placeholder cached forever under a real hash is the failure this
    addressing scheme exists to prevent.
    """
    from app.services import thumbnail

    payload = thumbnail.for_document(doc.id, mesh_hash)
    if payload is None:
        raise HTTPException(status_code=404, detail="No thumbnail for that hash")
    return Response(
        content=payload,
        media_type=thumbnail.MIME,
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


@router.get("/{doc_id}/artifact/{digest}")
def get_document_artifact(
    digest: str,
    doc: DesignDocument = Depends(readable_document),
):
    """An exported file, addressed by the hash of its own bytes.

    The same contract as the mesh and the thumbnail: immutable, cached forever,
    and a 404 rather than a *different* file under a digest a client has been
    told to keep. `private` because the endpoint is access-checked — a shared
    proxy caching it publicly would hand one tenant's geometry to another.

    Exports are held in a bounded LRU, so an old digest eventually stops
    resolving. That is a 404 the caller re-exports from, not a wrong answer.
    """
    from app.services import artifacts

    artifact = artifacts.fetch(doc.id, digest)
    if artifact is None:
        raise HTTPException(status_code=404, detail="No export with that hash")
    return Response(
        content=artifact.payload,
        media_type=artifact.media_type,
        headers={
            "Cache-Control": "private, max-age=31536000, immutable",
            "Content-Disposition": f'attachment; filename="{artifact.filename}"',
        },
    )


class ReleasePackageRequest(BaseModel):
    revision: Optional[str] = None
    drawing_spec: Optional[dict] = None


class ReleasePackageJob(BaseModel):
    id: str
    status: str                       # "running" | "done" | "error"
    message: str = ""
    result: Optional[dict] = None


# Bounded, process-local, and lost on restart — the same shape as the optimize
# job store, and for the same reason: the *artifact* is the durable thing and it
# is addressed by its own hash.
_PACKAGE_JOBS: Dict[str, ReleasePackageJob] = {}


@router.post("/{doc_id}/package", response_model=ReleasePackageJob)
async def start_release_package(
    body: ReleasePackageRequest,
    background_tasks: BackgroundTasks,
    _rl: None = Depends(_job_limit),
    doc: DesignDocument = Depends(readable_document),
):
    """Start building the handover package for a released revision.

    A job rather than a request because a package is a fan-out of every slow
    thing in the system — a drawing render is ~2.9 s, the STEP export is a build
    and the spec report meshes the body. Poll `GET /{doc_id}/package/{job_id}`.
    """
    job_id = str(uuid.uuid4())
    _PACKAGE_JOBS[job_id] = ReleasePackageJob(
        id=job_id, status="running", message="Building the release package")
    while len(_PACKAGE_JOBS) > 200:
        _PACKAGE_JOBS.pop(next(iter(_PACKAGE_JOBS)), None)
    background_tasks.add_task(_run_package_job, job_id, doc.id, body.revision,
                              body.drawing_spec)
    return _PACKAGE_JOBS[job_id]


async def _run_package_job(job_id: str, doc_id: str, revision: Optional[str],
                           drawing_spec: Optional[dict]):
    from app.services import release_package

    try:
        result = await asyncio.to_thread(
            release_package.build, doc_id, revision=revision, drawing_spec=drawing_spec)
        _PACKAGE_JOBS[job_id] = ReleasePackageJob(
            id=job_id, status="done",
            message=f"Revision {result['revision']} packaged", result=result)
    except Exception as exc:  # noqa: BLE001 — the message is the deliverable here
        _PACKAGE_JOBS[job_id] = ReleasePackageJob(
            id=job_id, status="error", message=str(exc))


@router.get("/{doc_id}/package/{job_id}", response_model=ReleasePackageJob)
def get_release_package_job(job_id: str, doc: DesignDocument = Depends(readable_document)):
    job = _PACKAGE_JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Package job not found")
    return job


@router.get("/{doc_id}/step")
def export_document_step(
    resolution: int = Query(128, ge=32, le=512),
    acc: accounts.Account = Depends(require_entitlement(ENT_STEP_EXPORT)),
    doc: DesignDocument = Depends(readable_document),
):
    """Stream a faceted STEP export, refusing ones too large to build safely.

    A faceted STEP costs roughly 1.3 kB per triangle, so a part meshed at a high
    resolution reaches hundreds of megabytes — enough to exhaust a small
    instance. The triangle budget turns that into an actionable 413 instead of
    an OOM, and streaming keeps a second full copy off the heap.
    """
    resolution = capped_resolution(acc, resolution)
    try:
        chunks, triangles = document_analysis.stream_step_document(doc.id, resolution=resolution)
    except document_analysis.ExportTooLargeError as exc:
        raise HTTPException(
            status_code=413,
            detail=(
                f"STEP export would contain {exc.triangles:,} triangles (limit "
                f"{exc.limit:,}). Re-request with a lower `resolution`."
            ),
        )
    return StreamingResponse(
        chunks,
        media_type="application/step",
        headers={
            "Content-Disposition": f"attachment; filename={doc.id}.step",
            "X-Forge-Triangles": str(triangles),
        },
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
    _rl: None = Depends(_job_limit),
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
        doc = await asyncio.to_thread(docs.run_document_optimize, doc_id, setup, resolution, progress)
        removed = (doc.feature_tree.get("root", {}).get("params", {})
                   .get("setup", {}).get("removed_features", []))
        message = "Optimization complete"
        if removed:
            names = ", ".join(r["name"] for r in removed[:3])
            more = f" +{len(removed) - 3} more" if len(removed) > 3 else ""
            message = f"Optimization complete — removed {names}{more}. Mark features to keep and re-run."
        docs._OPTIMIZE_JOBS[job_id] = OptimizeJob(
            id=job_id, status=JobStatus.DONE, progress=1.0,
            message=message, removed_features=removed,
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


# ── Materials, parameters, mass properties ──


@router.get("/meta/standard-parts")
def list_standard_parts(
    category: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    limit: int = Query(60, ge=1, le=250),
    acc: accounts.Account = Depends(current_account),
):
    """The purchased parts, as a resource rather than an operation.

    `list_standard_parts` also exists in the operation catalog, because an agent
    is always holding a document or an assembly id and the whole surface is
    addressed by one. A person browsing the library is not: a *new* assembly has
    no components and no open part, which is exactly when you want to reach for
    a screw. Requiring an id there would be the same mistake as making "create
    an assembly" an operation — an argument that means nothing.

    Both doors go through `standard_parts.listing`, so there is one catalog and
    it is scoped to the caller: the generated parts everyone has, plus the
    supplier files this account imported.
    """
    from app.services import standard_parts

    return standard_parts.listing(acc.id, category=category, search=search,
                                  source=source, limit=limit)


@router.post("/meta/supplier-parts")
async def import_supplier_part(
    _rl: None = Depends(_upload_limit),
    file: UploadFile = File(...),
    designation: str = Form(""),
    source: str = Form("other"),
    manufacturer: str = Form(""),
    part_number: str = Form(""),
    category: str = Form("other"),
    source_url: str = Form(""),
    notes: str = Form(""),
    units: str = Form("auto"),
    acc: accounts.Account = Depends(current_account),
):
    """File a supplier's CAD file as a purchased part of this account's.

    A multipart upload rather than an operation, for the reason every other
    import is: an operation's arguments are JSON and a STEP body is not. The
    *placing* of the part is an operation (`insert_standard_part`), so an agent
    can still use everything a person imports.

    This is the only lawful way this catalog grows beyond the generated
    standards — see the module docstring of `services/supplier_parts`. The
    person who accepted the portal's terms fetches the file; nothing here
    reaches out to a portal, and nothing is shared beyond the importing account.
    """
    from app.services import import_service, supplier_parts

    data = await file.read()
    try:
        row = supplier_parts.import_file(
            filename=file.filename or "part.step", data=data, owner_id=acc.id,
            designation=designation, source=source, manufacturer=manufacturer,
            part_number=part_number, category=category, source_url=source_url,
            notes=notes, units=units)
    except (ValueError, import_service.ImportError_) as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception:
        import logging

        logging.getLogger(__name__).exception("supplier part import failed")
        raise HTTPException(status_code=400, detail="Could not import this file")
    return supplier_parts.describe(row)


@router.delete("/meta/supplier-parts/{key:path}")
def forget_supplier_part(key: str, acc: accounts.Account = Depends(current_account)):
    """Take an imported part out of the catalog.

    Its document is left alone on purpose: the part may be placed in three
    assemblies, and a catalog row is a shortcut to placing it again rather than
    the thing keeping it alive. Deleting the document is the library's job.
    """
    from app.services import supplier_parts

    if not supplier_parts.forget(key, acc.id):
        raise HTTPException(status_code=404, detail=f"no imported part {key!r}")
    return {"ok": True, "key": key}


@router.get("/meta/materials")
def list_materials(acc: accounts.Account = Depends(current_account)):
    """The material catalog plus its category descriptors.

    Categories come down alongside the materials (rather than being inferred by
    the client) so display order and labels stay owned by the catalog.
    """
    from app.services import materials

    return {
        "materials": [m.model_dump() for m in materials.list_materials()],
        "categories": materials.list_categories(),
    }


class MaterialRequest(BaseModel):
    material_id: str


@router.post("/{doc_id}/material", response_model=DesignDocument, response_model_exclude={"version_history"})
def set_material(body: MaterialRequest, doc: DesignDocument = Depends(editable_document)):
    updated = docs.set_material(doc.id, body.material_id)
    return docs.ensure_derived(updated)


class ParametersRequest(BaseModel):
    parameters: list


@router.put("/{doc_id}/parameters", response_model=DesignDocument, response_model_exclude={"version_history"})
def set_parameters(body: ParametersRequest, doc: DesignDocument = Depends(editable_document)):
    from app.services import parameters as param_svc

    try:
        updated = docs.set_parameters(doc.id, body.parameters)
    except param_svc.ParameterError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return docs.ensure_derived(updated)


class ConfigurationsRequest(BaseModel):
    configurations: list
    active: str | None = None


@router.put("/{doc_id}/configurations", response_model=DesignDocument, response_model_exclude={"version_history"})
def set_configurations(body: ConfigurationsRequest, doc: DesignDocument = Depends(editable_document)):
    try:
        updated = docs.set_configurations(doc.id, body.configurations, body.active)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return docs.ensure_derived(updated)


class ActivateConfigRequest(BaseModel):
    name: str | None = None


@router.post("/{doc_id}/configurations/activate", response_model=DesignDocument, response_model_exclude={"version_history"})
def activate_configuration(body: ActivateConfigRequest, doc: DesignDocument = Depends(editable_document)):
    try:
        updated = docs.activate_configuration(doc.id, body.name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return docs.ensure_derived(updated)


@router.get("/{doc_id}/mass-properties")
def mass_properties(doc: DesignDocument = Depends(readable_document)):
    from app.services import mass_properties as mp

    try:
        return mp.mass_properties(doc.id)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.get("/meta/fit")
def compute_fit(nominal_mm: float, fit: str, acc: accounts.Account = Depends(current_account)):
    """ISO-286 fit/tolerance limits for a nominal size (feature 02)."""
    from app.services import tolerances

    try:
        return tolerances.fit(nominal_mm, fit) if "/" in fit else tolerances.limits_mm(nominal_mm, fit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


# ── Revision & release management (feature 09) ──


class LifecycleRequest(BaseModel):
    state: str


@router.post("/{doc_id}/state", response_model=DesignDocument, response_model_exclude={"version_history", "revisions"})
def set_lifecycle_state(body: LifecycleRequest, doc: DesignDocument = Depends(editable_document)):
    try:
        return docs.set_lifecycle_state(doc.id, body.state)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


class ReleaseRequest(BaseModel):
    notes: str = ""
    label: str = ""


@router.get("/{doc_id}/revisions")
def list_revisions(doc: DesignDocument = Depends(readable_document)):
    """Released revisions (letter, state, who/when, notes) — payload excluded."""
    return [{"id": r.id, "letter": r.letter, "label": r.label, "state": r.state,
             "released_by": r.released_by, "released_at": r.released_at, "notes": r.notes}
            for r in doc.revisions]


@router.post("/{doc_id}/revisions")
def release_revision(body: ReleaseRequest, doc: DesignDocument = Depends(editable_document),
                     acc: accounts.Account = Depends(current_account)):
    """Cut an immutable released revision from the current geometry."""
    rev = docs.release_revision(doc.id, notes=body.notes, author_id=acc.id, label=body.label)
    if rev is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return {"id": rev.id, "letter": rev.letter, "label": rev.label, "state": rev.state,
            "released_at": rev.released_at, "notes": rev.notes}


class TolerancesRequest(BaseModel):
    general_tolerance: str | None = None
    tolerances: list = []
    gdt: list = []


@router.put("/{doc_id}/tolerances", response_model=DesignDocument, response_model_exclude={"version_history"})
def set_tolerances(body: TolerancesRequest, doc: DesignDocument = Depends(editable_document)):
    updated = docs.set_tolerances(doc.id, body.general_tolerance, body.tolerances, body.gdt)
    return updated


@router.get("/{doc_id}/drawing")
def drawing(section: bool = True, doc: DesignDocument = Depends(readable_document)):
    """A dimensioned multi-view 2D engineering drawing (SVG) of the part.

    Draws the standard third-angle front/top/right with a mid-body section. To
    choose views, sections, dimensions, annotations, projection, sheet or scale,
    POST a drawing spec to this same path.
    """
    from app.services import drawing as drawing_svc

    try:
        return drawing_svc.generate_drawing(doc.id, section=section)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))


class DrawingSpecRequest(BaseModel):
    """A drawing spec. Every field is a drafting decision, not geometry."""

    views: Optional[list] = None
    sections: Optional[list] = None
    dimensions: Optional[list] = None
    annotations: Optional[list] = None
    projection: Optional[str] = None
    sheet: Optional[str] = None
    scale: Optional[Any] = None
    show_hidden: Optional[bool] = None


@router.post("/{doc_id}/drawing")
def drawing_from_spec(body: DrawingSpecRequest,
                      doc: DesignDocument = Depends(readable_document)):
    """Render a drawing to an explicit spec."""
    from app.services import drawing as drawing_svc

    try:
        return drawing_svc.generate_drawing(
            doc.id, spec=body.model_dump(exclude_none=True))
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.post("/{doc_id}/drawing/check")
def drawing_check(body: DrawingSpecRequest,
                  doc: DesignDocument = Depends(readable_document)):
    """Validate a drawing as a manufacturing document."""
    from app.services import drawing as drawing_svc

    return drawing_svc.check_drawing(doc.id, spec=body.model_dump(exclude_none=True))


@router.get("/{doc_id}/profile-cut")
def profile_cut(kerf_mm: float = 0.2, plane: str = "xy", feed_mm_min: float = 1200.0,
                power: float = 100.0, doc: DesignDocument = Depends(readable_document)):
    """2D laser/plasma/waterjet cut paths + G-code for the part outline."""
    from app.services.cam import profile_cut as pc

    try:
        return pc.profile_cut(doc.id, kerf_mm=kerf_mm, plane=plane, feed_mm_min=feed_mm_min, power=power)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.get("/{doc_id}/nest")
def nest_sheet(machine_id: str = "tci-dl1530", quantity: int = 1, kerf_mm: float = 0.2,
               spacing_mm: float = 5.0, doc: DesignDocument = Depends(readable_document)):
    """Nest copies of the part's cut profile onto the machine's sheet."""
    from app.services.cam import nesting

    try:
        return nesting.nest(doc.id, machine_id, quantity=quantity, kerf_mm=kerf_mm, spacing_mm=spacing_mm)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))


class PostRequest(BaseModel):
    machine_id: str
    gcode: str | None = None
    source: str = "print"  # print | cut — which generated program to post


@router.post("/{doc_id}/post")
def post_gcode(body: PostRequest, doc: DesignDocument = Depends(readable_document)):
    """Post-process a program to a machine's dialect (the last step before dispatch)."""
    import base64

    from app.services.cam import post, profile_cut as pc

    src = body.gcode
    if not src:
        if body.source == "cut":
            src = pc.profile_cut(doc.id)["gcode"]
        else:
            cam = doc.derived.cam
            if cam is None or not cam.gcode_url:
                raise HTTPException(status_code=400, detail="No program yet — slice or cut the part first")
            src = base64.b64decode(cam.gcode_url.split(",", 1)[1]).decode("utf-8")
    return post.post_process(src, body.machine_id)


@router.get("/{doc_id}/profile-cut.gcode", response_class=PlainTextResponse)
def profile_cut_gcode(kerf_mm: float = 0.2, plane: str = "xy", feed_mm_min: float = 1200.0,
                      power: float = 100.0, doc: DesignDocument = Depends(readable_document)):
    from app.services.cam import profile_cut as pc

    try:
        out = pc.profile_cut(doc.id, kerf_mm=kerf_mm, plane=plane, feed_mm_min=feed_mm_min, power=power)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return PlainTextResponse(out["gcode"], media_type="text/plain",
                             headers={"Content-Disposition": f"attachment; filename={doc.name[:40].replace(' ', '_') or 'part'}_cut.gcode"})


@router.get("/{doc_id}/flat-pattern.dxf", response_class=PlainTextResponse)
def flat_pattern_dxf(doc: DesignDocument = Depends(readable_document)):
    """The sheet-metal blank as a DXF: outline on CUT, bend lines on BEND.

    This is the handoff to the shop — the blank goes straight to a laser or
    waterjet, with the bend lines on their own layer so they are marked, never
    cut.
    """
    try:
        dxf = document_analysis.flat_pattern_dxf(doc.id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    name = (doc.name[:40].replace(" ", "_") or "part")
    return PlainTextResponse(
        dxf,
        media_type="image/vnd.dxf",
        headers={"Content-Disposition": f"attachment; filename={name}_flat.dxf"},
    )


@router.get("/{doc_id}/drawing.svg")
def drawing_svg(section: bool = True, doc: DesignDocument = Depends(readable_document)):
    from fastapi.responses import Response
    from app.services import drawing as drawing_svc

    try:
        out = drawing_svc.generate_drawing(doc.id, section=section)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return Response(out["svg"], media_type="image/svg+xml")


@router.post("/{doc_id}/drawing.dxf", response_class=PlainTextResponse)
def drawing_dxf(spec: Optional[dict] = None,
                doc: DesignDocument = Depends(readable_document)):
    """The same drawing as an R12 DXF — geometry on named layers, in paper mm.

    The SVG is a sheet to read; this is the file the next tool measures and
    edits. Views and section outlines land on VISIBLE/HIDDEN/SECTION, and the
    cutting-plane lines on CUTPLANE with a dash-dot linetype, so it opens as a
    drawing rather than as a pile of lines.
    """
    from app.services import drawing as drawing_svc

    try:
        body = drawing_svc.render_dxf(doc.id, spec=spec)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    name = (doc.name[:40].replace(" ", "_") or "part")
    return PlainTextResponse(body, media_type="application/dxf", headers={
        "Content-Disposition": f'attachment; filename="{name}_drawing.dxf"'})


# ── FDM slicing (3D printing) ──


@router.post("/{doc_id}/slice")
def slice_document_route(
    body: slicer.SliceSettings,
    acc: accounts.Account = Depends(require_entitlement(ENT_CAM)),
    doc: DesignDocument = Depends(readable_document),
):
    """Slice the body for FDM printing. Returns stats + a sampled layer preview;
    the full G-code is stored on the document (download via GET /{doc_id}/gcode,
    print via /api/machine/send-document)."""
    import base64

    try:
        out = slicer.slice_document(doc.id, body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    b64 = base64.b64encode(out["gcode"].encode("utf-8")).decode("ascii")
    doc.derived.cam = CamResult(
        gcode_url=f"data:text/plain;base64,{b64}",
        operations=[f"fdm {body.layer_height}mm/{body.wall_count} walls/{int(body.infill * 100)}% infill"],
        estimated_minutes=out["stats"]["estimated_minutes"],
    )
    return {"stats": out["stats"], "preview": out["preview"]}


@router.get("/{doc_id}/gcode", response_class=PlainTextResponse)
def download_gcode(doc: DesignDocument = Depends(readable_document)):
    """The last sliced G-code as a downloadable file."""
    import base64

    cam = doc.derived.cam
    if cam is None or not cam.gcode_url:
        raise HTTPException(status_code=404, detail="No G-code — slice the part first")
    _, b64 = cam.gcode_url.split(",", 1)
    return PlainTextResponse(
        base64.b64decode(b64).decode("utf-8"),
        media_type="text/plain",
        headers={"Content-Disposition": f"attachment; filename={doc.name[:40].replace(' ', '_') or 'part'}.gcode"},
    )


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

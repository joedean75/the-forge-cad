from dotenv import load_dotenv

load_dotenv()

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.routers import materials, optimize, simulate, manufacturing, text_to_cad, assembly, export, cad, design, llm, documents, machine, operations, sketch, auth
from app.services import accounts, persistence
from app.services.persistence import list_candidates, load_candidate, list_assemblies, load_assembly
from app.services.mesh_generator import CANDIDATE_DATA
from app.services.assembly_generator import ASSEMBLY_DATA
from app.services import document_service
from app.models.schemas import Candidate, AssemblyResult


def _load_persisted_data():
    """Restore candidate and assembly metadata from the datastore on startup.

    Solids are not re-evaluated here to keep startup fast; they are generated
    lazily when export/manufacturing endpoints need them.
    """
    # Ensure the schema exists and fold any legacy JSON files into the database
    # before anything reads from it.
    persistence.init_db()
    migrated = persistence.migrate_json_files()
    if migrated:
        logging.getLogger(__name__).info("Migrated %d legacy JSON records into the database", migrated)

    for candidate_id in list_candidates():
        data = load_candidate(candidate_id)
        if not data:
            continue
        try:
            candidate = Candidate(**data["candidate"])
            CANDIDATE_DATA[candidate_id] = {
                "spec": None,
                "material": None,
                "kernel_solid": None,
                "feature_tree": data["feature_tree"],
                "candidate_mesh": None,
                "threshold": 0.0,
                "candidate": candidate,
            }
        except Exception:
            continue

    for assembly_id in list_assemblies():
        data = load_assembly(assembly_id)
        if not data:
            continue
        try:
            ASSEMBLY_DATA[assembly_id] = AssemblyResult(**data)
        except Exception:
            continue

    document_service.restore_documents()
    accounts.restore_accounts()


def _check_production_posture() -> None:
    """Warn loudly about insecure configuration when auth enforcement is on."""
    log = logging.getLogger(__name__)
    from app.security import AUTH_REQUIRED

    if not AUTH_REQUIRED:
        log.warning(
            "DAPS_AUTH_REQUIRED is off — every request is attributed to a shared "
            "enterprise dev account. Set DAPS_AUTH_REQUIRED=1 for any deployment."
        )
    if AUTH_REQUIRED and not os.environ.get("DAPS_JWT_SECRET"):
        log.error(
            "DAPS_AUTH_REQUIRED is on but DAPS_JWT_SECRET is unset — session tokens "
            "use an ephemeral per-process secret and will be invalidated on restart. "
            "Set a stable DAPS_JWT_SECRET."
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_persisted_data()
    _check_production_posture()
    yield


app = FastAPI(title="DAPS Design Mock API", lifespan=lifespan)


# Baseline security headers on every response (defense-in-depth for the SPA + API).
@app.middleware("http")
async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        # The SPA bundles its own assets; allow same-origin + inline styles (Vite)
        # and connect to self. Tightened from a permissive default.
        "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; "
        "base-uri 'self'",
    )
    return response


# CORS origins are configurable; default to the local dev frontend. In production
# the SPA is served same-origin (StaticFiles mount) so CORS is not needed for it —
# only set DAPS_CORS_ORIGINS for genuinely separate front-end origins. A wildcard
# is refused together with credentials (browsers reject it and it is unsafe).
_cors_origins = [o.strip() for o in os.environ.get("DAPS_CORS_ORIGINS", "http://localhost:5173").split(",") if o.strip()]
if "*" in _cors_origins:
    logging.getLogger(__name__).warning("Ignoring wildcard CORS origin with credentials; set explicit origins.")
    _cors_origins = ["http://localhost:5173"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(materials.router)
app.include_router(optimize.router)
app.include_router(simulate.router)
app.include_router(manufacturing.router)
app.include_router(text_to_cad.router)
app.include_router(assembly.router)
app.include_router(export.router)
app.include_router(cad.router)
app.include_router(design.router)
app.include_router(llm.router)
app.include_router(documents.router)
app.include_router(machine.router)
app.include_router(operations.router)
app.include_router(sketch.router)
app.include_router(auth.router)


@app.get("/api/health")
def health():
    return {"status": "ok"}


# Serve the built React/Vite frontend so the app works from a single URL.
dist_dir = Path(__file__).resolve().parent.parent / "frontend" / "dist"


@app.get("/landing", include_in_schema=False)
def landing_page():
    """Marketing landing page (also reachable at /landing.html via the mount)."""
    from fastapi import HTTPException
    from fastapi.responses import FileResponse

    page = dist_dir / "landing.html"
    if not page.exists():
        raise HTTPException(status_code=404, detail="landing page not built")
    return FileResponse(str(page))


if dist_dir.exists():
    app.mount("/", StaticFiles(directory=str(dist_dir), html=True), name="static")

from dotenv import load_dotenv

load_dotenv()

import base64
import hashlib
import re
from functools import lru_cache
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app.body_limit import BodySizeLimitMiddleware
from fastapi.staticfiles import StaticFiles

from app.routers import llm, documents, machine, operations, sketch, auth, assemblies, collab, trajectories
from app.services import accounts, persistence

_LOGGER = logging.getLogger(__name__)


def _load_persisted_data():
    """Prepare the datastore on startup.

    Nothing heavy is loaded eagerly: documents, candidates and assemblies are
    restored lazily on first access (their payloads embed meshes and would cost
    gigabytes of RAM up front). Only the small account records come into memory.
    """
    # Ensure the schema exists and fold any legacy JSON files into the database
    # before anything reads from it (both passes are marker-gated one-time work).
    persistence.init_db()
    migrated = persistence.migrate_json_files()
    if migrated:
        logging.getLogger(__name__).info("Migrated %d legacy JSON records into the database", migrated)
    stripped = persistence.strip_document_meshes()
    if stripped:
        logging.getLogger(__name__).info("Slimmed %d document rows (derived meshes now re-evaluated on demand)", stripped)

    accounts.restore_accounts()


def _check_production_posture() -> None:
    """Warn loudly about insecure configuration when auth enforcement is on."""
    log = logging.getLogger(__name__)
    from app.security import AUTH_REQUIRED

    if not AUTH_REQUIRED:
        log.warning(
            "FORGE_AUTH_REQUIRED is off — every request is attributed to a shared "
            "enterprise dev account. Set FORGE_AUTH_REQUIRED=1 for any deployment."
        )
    if AUTH_REQUIRED and not os.environ.get("FORGE_JWT_SECRET"):
        # Fatal, not a warning. The signing key falls back to a per-*process*
        # random value, so this is not merely "tokens die on restart": run more
        # than one worker and each signs with a different key, so a session is
        # valid or a 401 depending on which worker answers — an intermittent
        # logout nobody will diagnose from a log line that scrolled past at
        # boot. A misconfiguration that only shows up as flakiness has to stop
        # the process.
        message = (
            "FORGE_AUTH_REQUIRED is on but FORGE_JWT_SECRET is unset. Session "
            "tokens would be signed with an ephemeral per-process secret: they "
            "break on restart and, with more than one worker, at random. Set a "
            "stable FORGE_JWT_SECRET (e.g. `openssl rand -hex 32`), or set "
            "FORGE_ALLOW_INSECURE=1 to start anyway."
        )
        if os.environ.get("FORGE_ALLOW_INSECURE", "").lower() in ("1", "true", "yes"):
            log.error("%s (continuing: FORGE_ALLOW_INSECURE is set)", message)
        else:
            raise RuntimeError(message)


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio

    from app.services import realtime

    realtime.set_loop(asyncio.get_running_loop())
    _load_persisted_data()
    _check_production_posture()
    yield


app = FastAPI(title="Forge Design Mock API", lifespan=lifespan)


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
        # The SPA bundles its own assets; allow same-origin plus inline styles
        # (Vite emits them). Tightened from a permissive default.
        #
        # `connect-src` used to allow `data:` because an evaluated solid reached
        # the viewport as `derived.mesh_url`, an STL data URI that three.js's
        # STLLoader fetched over XHR. P1 made the mesh a hash-keyed same-origin
        # URL and the loader went with it, so the allowance has been dead since —
        # nothing in the client fetches a `data:` URL any more. `img-src` keeps
        # `data:` and `blob:`: the library thumbnails are authed fetches turned
        # into blob URLs, and the sketch backdrop draws a data-URI image.
        #
        # CSP only applies where FastAPI serves the SPA (production /
        # single-origin); the Vite dev server sends none, which is why a mistake
        # here is invisible in dev.
        _csp(),
    )
    return response


# Compression still earns its place, though the reason it was added is gone: a
# document used to carry its mesh as a base64 STL data URI and ran to ~11 MB,
# and P1 moved the triangles to a hash-keyed URL fetched once. What is left is
# JSON — feature trees, observations, DFM reports, the drawing SVG — which is
# what compresses well. Level 1 rather than the default 9 on purpose: this is a
# single-worker CPU-bound server, and level 1 got 5.2x for 87 ms on the old
# payloads where level 9 spent 3x the CPU for another 20%.
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=1)

# Added last, so it runs *first*: Starlette applies middleware outermost-last,
# and a body-size check that runs after the body has been buffered is not a
# check. See app/body_limit.py — every upload route reads the whole file before
# testing its length, and the operation endpoint buffers whatever it is handed.
app.add_middleware(BodySizeLimitMiddleware)


# CORS origins are configurable; default to the local dev frontend. In production
# the SPA is served same-origin (StaticFiles mount) so CORS is not needed for it —
# only set FORGE_CORS_ORIGINS for genuinely separate front-end origins. A wildcard
# is refused together with credentials (browsers reject it and it is unsafe).
def _default_cors_origins() -> str:
    """Dev convenience, but never in production.

    The Vite dev server origin is only a sensible default when auth is not being
    enforced. Once FORGE_AUTH_REQUIRED is on, defaulting to an origin the
    operator never asked for would let a page on localhost:5173 make credentialed
    cross-origin calls; production is same-origin and needs no CORS at all.
    """
    from app.security import AUTH_REQUIRED

    return "" if AUTH_REQUIRED else "http://localhost:5173"


_cors_origins = [
    o.strip()
    for o in os.environ.get("FORGE_CORS_ORIGINS", _default_cors_origins()).split(",")
    if o.strip()
]
if "*" in _cors_origins:
    logging.getLogger(__name__).warning("Ignoring wildcard CORS origin with credentials; set explicit origins.")
    _cors_origins = [o for o in _cors_origins if o != "*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.exception_handler(Exception)
async def _unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
    """Any unhandled exception becomes structured JSON, never a bare 500.

    Starlette's default for an uncaught exception is the plain-text body
    ``Internal Server Error``. A client — and especially an agent parsing
    responses — then has nothing to branch on, and the MCP layer forwarded it
    verbatim as ``Error 500: Internal Server Error``. The cause still goes to the
    log with a traceback; only the wire format changes.
    """
    _LOGGER.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": {"code": "INTERNAL",
                            "message": f"{type(exc).__name__}: {exc}"}},
    )


app.include_router(llm.router)
app.include_router(documents.router)
app.include_router(assemblies.router)
app.include_router(collab.router)
app.include_router(machine.router)
app.include_router(operations.router)
app.include_router(sketch.router)
app.include_router(auth.router)
app.include_router(trajectories.router)


@app.get("/api/health")
def health():
    return {"status": "ok"}


# Serve the built React/Vite frontend so the app works from a single URL.
dist_dir = Path(__file__).resolve().parent.parent / "frontend" / "dist"


@lru_cache(maxsize=1)
def _csp() -> str:
    """The Content-Security-Policy header, with the SPA's inline scripts hashed.

    `index.html` carries one inline script: the `window.onerror` handler that
    paints a readable failure into `#root` when the bundle itself throws. Under
    `script-src 'self'` a browser refuses it — so the single safety net for a
    white-screen failure was itself dead, and only in production, because the
    Vite dev server sends no CSP and dev can never see it.

    The hash is computed from the file actually being served rather than pinned
    as a constant, so editing that handler cannot silently re-break it. The
    lazy alternative, `'unsafe-inline'`, would readmit every injected script the
    policy exists to stop.
    """
    inline = ""
    page = dist_dir / "index.html"
    if page.exists():
        try:
            scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
                                 page.read_text(encoding="utf-8"), re.S)
            inline = "".join(
                " 'sha256-" + base64.b64encode(hashlib.sha256(s.encode()).digest()).decode() + "'"
                for s in scripts)
        except OSError:
            pass
    return (
        "default-src 'self'; img-src 'self' data: blob:; "
        "style-src 'self' 'unsafe-inline'; "
        f"script-src 'self'{inline}; connect-src 'self'; object-src 'none'; "
        "frame-ancestors 'none'; base-uri 'self'"
    )


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

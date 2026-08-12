# Production image: build the SPA, then serve it from FastAPI in a single
# container (matching the StaticFiles mount in backend/main.py). Deploy this
# image anywhere that runs Docker (Render/Fly/Railway/EC2/…). Point DATABASE_URL
# at managed Postgres for durability; see DEPLOY.md.

# ---- stage 1: build the React/Vite frontend ----
FROM node:22-alpine AS frontend
WORKDIR /fe
COPY frontend/package*.json ./
RUN npm ci
# The frontend imports ../../../shared/types, so the shared/ dir must be present
# at the path Vite expects relative to frontend/.
COPY shared/ /shared/
COPY frontend/ ./
RUN npm run build

# ---- stage 2: build the native SDF sampler (Rust) ----
#
# Optional at runtime and worth building anyway: the SDF grid sampler is 3-30x
# faster than the numpy evaluator on the same trees, bit-for-bit identically
# (`tests/test_sdf_native_parity.py`). It had never been built by any
# deployment — the only instructions were a `maturin develop` in a README — so
# the fast path existed and had never once run in production.
#
# It only matters where the SDF runs at all: with OpenCascade present the common
# case is built exactly by the B-rep kernel and never touches this. What it does
# accelerate is what B-rep declines — smooth fillet/chamfer blends between
# bodies, uniform shells — plus every deployment without OCP.
FROM rust:1-slim-bookworm AS native
WORKDIR /native
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip \
    && pip3 install --break-system-packages --no-cache-dir maturin \
    && rm -rf /var/lib/apt/lists/*
COPY backend/native/forge_sdf/ ./forge_sdf/
# `-i python3.12` so the wheel is tagged for the runtime stage's interpreter.
# pyo3 0.22 here is not built with the abi3 feature, so a wheel is specific to
# one CPython minor version and a mismatch silently produces no fast path.
RUN cd forge_sdf && maturin build --release --out /wheels

# ---- stage 3: backend + built assets ----
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

# System libs required by trimesh / scikit-image (OpenGL, glib).
# `libgl1` replaces the old `libgl1-mesa-glx`, which was dropped in Debian 12.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/backend
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# The native sampler. Installed permissively on purpose: it is genuinely
# optional — `evaluator/sdf_native` falls back to numpy on ImportError — so a
# wheel that will not install must not take the image down with it. The build
# logs which happened, because "the fast path silently is not there" is exactly
# how it came to have never run anywhere.
COPY --from=native /wheels /tmp/wheels
RUN pip install --no-cache-dir /tmp/wheels/*.whl \
      && echo "native SDF sampler installed" \
      || echo "WARNING: native SDF wheel did not install; using the numpy evaluator"; \
    rm -rf /tmp/wheels

COPY backend/ /srv/backend/
COPY shared/ /srv/shared/
# Built SPA lands where main.py looks for it (../frontend/dist from /srv/backend).
COPY --from=frontend /fe/dist /srv/frontend/dist

EXPOSE 8000

# Single worker: the app keeps some coherent in-process state (account/apikey
# indexes, optimize jobs, solid cache, rate-limit counters). Scale vertically for
# now; DEPLOY.md documents the path to multiple workers/instances. The long
# timeout accommodates CPU-bound SIMP optimization solves.
# `--forwarded-allow-ips=` (empty) stops uvicorn/gunicorn rewriting the peer
# address from client-supplied X-Forwarded-For. Left on, any caller can spoof its
# own IP and get a fresh rate-limit bucket per request, which silently defeats
# the login brute-force limiter. The app parses the header itself, deliberately,
# under FORGE_TRUSTED_PROXY_HOPS — see app/rate_limit.py.
CMD ["gunicorn", "main:app", "-k", "uvicorn.workers.UvicornWorker", \
     "-w", "1", "-b", "0.0.0.0:8000", "--timeout", "600", "--graceful-timeout", "30", \
     "--forwarded-allow-ips="]

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

# ---- stage 2: backend + built assets ----
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

COPY backend/ /srv/backend/
COPY shared/ /srv/shared/
# Built SPA lands where main.py looks for it (../frontend/dist from /srv/backend).
COPY --from=frontend /fe/dist /srv/frontend/dist

EXPOSE 8000

# Single worker: the app keeps some coherent in-process state (account/apikey
# indexes, optimize jobs, solid cache, rate-limit counters). Scale vertically for
# now; DEPLOY.md documents the path to multiple workers/instances. The long
# timeout accommodates CPU-bound SIMP optimization solves.
CMD ["gunicorn", "main:app", "-k", "uvicorn.workers.UvicornWorker", \
     "-w", "1", "-b", "0.0.0.0:8000", "--timeout", "600", "--graceful-timeout", "30"]

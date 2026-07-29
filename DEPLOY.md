# Deploying Forge

This is the researched deployment plan for Forge: what the workload needs, the
options considered, the recommendation, and step-by-step instructions. A
CI/CD pipeline (GitHub Actions) is already wired — see the end.

## What the workload actually is

- **FastAPI backend** (Python 3.12) that is **CPU- and RAM-heavy**: it runs a real
  SIMP topology optimizer (numpy/scipy/torch) and marching-cubes meshing. Solves
  are long-running and run in a threadpool. This rules out serverless/edge (cold
  starts, execution-time and memory caps, no room for torch).
- **React/Vite SPA**, served **same-origin** by the backend's `StaticFiles` mount
  in production — one origin, no separate web host or CORS needed.
- **PostgreSQL** as the durable store (`DATABASE_URL`). Dev/tests use embedded
  SQLite automatically.
- **Some coherent in-process state** — account/API-key indexes, optimize job
  registry, evaluated-solid cache, rate-limit counters, in-flight OAuth
  state/handoff codes. **Today this means run a single instance / single
  worker.** (See "Scaling" below for the path off this.)

## Options considered

| Option | Fit | Notes |
|---|---|---|
| **PaaS + managed Postgres** (Render, Railway, Fly.io) | ★ **Recommended** | Managed TLS, managed Postgres with backups, health checks, zero-ops. One Docker image. Matches a single-instance app. |
| Single VM + Docker Compose (Hetzner, DO, EC2) | Good | Cheapest at scale; you own TLS (Caddy/Traefik), Postgres, backups, patching. `docker-compose.yml` in this repo already does this. |
| Kubernetes (EKS/GKE) | Overkill | No multi-service topology to justify the operational cost yet. |
| Serverless (Lambda/Cloud Run jobs) | ✗ Poor | torch + minutes-long CPU solves exceed limits. |

## Recommendation

**Ship the single production Docker image to a PaaS with managed Postgres.**
Render is the smoothest (a `render.yaml` blueprint is included); Fly.io or Railway
work the same way with the same image. This gives durability + backups + TLS +
health checks with no servers to run, and a clean vertical-scale path for launch.

### Deploy to Render (fastest path)

1. Push this repo to GitHub (the CI/CD workflows come with it).
2. In Render: **New → Blueprint**, select the repo. It reads `render.yaml` and
   provisions the Postgres database + the Docker web service.
3. Set the dashboard secrets (marked `sync: false`): `PUBLIC_BASE_URL` (your
   Render URL, e.g. `https://forge.onrender.com`), and if using billing:
   `STRIPE_API_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_PRO`,
   `STRIPE_PRICE_ENTERPRISE`. `LLM_API_KEY` is optional.
4. `DATABASE_URL`, `FORGE_AUTH_REQUIRED=1`, and a generated `FORGE_JWT_SECRET` are
   wired automatically by the blueprint.
5. In the **Stripe dashboard**, add a webhook endpoint pointing at
   `https://<your-domain>/api/auth/webhook/stripe` and paste its signing secret
   into `STRIPE_WEBHOOK_SECRET`.

### Deploy on your own VM (alternative)

```bash
# On the box (Docker installed):
git clone <repo> && cd cad
# Put real secrets in an env file (never commit it):
#   DATABASE_URL=postgresql://user:pass@db-host/forge
#   FORGE_AUTH_REQUIRED=1
#   FORGE_JWT_SECRET=<long random string>
#   STRIPE_* / LLM_API_KEY as needed
docker compose up -d          # brings up postgres + backend (edit compose for prod)
# Front it with Caddy/Traefik/nginx for TLS on :443 → :8000.
```

## Required production configuration

| Env var | Why |
|---|---|
| `DATABASE_URL` | Managed Postgres. `postgres://` / `postgresql://` URLs are auto-normalized to the psycopg v3 driver. |
| `FORGE_AUTH_REQUIRED=1` | **Critical.** Without it every request is attributed to a shared enterprise dev account. The app logs a loud warning at startup if it is off. |
| `FORGE_JWT_SECRET` | Stable session-token signing key. Without it tokens reset on every restart. |
| `PUBLIC_BASE_URL` | Base URL for Stripe checkout redirects. |
| `STRIPE_API_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_*` | Live billing. Omit for local/self-serve plan changes. |
| `FORGE_CORS_ORIGINS` | Only if the SPA is hosted on a different origin than the API. Same-origin (default) needs nothing. |
| `MOONRAKER_URLS` / `FORGE_ALLOW_PRIVATE_MACHINE_URLS` | Machine control SSRF policy — see below. |
| `FORGE_TRUSTED_PROXY_HOPS` | How many reverse proxies sit in front of the app. `0` (default) ignores `X-Forwarded-For` entirely; set `1` behind a single PaaS load balancer so the auth rate limiter sees real client IPs instead of the balancer's. |
| `FORGE_STEP_MAX_TRIANGLES` | Cap on a single STEP export (default 100000, ~130 MB). Oversized requests get a 413 telling them to lower `resolution` rather than exhausting RAM. |

## Security posture (enforced in code)

- **Auth**: JWT sessions + API keys; PBKDF2-hashed passwords; login is
  rate-limited and constant-time against account enumeration.
- **Client identity**: the app parses `X-Forwarded-For` itself under
  `FORGE_TRUSTED_PROXY_HOPS`, and the image runs gunicorn with
  `--forwarded-allow-ips=` so the server never rewrites the peer address from a
  client-supplied header. Leaving the server's default `--proxy-headers` on lets
  any caller spoof its IP and mint a fresh rate-limit bucket per request. If you
  run the app outside this image, pass `--no-proxy-headers`.
- **Tenant isolation**: documents are owner-scoped with role-based sharing,
  enforced on every route (404 for non-members, 403 for insufficient role).
- **SSRF protection**: outbound machine-control requests are validated
  (`app/services/machine/url_guard.py`). On a hosted deployment set
  `MOONRAKER_URLS` to an **allowlist** of printer hosts. On a self-hosted LAN box,
  set `FORGE_ALLOW_PRIVATE_MACHINE_URLS=1` to reach local printers; loopback and
  cloud-metadata addresses are always blocked.
- **Headers**: CSP, `X-Content-Type-Options`, `X-Frame-Options: DENY`,
  `Referrer-Policy`, COOP on every response.
- **Billing**: Stripe webhooks are signature-verified with a replay window;
  paid plans are granted only by the webhook, never the browser redirect.
- **Secrets**: `.env` and `backend/data/` are git-ignored; the Docker image
  excludes them via `.dockerignore`.

## Scaling (the honest path beyond one instance)

The app is currently correct on **one instance / one worker** because some state
lives in process. To scale horizontally later:

1. **Account/API-key lookups** → query the DB per request instead of the
   in-memory indexes (or put them in Redis), so any worker sees new signups.
2. **Optimize jobs** → move the `_jobs`/`_results` registry to Redis or a task
   queue (Celery/RQ) with a worker pool; this also decouples long solves from web
   workers.
3. **Rate-limit counters** → Redis (the `app/rate_limit.py` seam is drop-in).
4. **Solid cache** (`_SOLID_CACHE`) is a pure derived cache — safe to keep
   per-worker or move to Redis/object storage.
5. **OAuth login state** (`_PENDING`/`_HANDOFFS` in `services/oauth.py`) → Redis.
   Both are short-lived (10 min / 60 s) and already bounded, but a sign-in that
   starts on one worker and returns on another would fail state validation, so
   they must be shared before running more than one worker.

Until then, **scale vertically** (more CPU/RAM) and run a single worker; the
Dockerfile and `render.yaml` are configured this way.

## CI/CD (already wired)

- **`.github/workflows/ci.yml`** — on every push/PR: backend `pytest`, frontend
  lint + type-check + build, and a Docker image build. This is the merge gate.
- **`.github/workflows/deploy.yml`** — on push to `main` (and `v*` tags): builds
  and pushes a versioned image to GHCR, then pings a Render deploy hook if
  `RENDER_DEPLOY_HOOK_URL` is set as a repo secret (otherwise Render's
  `autoDeploy` handles it).

Set repo secret `RENDER_DEPLOY_HOOK_URL` (optional) to have GitHub trigger the
deploy explicitly; otherwise Render auto-deploys from the connected branch.

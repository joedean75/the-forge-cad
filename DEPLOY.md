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
- **Some coherent in-process state** — account/API-key indexes, optimize and
  release-package job registries, evaluated-solid cache, rate-limit counters,
  in-flight OAuth state/handoff codes. **Today this means run a single instance
  / single worker.** (See "Scaling" below for the path off this.)
- **Content-addressed byte caches** — mesh, thumbnail and export LRUs, each
  keyed by a hash the client is handed as a URL and told to cache forever. These
  are the sharpest edge of the single-worker rule: with two workers a client can
  be given `/artifact/{digest}` by one and 404 by the other, which looks like
  data loss rather than a routing problem. They are pure caches, so the fix is a
  shared blob store, not stickiness.

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

## Deploying it internally (the posture this is actually for)

Forge is an **internal tool** — one organisation, private repository, nothing
offered to anyone outside it. Most of this document is unchanged by that, but
four things are, and the first is a trap you meet on day one.

- **The default plan is `free`, and that is 3 documents, resolution cap 64 and
  200 ops a month** (`services/plans.py`). Nothing about an internal deployment
  changes it: `security._dev_account()` is enterprise, so with
  `FORGE_AUTH_REQUIRED` unset nobody ever sees the cap — and the first real
  signup hits a three-document wall and visibly blockier geometry than the demo
  had. Without `STRIPE_API_KEY` plan changes are local and immediate, so the fix
  costs nothing; it just has to be *done*, per account, or defaulted. Put
  everyone on `enterprise`.
- **One plan for everyone, or geometry stops being comparable.**
  `capped_resolution` clamps to the account's plan, so the same document meshes
  at 64 for one engineer and 256 for another — two people looking at the same
  part and disagreeing about its DFM report. Anything that compares meshes
  across runs (benchmarks, dataset generation, a differential test) pins
  resolution through `rollout.rollout(resolution=…)` or `FORGE_ROLLOUT_RESOLUTION`
  instead of trusting the plan.
- **Internal is not unauthenticated.** Set `FORGE_AUTH_REQUIRED=1` and
  `FORGE_JWT_SECRET`. With auth off every request is attributed to one shared
  dev account: documents are all owned by it, `can_access` has nothing to
  separate, recorded trajectories name nobody, and `list_standard_parts` returns
  that one account's supplier imports to everybody. Rate limits stay on for the
  same practical reason they exist — not hostile users, but an agent loop that
  runs 300 operations a minute by accident.
- **Printers live on the LAN**, so `FORGE_ALLOW_PRIVATE_MACHINE_URLS=1` is the
  expected setting here rather than the exception. Link-local, multicast and
  reserved addresses are still refused whatever the flag says, so cloud
  metadata stays unreachable.

Leave the `STRIPE_*` variables unset; plans then behave as local entitlements,
which is all they are being used for.

One non-deployment consequence, recorded here because this is where someone
checks before opening the tool up: the parts-catalog licence analysis in
[`docs/PURCHASED-PARTS-SOURCING.md`](docs/PURCHASED-PARTS-SOURCING.md) rests on
this exact posture. Copyright obligations trigger on distributing a copy, so
serving the tool to anyone outside the organisation is the event that revives
them. That document's last section is the checklist.

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

## Performance knobs

- **The native SDF sampler** is built by the image's `native` stage (Rust →
  wheel → installed). It is optional: if the wheel fails to build or install the
  build logs it and the numpy evaluator takes over. It matters only where the
  SDF runs at all — with OpenCascade present the common case is exact B-rep and
  never touches it.
- **`FORGE_DEVICE`** pins where the sparse solves run: `cpu`, `cuda`, `cuda:1`.
  Unset means CUDA when it is present and CPU otherwise. Both SIMP and the FEA
  analysis honour it, and the analysis reports the device it used in `solver`.
  There is no GPU requirement anywhere — it is a speed knob, not a dependency.

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
  `MOONRAKER_URLS` to an **allowlist** of printer hosts. On a self-hosted box set
  `FORGE_ALLOW_PRIVATE_MACHINE_URLS=1` to reach printers on loopback or the LAN —
  including the shipped default `http://localhost:7125`, which is refused without
  it. **Link-local, multicast and reserved addresses are refused whatever the
  flag says**, so cloud metadata (169.254.169.254) is never reachable.
- **Request body size**: capped before the body is buffered
  (`app/body_limit.py`) — 8 MB for JSON, 56 MB on the three upload routes.
  Enforced on `Content-Length` *and* on the streamed byte count, so a chunked
  upload cannot walk past it.
- **Rate limits**: the auth routes, the operation endpoints
  (`POST /api/{documents,assemblies}/{id}/op`), the uploads, and the background
  jobs (copilot, optimize, package, text-to-assembly). Buckets key on the route
  *template*, not the concrete path — keyed on the path, a caller only had to
  walk their own documents for a fresh bucket. In-process, so a second worker
  doubles every limit; front a scaled deployment with an edge/WAF limit.
- **Startup refuses an unsafe posture**: `FORGE_AUTH_REQUIRED=1` without
  `FORGE_JWT_SECRET` raises at boot rather than warning. The fallback key is
  per-process, so multiple workers would sign with different keys and log users
  out at random. `FORGE_ALLOW_INSECURE=1` overrides it for a single-worker demo.
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
2. **Optimize and release-package jobs** → move the `_jobs`/`_results` and
   `_PACKAGE_JOBS` registries to Redis or a task queue (Celery/RQ) with a worker
   pool; this also decouples long solves and package fan-outs from web workers.
3. **Rate-limit counters** → Redis (the `app/rate_limit.py` seam is drop-in).
4. **Solid cache** (`_SOLID_CACHE`) is a pure derived cache — safe to keep
   per-worker or move to Redis/object storage.
5. **Hash-keyed bytes** — the mesh, thumbnail and export/artifact LRUs — to
   object storage (S3/R2), keyed by the same digest. This one is not optional
   before a second worker: the digest is handed to the browser as an immutable
   URL, so a worker that never built that file answers 404 for something the
   client was told would exist forever. Everything in them is re-derivable, so
   the store needs no durability guarantee, only reachability.
6. **OAuth login state** (`_PENDING`/`_HANDOFFS` in `services/oauth.py`) → Redis.
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

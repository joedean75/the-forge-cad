# Architecture

Two processes, one API contract: a React/Vite SPA talks to a FastAPI backend
exclusively through `/api/*`. In development Vite proxies `/api` to `:8000`;
in production FastAPI serves the built SPA from the same origin, so the whole
app ships as one Docker image (see [../DEPLOY.md](../DEPLOY.md)).

```
frontend/src ──/api──▶ backend/app/routers ──▶ services ──▶ cad_kernel
     ▲                                   │
shared/types.ts (hand-mirrored)          ▼
                              SQLAlchemy datastore (SQLite dev / Postgres prod)
```

## The CAD kernel (`backend/app/cad_kernel/`)

Self-contained and SDF-first. A `FeatureTree` of primitives, booleans,
modifiers, sketches (`ops/`) is evaluated as a **signed distance field**
(`evaluator/sdf.py`), meshed with marching cubes (`geom/meshing.py`), and
exported to STL/STEP (`io/`). `kernel.py` is the facade. The common
box/cylinder/boolean case samples through a native Rust module (`forge_sdf`);
everything else falls back to vectorized numpy.

The SDF representation is what makes the rest of the product cheap:

- **Generative results and imports compose** — a SIMP density grid or an
  imported mesh becomes a `GridSDF` (voxel occupancy → EDT), so optimized and
  imported bodies keep participating in booleans, fillets, and shells.
- **The FDM slicer offsets by iso-contouring** the field per layer
  ([PRINTING.md](PRINTING.md)).

## Documents & operations

The durable unit is the `DesignDocument`: a feature tree + derived cache
(mesh, bbox, mass, sim/DFM/CAM results) + version history + collaborators.
All geometry mutation flows through **one operation API**
(`services/cad_operations.py`) — a self-describing catalog (`GET
/api/operations`) of typed query/mutate operations. The UI's tool ribbon, the
Copilot loop, REST agents, and the MCP server all call the same operations, so
capability parity between humans and agents is structural, not aspirational.

Derived products (meshes, G-code) are **never persisted** — they re-evaluate
lazily on first access after a restart. Documents persist as single rows in the
`records` table (SQLite in dev, set `DATABASE_URL=postgresql+psycopg://…` for
production; no code change).

## Multi-tenancy & realtime

Documents are owner-scoped with role-based sharing (viewer/editor). Access
misses return **404, never 403**, so existence is not leaked across tenants.
Live collaboration is an in-process WebSocket hub (presence +
`document_updated` broadcasts) announced from the single persistence
choke point; browsers connect with single-use 60-second tickets so JWTs never
appear in URLs. The hub, job registries, and caches are process-local — run a
single instance until they're externalized (Redis) as described in DEPLOY.md.

## Async work

Long work never holds a request open: generative optimization and Copilot runs
are background jobs polled by id (bounded in-memory registries). The Copilot
loop itself is planner-injectable — tests script it, production uses the
configured LLM.

## Configuration

Everything optional degrades gracefully with **no** credentials.

| Env | Purpose |
|---|---|
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | LLM text-to-CAD + Copilot (OpenAI-style protocol; without it, the rule-based parser runs) |
| `DATABASE_URL` | Postgres in production; embedded SQLite otherwise |
| `FORGE_AUTH_REQUIRED=1` | enforce auth (otherwise a shared dev account) |
| `FORGE_JWT_SECRET` | stable session signing in production |
| `STRIPE_API_KEY` / `STRIPE_WEBHOOK_SECRET` / `STRIPE_PRICE_*` / `PUBLIC_BASE_URL` | live billing; without them plan changes apply locally |
| `QUOTE_PROVIDER_URL` / `QUOTE_PROVIDER_KEY` | relay quoting to a supplier API (local estimator otherwise) |
| `MOONRAKER_URLS` | preconfigured printer list (`Name\|url,…`) |
| `FORGE_CORS_ORIGINS` | only needed for a separately-hosted frontend |
| `FORGE_DATA_DIR` | override the data directory (tests point this at a temp dir) |

## Testing

`backend/tests/` runs against an isolated temp database and temp data dir with
the LLM disabled — deterministic and offline. `npm run build` is the frontend
type gate. CI runs both plus a Docker build on every push (`.github/workflows`).

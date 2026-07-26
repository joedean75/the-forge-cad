# The Forge

An agentic, browser-based CAD suite: describe a part in plain language, refine it
parametrically, optimize it generatively, get instant manufacturing quotes, slice
it, and print it on your own machine — with every one of those steps equally
available to AI agents through a first-class API.

The geometry engine is real: a from-scratch **SDF-based CAD kernel** (signed
distance fields evaluated to meshes via marching cubes, with a native Rust
sampler on the hot path) and a real **3D SIMP topology optimizer**
(numpy/scipy, GPU density ops via PyTorch when CUDA is present).

## What it does

| Area | What you get |
|---|---|
| **Text-to-CAD** | A prompt bar that turns "a 100×50×30 mm bracket with two mounting holes" into an editable feature tree. LLM-first with validation + self-repair; a rule-based parser covers everything credential-free. |
| **Forge Copilot** | An agentic loop that plans and executes multi-step edits ("shell it to 2 mm and fillet the top edges") through the same operation API agents use — every step visible and undoable. |
| **Parametric modeling** | Feature tree (box/cylinder/sphere/cone, booleans, fillet/chamfer/shell/hole/pattern/transform, constrained sketches, **revolve/mirror/sweep/loft**) with a CAD-standard tool ribbon, driving parameters, configurations, dimensions, and version history. |
| **Sheet metal** | Base flange + edge flanges with real bend geometry (inside radius, angle, up or down), and **flat-pattern unfold** — developed blank size, K-factor bend allowances and bend-line positions, ready for the brake. |
| **Materials** | 43 grades in 12 browsable categories, tagged maker or professional — filaments and resins through to tool steel, titanium, PEEK and composites — driving mass, simulation, DFM and quotes. |
| **Generative design** | SIMP topology optimization scoped to *your part* — the result folds back into the tree as an `OptimizeFeature` and stays editable. |
| **Universal import** | STL / OBJ / PLY / GLB / OFF and faceted STEP become editable bodies (voxelized into the SDF representation). |
| **Validate & Make** | Simulation on the evaluated solid, DFM checks, instant quotes across CNC / FDM / SLA / LPBF with order placement (RFQs; pluggable supplier API). |
| **CAM** | CNC 2.5D roughing + finishing toolpaths, and **2D laser / plasma / waterjet profile cutting** (kerf-compensated sheet-cut G-code) — both from the same SDF section machinery. |
| **3D printing** | A built-in SDF-native FDM slicer — walls, solid top/bottom skins, infill, skirt, retraction, cooling — producing Klipper/Marlin G-code, with a layer preview and one-click upload to a Moonraker printer. See [docs/PRINTING.md](docs/PRINTING.md). |
| **Machine control** | Moonraker/Klipper status, temperatures, jog, job progress. |
| **Collaboration** | Owner-scoped documents, role-based sharing (viewer/editor), live presence + real-time updates over WebSocket, version snapshots/restore/branch. |
| **Agent platform** | Self-describing operation catalog, API keys, an MCP server, and a discovery manifest at `GET /api/platform`. An agent can go prompt → part → quote → **print** end to end. See [docs/AGENT-PLATFORM.md](docs/AGENT-PLATFORM.md). |
| **Accounts & billing** | JWT auth, plans/entitlements/metering; optional Stripe checkout + webhook for live billing. |

## Quick start

```bash
# backend
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000

# frontend (second terminal)
cd frontend
npm install
npm run dev        # http://localhost:5173 (proxies /api to :8000)
```

Or both at once with `./start.sh`, or `docker-compose up`. No API keys are
required — the rule-based text-to-CAD parser, local quote estimator, and local
plan management all work credential-free. Optional integrations (LLM, Stripe,
quote provider, printers) are enabled by env vars — see
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#configuration).

## The workflow

Sign in → part library → editor. The editor is a single persistent document
with six modes strung on the load-path spine — design it, prove it, prepare the
job, watch it run, then send it out:

1. **Model** — prompt bar, tool ribbon (Sketch | Create | Forms | Modify),
   feature tree, Copilot.
2. **Optimize** — generative topology optimization on the current body.
3. **Analysis** — structural FEA estimate: stress, deflection, safety margin.
4. **Make** — prepare the job: drawing & release, slice / laser cut / CNC
   toolpaths, DFM, then **nest** copies onto the machine's sheet and **post**
   the program to its dialect.
5. **Validate** — replay that job as a build simulation: an FDM print builds
   layer by layer, a laser traces its cut paths, on the selected machine.
6. **Machine** — pick the real machine (Ender 3, TCI DynamicLine 1530,
   Gasparini XP165 …) and either **send it out for a quote** or **run it** —
   live over Moonraker, or exported as a posted job for the control.

Assemblies compose part documents with placement transforms.

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — system map, kernel, data model, configuration.
- [docs/AGENT-PLATFORM.md](docs/AGENT-PLATFORM.md) — build agents against the API/MCP surface.
- [docs/PRINTING.md](docs/PRINTING.md) — the slicer and print pipeline.
- [DEPLOY.md](DEPLOY.md) — production deployment (single Docker image + Postgres).
- [PRODUCT-ROADMAP.md](PRODUCT-ROADMAP.md) — market read and shipped roadmap.

## Development

```bash
cd backend && pytest            # backend suite
cd frontend && npm run build    # type gate (tsc) + bundle
cd frontend && npm run lint     # oxlint
```

Backend layering is `routers/` → `services/` → `cad_kernel/`; the frontend talks
only through `/api/*` with types hand-mirrored in `shared/types.ts`.

## License

MIT.

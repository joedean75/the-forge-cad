# The Forge

An agentic, browser-based CAD suite: describe a part in plain language, refine it
parametrically, optimize it generatively, get instant manufacturing quotes, slice
it, and print it on your own machine — with every one of those steps equally
available to AI agents through a first-class API.

**An internal tool**, aimed at RC, drone and robotics work rather than at
competing for CAD seats — see [docs/roadmaps/RC-DRONE-ROBOTICS.md](docs/roadmaps/RC-DRONE-ROBOTICS.md)
for what that re-ranks, and [DEPLOY.md](DEPLOY.md#deploying-it-internally-the-posture-this-is-actually-for)
for what it means when you stand one up (starting with: the default plan is
`free`, and you almost certainly do not want it).

The geometry engine is real, and there are two of them. An **exact B-rep kernel**
on OpenCascade gives faces and edges you can select, exact dimensions and
analytic STEP; a from-scratch **SDF kernel** (signed distance fields meshed via
marching cubes, native Rust sampler on the hot path) handles what implicit
geometry does better — voxel density fields from the optimizer, mesh imports,
smooth blends. The kernel tries exact first and records which one ran. Alongside
them: a real **3D SIMP topology optimizer** and a real **linear-static FEA
solve** (numpy/scipy, GPU density ops via PyTorch when CUDA is present).

## What it does

| Area | What you get |
|---|---|
| **Text-to-CAD** | A prompt bar that turns "a 100×50×30 mm bracket with two mounting holes" into an editable feature tree, with validation + self-repair. Requires an LLM key — there is no rule-based fallback, because a parser that silently substitutes a plain box for the part you described is worse than one that says it can't help. The credential-free path to a part is the starter template gallery (operation sequences, not prompts). |
| **Image-to-CAD** | Hand it a sketch, logo, drawing or photo. A vision model builds the tree when one is configured; otherwise the silhouette is **traced** into an extruded profile — holes included, exact and instant, no credentials. Either way you get an editable feature tree, not an opaque mesh. |
| **Forge Copilot** | An agentic loop that plans and executes multi-step edits ("shell it to 2 mm and fillet the top edges") through the same operation API agents use — every step visible and undoable. |
| **Parametric modeling** | Feature tree (box/cylinder/sphere/cone, booleans, fillet/chamfer/shell/hole/pattern/transform, constrained sketches, **revolve/mirror/sweep/loft**, plus real **edge fillets and chamfers on selected edges** and shells that open named faces) with a CAD-standard tool ribbon, driving parameters, configurations, dimensions, and version history. |
| **Sheet metal** | Base flange + edge flanges with real bend geometry (inside radius, angle, up or down), and **flat-pattern unfold** — developed blank, K-factor bend allowances and bend lines. Exports DXF (outline on `CUT`, bends on `BEND`) and the cutting pipeline cuts the *blank*, holes included. |
| **Materials** | 43 grades in 12 browsable categories, tagged maker or professional — filaments and resins through to tool steel, titanium, PEEK and composites — driving mass, simulation, DFM and quotes. |
| **Generative design** | SIMP topology optimization scoped to *your part* — the result folds back into the tree as an `OptimizeFeature` and stays editable. Flag features **must-keep** so the solve can't delete a mounting boss it thinks is unloaded, and anything it does eat through is reported instead of quietly missing. |
| **Universal import** | **STEP / IGES arrive exact** — addressable faces and edges, exact measurement, editable re-export; units come from the file header, not a size guess. Meshes (STL / OBJ / PLY / GLB / OFF) are voxelized instead, because an STL has nothing better to recover. |
| **Validate & Make** | Linear-static **FEA** (hex elements, von Mises stress, displacement, safety factor), **DFM per process** — additive, milling, molding, casting — with wall thickness, draft, undercuts and tool access measured on the exact body, plus **design specs** that score the part against stated requirements. Instant quotes across CNC / FDM / SLA / LPBF with order placement. |
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
required to run the app — parametric modeling, the starter templates, local
quote estimator, and local plan management all work credential-free. Text-to-CAD
and text-to-assembly need an LLM key (no rule-based fallback). Optional
integrations (LLM, Stripe, quote provider, printers) are enabled by env vars —
see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#configuration).

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

Copilot remembers conversations per account and per part or assembly in this
browser. Use the **Chat** picker to return to a conversation, or **New chat** to
start fresh. Every follow-up includes that chat's earlier goals, outcomes and
operation details, so “continue” can pick up after a step-budget stop. Long
histories are abbreviated to keep context bounded. Reference image pixels are
not saved in chat history; reattach images when a follow-up needs them.

## Documentation

**Start here**

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — how the system fits together: the
  two kernels, documents and operations, storage, configuration.
- [CLAUDE.md](CLAUDE.md) — the working reference. Longer and more opinionated than
  the others: what each part does, and *why it is that way* rather than the
  obvious alternative. Authoritative when docs disagree.

**By task**

- [docs/AGENT-PLATFORM.md](docs/AGENT-PLATFORM.md) — driving Forge from an agent:
  the operation API, face/edge selection, verification, MCP, rollouts.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — the two processes and the layering.
- [docs/PRINTING.md](docs/PRINTING.md) — the slicer and print pipeline.
- [docs/PURCHASED-PARTS-SOURCING.md](docs/PURCHASED-PARTS-SOURCING.md) — where
  purchased-part CAD may and may not come from, with the terms that decide it.
- [DEPLOY.md](DEPLOY.md) — production deployment (single Docker image + Postgres).

**Where it's going, and where it has been**

- [PRODUCT-ROADMAP.md](PRODUCT-ROADMAP.md) — market read and the three waves of work.
- [docs/roadmaps/](docs/roadmaps/) — six completed programmes, kept for the
  reasoning and the measurements rather than as outstanding work.

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

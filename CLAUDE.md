# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A browser-based replica of the Forge (The Forge) generative-design workflow. The UI is a presentation-layer mockup, but the backend runs a **real 3D SIMP topology optimizer** and a **from-scratch SDF-based CAD kernel** — those are the parts with real substance, not stubs.

## Commands

Backend (run from `backend/`):
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000    # dev server
pytest                                          # all tests
pytest tests/test_cad_kernel.py::test_name      # single test
```

Frontend (run from `frontend/`):
```bash
npm install
npm run dev        # Vite dev server on :5173, proxies /api -> :8000
npm run build      # tsc -b && vite build  (type-check is part of the build)
npm run lint       # oxlint
```

Both at once: `./start.sh` (expects the backend venv to already exist). Or `docker-compose up`.

There is no frontend test suite; `npm run build` is the type-check gate.

## Architecture

**Two processes, one API contract.** React/Vite frontend talks to a FastAPI backend only through `/api/*`. In dev, Vite proxies `/api` to `:8000` (`frontend/vite.config.ts`). In production, FastAPI mounts the built `frontend/dist` at `/` (see the `StaticFiles` mount at the bottom of `backend/main.py`), so the whole app ships from one URL.

**Shared types are duplicated, not imported.** `shared/types.ts` (consumed by the frontend via relative import `../../../shared/types`) mirrors the Pydantic models in `backend/app/models/schemas.py`. These two must be kept in sync **by hand** — changing a response shape means editing both.

**The frontend is a screen router around one persistent document.** `useNavStore` moves between `signin → library → editor/assembly` (hash-synced). Inside the editor, `useDocumentStore` holds the open `DesignDocument` and the six modes on the load-path spine — Model, Optimize, Analysis, Make, Validate, Machine — each a panel under `frontend/src/features/*`. `useAuthStore` and `useAssemblyStore` cover sessions and assemblies. All server calls funnel through the typed `api` object in `frontend/src/api/client.ts` — add new endpoints there, not with ad-hoc `fetch`.

The older linear 7-step flow (`requirements → generative → topology → …`), its `useAppStore`, and the candidate-based backend it talked to (`routers/cad|export|optimize|simulate|manufacturing|text_to_cad|design|assembly`, `services/mesh_generator|assembly_generator|design_intent`) were **removed** — every capability now lives on the document API. Don't reintroduce a `Candidate`; the durable unit is a `DesignDocument`.

**Backend layering:** `routers/` (HTTP surface) → `services/` (orchestration/business logic) → `cad_kernel/` (pure geometry engine). Keep geometry math in `cad_kernel`; keep request handling thin.

**The operation catalog is an action space, not just an API.** It is meant to be driven by a model, so it is built to be *machine-checkable* rather than merely self-describing: every operation publishes strict JSON Schema (`GET /api/operations`, or `GET /api/operations/tools` in function-calling form), arguments are validated before a handler runs, failures carry a stable code, and every result reports what changed. The pieces are `services/op_schema.py` (schema + validation), `op_errors.py` (typed failures), `observation.py` (the compact state an agent sees, and the delta between two of them), `geometry_check.py` (is it a valid solid), `trajectories.py` (recording what was done), and `decompiler.py` (recovering an op sequence from a tree). If you are adding to the surface, the test to apply is: could a model tell from the response alone whether it succeeded, why not, and what changed?

**The CAD kernel** (`backend/app/cad_kernel/`) is self-contained: a `FeatureTree` of primitives (`ops/primitives.py`) and booleans (`ops/booleans.py`) is evaluated as a signed distance field (`evaluator/sdf.py`), meshed via marching cubes (`geom/meshing.py`), and exported to STL/STEP/DXF (`io/`). `kernel.py` is the public facade.

**Adding a feature type** takes two edits, both data: define the class in `ops/` under `@register_feature`, and add one line to `_SDF_BUILDERS` in `evaluator/sdf.py`. `tests/test_registries.py` fails immediately if you do only one. There is no import list or `isinstance` chain to keep in step any more.

**Adding an operation** takes one edit: decorate the handler with `@operation(name, kind, description, params)` in `services/cad_operations.py`. The catalog, the REST surface, the MCP server and the UI all read from that registry. Mark an argument the handler cannot work without as `{"required": True}` — `services/op_schema.py` turns the block into strict JSON Schema and validates against it *before* the handler runs, so a malformed call is refused rather than half-applying an edit. Fetch an argument with `op_errors.require(args, "name")`, never `args["name"]`: the bracket form raises `KeyError`, which used to surface to the caller as `404 Not found: 'sketch'`.

**Two ways to run an operation.** `dispatch()` raises a typed `OperationError`; `execute()` never raises and returns the full envelope — `ok`, a typed `error`, a geometry `delta`, the compact `state`, and `warnings`. Anything that needs to *observe* a failure rather than propagate it (an agent loop, a training rollout, the HTTP layer) uses `execute`. `ok` is computed: it was a hardcoded `True` with a single construction site until the envelope landed.

**Failures carry a code, not just prose.** `services/op_errors.py`: `MISSING_ARG`, `BAD_ARG`, `NO_SUCH_DOCUMENT`, `NO_SUCH_FEATURE`, `PRECONDITION`, `EVAL_FAILED`, `UNKNOWN_OP`, `INTERNAL`. They subclass `ValueError`, so the routers' existing `except ValueError` handlers keep working. Pick `PRECONDITION` when the fix is a *different previous step* (a flat pattern on a part with no sheet-metal feature), not different arguments.

**A tree that does not evaluate is a warning, not a failure.** Binding a feature to a parameter expression before defining the parameter is a supported workflow, and `derived.error` exists precisely so a malformed edit never wedges a document. `execute` reports it in `warnings` and `state.error`. `rollback_on_eval_error=True` opts into the stricter contract an RL environment may want; it is off by default because it breaks that workflow.

**Feature ids are deterministic.** `feature_ops.IdAllocator` assigns the lowest unused `f<n>` for the tree in hand, so replaying an operation sequence reproduces the document byte for byte — which is what makes trajectory replay and the decompiler's verification possible. Never mint an id from the clock or a uuid; legacy timestamp/uuid ids are kept in the allocator's used-set so nothing collides.

**Every operation is recorded when a session is open.** `services/trajectories.py` hooks `execute()` and writes `(goal, obs_before, op, args, result, delta, obs_after)` to a `trajectory` row. Opt-in via `trajectories.recording(doc_id, goal=…)`, so the normal edit path costs one ContextVar read. `services/decompiler.py` goes the other way: it walks any feature tree, emits the op sequence that would build it, replays it and compares mesh hashes — only an exact match is kept, because a demonstration that rebuilds the wrong part teaches the wrong thing.

**Verifying geometry** is `services/geometry_check.py` (watertight, winding, volume, connected bodies — via `trimesh`, which was a dependency long before anything used it for this) and `drawing.check_drawing`. Both return `DfmCheck` rows so they render and score like the manufacturability checks. `simulate` is deliberately *not* an operation: `_approximate_simulation` is closed-form scalars, not analysis, and exposing it as a verifiable signal would teach a model to optimise a meaningless number.

**Walking a serialized tree** goes through `core/feature_tree`: `node_children`, `walk_nodes`, `find_node_by_type`, `root_of`. Children live under `params.children` — every hand-rolled walk that reached for a top-level `children` key has been a bug.

**Generative optimization is async + in-memory.** `POST /api/documents/{id}/optimize` spawns a FastAPI `BackgroundTask` and returns a job id; the frontend polls `/api/documents/{id}/optimize/{job_id}`. Jobs live in a bounded process-local dict in `routers/documents.py`. The actual work is `services/simp_optimizer.py` — a real SIMP compliance-minimization solve on a hexahedral voxel mesh (density ops use PyTorch/CUDA when available; the sparse linear solve uses scipy). The result folds back into the tree as an `OptimizeFeature` and stays editable.

**Text-to-CAD has two engines.** `services/text_to_cad_engine.generate_part_tree` (reached from `document_service.create_document(prompt=…)`) produces the feature tree. Geometry comes from a rule-based, credential-free parser (`services/text_to_cad_geometry.py`) by default; an **optional** OpenAI-style LLM client (`services/llm_client.py`) is used only when configured. Text-to-assembly is `services/text_to_assembly.py`, reached from `/api/assemblies/from-prompt`. Anything touching this must degrade gracefully when no LLM key is set.

**No canned shapes in the parser.** It reads dimensions, never subject nouns. A hardcoded "RC chassis" used to short-circuit any prompt containing `chassis`/`rc`/`rally`/`buggy` and return a fixed plate — and because `"rc" in prompt` is a substring test, *circle*, *arch*, *torch* and *force* all triggered it too. Adding a shape shortcut keyed on a word is how that happens; parse the numbers instead.

**Image-to-CAD mirrors it.** `services/image_to_cad.py`: a vision LLM builds the tree when configured, otherwise `trace_to_tree` thresholds the image (alpha channel wins when present), contours the silhouette, and extrudes it with interior contours subtracted as holes. Output is an ordinary editable tree — deliberately *not* an `ImportFeature`, which is an opaque voxel body. It never infers depth from a single view; thickness is a parameter.

**Materials are one catalog.** `services/materials.py` is the single source (43 grades across 12 categories, each tagged `tier: maker|professional`). Properties are SI throughout; `to_spec_material()` converts to the MPa/GPa view the solver schema wants — never hand-convert at a call site. Material ids are persisted on documents, so **existing ids are never renamed**.

**Sheet metal is modelled as bends, not as a generic solid.** `cad_kernel/ops/sheet_metal.py` holds a base flange plus edge flanges (inside radius + angle), evaluated by `SheetMetalSDF`. That structure is what makes `flat_pattern()` computable — the developed blank needs bend geometry that can't be recovered from an arbitrary body. Bend allowance uses the K-factor convention. Anything that *cuts* a sheet-metal part must first call `sheet_metal.unfold_tree` (profile-cut and nesting do): sectioning the folded body traces a silhouette that doesn't fold back into the part.

**Drawings: the kernel projects, the caller decides.** `services/drawing.py` takes a `DrawingSpec` — which of the six orthographic views, which section planes and where they cut, what to dimension, what to call out, projection angle, sheet, scale. Projection, section-cutting and hidden-line extraction are pure functions of the body; everything else is a drafting decision and belongs to whoever is driving. It used to be inverted: hardcoded front/top/right, a section pinned to mid-Y, and one boolean for the caller.

Three things there are easy to undo by accident:

- **Outlines contour the signed field, never a boolean occupancy mask.** A mask pins the outline to cell boundaries, and where a sample lands exactly on the surface `sdf <= 0` counts it inside and the silhouette gains half a cell — a 35 mm plate-and-boss drew as 35.33 mm.
- **Printed dimension figures come from `derived.bbox`**, not from measuring the traced silhouette, so a drawing and `bounding_box` cannot disagree about the part's size. Dimension endpoints are given in model mm on the view's own two axes, so the figure stays true at any scale.
- **Hidden lines come from `_occluded`**: along each ray, the largest field value between where it enters and leaves the body. Solid all the way through and that stays negative; pass through a void and it goes positive. The `min_gap` floor is load-bearing — without it a ray grazing the surface reads its own boundary sample as a cavity and the view fills with spurious dashes.

**Optimization can be told what not to delete.** SIMP removes any material that isn't carrying load, which routinely eats mounting bosses and bolt-hole walls. `feature_ops.set_keep` flags a feature; `run_simp_from_solid` rasterizes those features onto the element grid and pins them solid (exact geometry, not the sphere `keep_in` regions use, and *not* also a fixed support). Every leaf feature is then audited after the solve, and anything left under `_FEATURE_INTACT_FRACTION` is reported in `info["removed_features"]` — surfaced on the job and persisted on the `OptimizeFeature`, so a part never comes back quietly missing something.

**State & persistence.** The durable source of truth is a **SQLAlchemy datastore** (`app/db.py`): accounts, documents, candidates and assemblies persist as transactional rows via `services/persistence.py` (same function signatures as before — the file-JSON backing was swapped out underneath). Dev/tests use embedded **SQLite** (WAL mode) at `backend/data/forge.db`; set `DATABASE_URL` to a `postgresql+psycopg://…` URL for production (MVCC concurrent writers, pooling, TLS, horizontal scale) — no code change. On startup `main.py` calls `init_db()` + `migrate_json_files()` (one-time lossless import of any legacy `backend/data/*/*.json`). Purely in-memory (rebuilt/lazily re-derived, lost on restart): the optimize job dicts (`_jobs`/`_results`), the evaluated-solid cache `_SOLID_CACHE`, and the `CANDIDATE_DATA`/`ASSEMBLY_DATA` working caches; on startup only candidate/assembly **metadata** is restored and solids are re-evaluated lazily. Tests run against an isolated temp DB (`tests/conftest.py` sets `DATABASE_URL` before app import).

The datastore is one table — `records(kind, id, owner_id, data)` — so a new persisted type is a new `kind` and no migration. That is how `trajectory` was added. Filter inside a row with `persistence._rows_by_json_field`, which is dialect-aware (SQLite `json_extract`, Postgres `->>`); loading every row to test one field means deserializing whole feature trees.

**Geometry is only comparable when the conditions match.** `DerivedCache` records `resolution`, `sampler` (the Rust and numpy field evaluators are picked by a `try: import` and are not bit-identical) and `mesh_hash`. `resolution` is a *total voxel budget*, not per-axis, and `capped_resolution` clamps it per plan — so the same document yields different geometry depending on who asks. Pin it for anything that compares meshes across runs.

## LLM configuration

Optional, opt-in via `backend/.env`: `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL` (provider-agnostic OpenAI-style chat protocol; defaults target Moonshot/Kimi, also works with OpenRouter). With no key, the rule-based parser handles everything.

## Subscription & billing configuration

Accounts/plans/entitlements always run; `services/billing.py` is a pluggable payment provider following the same opt-in pattern. With **no** `STRIPE_API_KEY`, plan changes apply immediately (local self-serve) so dev needs no payment credentials. Set these in `backend/.env` to go live: `STRIPE_API_KEY` (secret key), `STRIPE_WEBHOOK_SECRET` (`whsec_…`, required to accept webhooks), `STRIPE_PRICE_PRO` / `STRIPE_PRICE_ENTERPRISE` (recurring price ids), `PUBLIC_BASE_URL` (post-checkout redirect base). In live mode a paid upgrade returns a Stripe Checkout URL and the plan is granted **only** by the signature-verified webhook at `POST /api/auth/webhook/stripe` — never trusted from the browser redirect. Stripe calls go through `httpx` (no `stripe` SDK). `FORGE_AUTH_REQUIRED=1` flips the whole API from dev-account fallback to enforced auth. Documents are owner-scoped and isolated per account (see `document_service.can_access`).

## gstack skills

This environment has [gstack](https://github.com/garrytan/gstack) installed (Garry Tan's Claude Code setup) — a suite of slash-command "skills" that act as specialist roles across the dev lifecycle. Invoke `/gstack` to route a request, or call a skill directly. Full reference: `~/.claude/skills/gstack/docs/skills.md`.

Lifecycle order: `office-hours → spec → autoplan → implement → review → qa → ship → land-and-deploy → retro`.

**Plan & product**
- `/office-hours` — YC-style reframe; start here on a new idea. `/spec` — turn vague intent into an executable spec.
- `/plan-ceo-review` — find the 10-star product in the request. `/plan-eng-review` — lock architecture/data-flow/tests (the only required review gate). `/plan-design-review` — audit the plan's UX before code. `/plan-devex-review` — DX/time-to-hello-world. `/autoplan` — run CEO→design→eng→DX automatically.

**Design**
- `/design-consultation` — build a design system from scratch (writes `DESIGN.md`). `/design-shotgun` — generate & compare visual variants. `/design-html` — mockup → production HTML. `/design-review` — live-site visual audit + fix loop.

**Build, review, QA**
- `/review` — production-bug hunt (auto-fixes obvious ones). `/investigate` — root-cause debugging (no fix without investigation). `/codex` — independent second opinion from OpenAI Codex. `/qa` / `/qa-only` — browser-driven testing (diff-aware). `/browse` — real Chromium eyes for the agent. `/health` — type/lint/test/dead-code dashboard.

**Ship & operate**
- `/ship` — sync, test, coverage audit, push, open PR. `/land-and-deploy` — merge → deploy → verify prod (`/setup-deploy` first). `/canary` — post-deploy monitoring. `/benchmark` — perf baselines. `/cso` — OWASP+STRIDE security audit. `/document-release` / `/document-generate` — keep docs in sync / generate Diataxis docs. `/retro` — team-aware retrospective.

**Safety & utility**
- `/careful` (warn on destructive commands), `/freeze` (lock edits to one dir), `/guard` (both), `/unfreeze`. `/make-pdf`, `/diagram`, `/learn` (session memory), `/context-save` + `/context-restore`, `/gstack-upgrade`.

Note: gstack stores its own per-project state under `~/.gstack/projects/`, separate from this repo.

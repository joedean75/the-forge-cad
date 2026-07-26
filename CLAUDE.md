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

**The CAD kernel** (`backend/app/cad_kernel/`) is self-contained: a `FeatureTree` of primitives (`ops/primitives.py`) and booleans (`ops/booleans.py`) is evaluated as a signed distance field (`evaluator/sdf.py`), meshed via marching cubes (`geom/meshing.py`), and exported to STL/STEP (`io/`). `kernel.py` is the public facade.

**Generative optimization is async + in-memory.** `POST /api/documents/{id}/optimize` spawns a FastAPI `BackgroundTask` and returns a job id; the frontend polls `/api/documents/{id}/optimize/{job_id}`. Jobs live in a bounded process-local dict in `routers/documents.py`. The actual work is `services/simp_optimizer.py` — a real SIMP compliance-minimization solve on a hexahedral voxel mesh (density ops use PyTorch/CUDA when available; the sparse linear solve uses scipy). The result folds back into the tree as an `OptimizeFeature` and stays editable.

**Text-to-CAD has two engines.** `services/text_to_cad_engine.generate_part_tree` (reached from `document_service.create_document(prompt=…)`) produces the feature tree. Geometry comes from a rule-based, credential-free parser (`services/text_to_cad_geometry.py`) by default; an **optional** OpenAI-style LLM client (`services/llm_client.py`) is used only when configured. Text-to-assembly is `services/text_to_assembly.py`, reached from `/api/assemblies/from-prompt`. Anything touching this must degrade gracefully when no LLM key is set.

**Materials are one catalog.** `services/materials.py` is the single source (43 grades across 12 categories, each tagged `tier: maker|professional`). Properties are SI throughout; `to_spec_material()` converts to the MPa/GPa view the solver schema wants — never hand-convert at a call site. Material ids are persisted on documents, so **existing ids are never renamed**.

**Sheet metal is modelled as bends, not as a generic solid.** `cad_kernel/ops/sheet_metal.py` holds a base flange plus edge flanges (inside radius + angle), evaluated by `SheetMetalSDF`. That structure is what makes `flat_pattern()` computable — the developed blank needs bend geometry that can't be recovered from an arbitrary body. Bend allowance uses the K-factor convention.

**State & persistence.** The durable source of truth is a **SQLAlchemy datastore** (`app/db.py`): accounts, documents, candidates and assemblies persist as transactional rows via `services/persistence.py` (same function signatures as before — the file-JSON backing was swapped out underneath). Dev/tests use embedded **SQLite** (WAL mode) at `backend/data/forge.db`; set `DATABASE_URL` to a `postgresql+psycopg://…` URL for production (MVCC concurrent writers, pooling, TLS, horizontal scale) — no code change. On startup `main.py` calls `init_db()` + `migrate_json_files()` (one-time lossless import of any legacy `backend/data/*/*.json`). Purely in-memory (rebuilt/lazily re-derived, lost on restart): the optimize job dicts (`_jobs`/`_results`), the evaluated-solid cache `_SOLID_CACHE`, and the `CANDIDATE_DATA`/`ASSEMBLY_DATA` working caches; on startup only candidate/assembly **metadata** is restored and solids are re-evaluated lazily. Tests run against an isolated temp DB (`tests/conftest.py` sets `DATABASE_URL` before app import).

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

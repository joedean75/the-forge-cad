# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A browser-based replica of the Forge (The Forge) generative-design workflow. The UI is a presentation-layer mockup, but the backend runs a **real 3D SIMP topology optimizer** and a **from-scratch SDF-based CAD kernel** — those are the parts with real substance, not stubs.

The operation catalog is also intended as the **action space for an LLM agent**, and increasingly as something a model can be fine-tuned against: typed and validated arguments, results that say what changed, verifiers that can score a part, and recorded trajectories. Changes to that surface should keep it machine-checkable — see the note in Architecture.

**Two kernels, and which one runs matters.** The SDF kernel is the original: robust booleans that never fail, and the only thing that can evaluate a voxel density field from the optimizer or an imported mesh. **OpenCascade now runs alongside it** (`cad_kernel/evaluator/brep.py`) and is tried *first*, because it is the one that gives geometry an identity — addressable faces and edges, exact dimensions, and real STEP. `Kernel.evaluate_feature` tries B-rep → native Rust SDF → numpy SDF, and `derived.sampler` records which ran.

`brep_from_feature` returns `None` for anything it cannot represent faithfully, and one such feature disqualifies the **whole tree** — half a body built exactly and half approximated is worse than either. What it declines, and why: `FilletFeature`/`ChamferFeature` are smooth booleans *between bodies*, not edge treatments; `ShellFeature` is `abs(d) - t/2`, which hollows uniformly and cannot open a face; `OptimizeFeature`/`ImportFeature` are cached voxel grids. `LoftFeature` is declined for a different reason worth understanding: `LoftSDF` blends the two profiles' *distance values*, which is not a ruled surface, so an OCC loft would be a genuinely **different shape** — mapping it would silently change what an existing document means depending on whether OCC is installed. The true loft is therefore `RuledLoftFeature` (`add_ruled_loft`), a second feature rather than a swap behind the same name. It is exact enough that a circle-to-circle loft matches the conical-frustum formula to 1e-6 relative and a square-to-square one matches the pyramid frustum, which is how you know the surface is genuinely ruled; `ruled=False` asks `ThruSections` for the smooth variant instead.

Revolve, sweep and sheet metal **are** mapped, each matched to the field semantics rather than to the obvious OCC call: a partial revolve sweeps symmetrically (−angle/2..+angle/2, not 0..angle), and a sweep is built from cylinders and spheres because `SweepSDF` is a union of *capsules* — `BRepOffsetAPI_MakePipe` would give flat ends and mitred joints, a different body at every corner. `SheetMetalFeature` rebuilds `SheetMetalSDF`'s own construction: an annular sector welded to the plate edge plus a leg tangent to its end, extruded across the part. Anything else — a fillet on the corner, say — would be a body `flat_pattern` does not unfold. The two kernels agree to 0.25% across five bend configurations, which is 200³ voxels on a 1.5 mm sheet.

The real versions of the first two are separate features (`ops/exact.py`): `EdgeFilletFeature`, `EdgeChamferFeature`, `FaceShellFeature`, driven by `fillet_edges` / `chamfer_edges` / `shell_faces`, plus `BrepBodyFeature` — an imported STEP/IGES solid carried as deflated OCC BRep, deserialized on every evaluation. These exist **only** on the exact kernel — their SDF builders raise rather than approximate, and `Kernel.evaluate_feature` rebuilds them strictly rather than falling back, so a failure says "OCC could not build a 500 mm fillet on those edges" instead of a generic "needs the exact kernel".

OCP is an **optional** dependency (~143 MB). Without it everything still works through the SDF, but no face or edge is addressable and STEP export is faceted.

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

**Driving it headlessly** (rollouts, dataset generation): `tests/conftest.py` is the working bootstrap — temp SQLite via `DATABASE_URL`, empty `FORGE_DATA_DIR`, `FORGE_RATE_LIMIT=0`, and `FORGE_AUTH_REQUIRED` left unset so every request is attributed to a shared enterprise dev account. Call `cad_operations.execute` directly to skip HTTP, auth and quota metering entirely. Over HTTP, `POST /api/documents/{id}/op?envelope=1` returns failures as 200 bodies instead of raising.

Two things used to bite at volume and now have a switch: `services/rollout.py`. `with rollout.rollout(resolution=192):` turns metering off and pins resolution, or `FORGE_ROLLOUT=1` (+ `FORGE_ROLLOUT_RESOLUTION`) sets it process-wide. The quota one is obvious — a few hundred thousand edits exhausts even the enterprise allowance and the failure arrives as a 402 mid-rollout. **Pinning matters more**: `capped_resolution` clamps to the account's plan, so the same document evaluates differently for different accounts, which destroys the one property a dataset needs — that a recorded observation can be reproduced. Both are opt-in and scoped to a block; neither touches `can_access`, which is the security boundary rather than a billing counter.

## Architecture

**Two processes, one API contract.** React/Vite frontend talks to a FastAPI backend only through `/api/*`. In dev, Vite proxies `/api` to `:8000` (`frontend/vite.config.ts`). In production, FastAPI mounts the built `frontend/dist` at `/` (see the `StaticFiles` mount at the bottom of `backend/main.py`), so the whole app ships from one URL.

**Shared types are duplicated, not imported.** `shared/types.ts` (consumed by the frontend via relative import `../../../shared/types`) mirrors the Pydantic models in `backend/app/models/schemas.py`. These two must be kept in sync **by hand** — changing a response shape means editing both.

**The frontend is a screen router around one persistent document.** `useNavStore` moves between `signin → library → editor/assembly` (hash-synced). Inside the editor, `useDocumentStore` holds the open `DesignDocument` and the six modes on the load-path spine — Model, Optimize, Analysis, Make, Validate, Machine — each a panel under `frontend/src/features/*`. `useAuthStore` and `useAssemblyStore` cover sessions and assemblies. All server calls funnel through the typed `api` object in `frontend/src/api/client.ts` — add new endpoints there, not with ad-hoc `fetch`.

The older linear 7-step flow (`requirements → generative → topology → …`), its `useAppStore`, and the candidate-based backend it talked to (`routers/cad|export|optimize|simulate|manufacturing|text_to_cad|design|assembly`, `services/mesh_generator|assembly_generator|design_intent`) were **removed** — every capability now lives on the document API. Don't reintroduce a `Candidate`; the durable unit is a `DesignDocument`.

**Backend layering:** `routers/` (HTTP surface) → `services/` (orchestration/business logic) → `cad_kernel/` (pure geometry engine). Keep geometry math in `cad_kernel`; keep request handling thin.

**The operation catalog is an action space, not just an API.** It is meant to be driven by a model, so it is built to be *machine-checkable* rather than merely self-describing: every operation publishes strict JSON Schema (`GET /api/operations`, or `GET /api/operations/tools` in function-calling form), arguments are validated before a handler runs, failures carry a stable code, and every result reports what changed. The pieces are `services/op_schema.py` (schema + validation), `op_errors.py` (typed failures), `observation.py` (the compact state an agent sees, and the delta between two of them), `geometry_check.py` (is it a valid solid), `trajectories.py` (recording what was done), and `decompiler.py` (recovering an op sequence from a tree). If you are adding to the surface, the test to apply is: could a model tell from the response alone whether it succeeded, why not, and what changed?

**Topological naming** (`cad_kernel/core/naming.py`) is what makes selection survive a rebuild. OCC's `TopoDS_Face` identity does not — change a dimension and the "same" face is a different object in a different exploration order. Entities are named by *what they are*: kind, size, and position **normalised to the body's bounding box**. That last part is load-bearing. Stretch a plate from 80 to 120 mm and its corner edges move 20 mm absolute, which no sane tolerance accepts; normalised they have not moved at all, so a fillet placed on them stays put. A reference that cannot be matched is reported unresolvable rather than snapped to whatever was nearest. `services/datum.py`'s symbolic `{feature_id, face}` scheme is the SDF-side equivalent and still applies when the body is not exact.

**The CAD kernel** (`backend/app/cad_kernel/`) is self-contained: a `FeatureTree` of primitives (`ops/primitives.py`) and booleans (`ops/booleans.py`) is evaluated as a signed distance field (`evaluator/sdf.py`), meshed via marching cubes (`geom/meshing.py`), and exported to STL/STEP/DXF (`io/`). `kernel.py` is the public facade.

**Adding a feature type** takes two edits, both data: define the class in `ops/` under `@register_feature`, and add one line to `_SDF_BUILDERS` in `evaluator/sdf.py`. `tests/test_registries.py` fails immediately if you do only one. There is no import list or `isinstance` chain to keep in step any more.

**Adding an operation** takes one edit: decorate the handler with `@operation(name, kind, description, params)` in `services/cad_operations.py`. The catalog, the REST surface, the MCP server and the UI all read from that registry. Mark an argument the handler cannot work without as `{"required": True}` — `services/op_schema.py` turns the block into strict JSON Schema and validates against it *before* the handler runs, so a malformed call is refused rather than half-applying an edit. Fetch an argument with `op_errors.require(args, "name")`, never `args["name"]`: the bracket form raises `KeyError`, which used to surface to the caller as `404 Not found: 'sketch'`.

**Two ways to run an operation.** `dispatch()` raises a typed `OperationError`; `execute()` never raises and returns the full envelope — `ok`, a typed `error`, a geometry `delta`, the compact `state`, and `warnings`. Anything that needs to *observe* a failure rather than propagate it (an agent loop, a training rollout, the HTTP layer) uses `execute`. `ok` is computed: it was a hardcoded `True` with a single construction site until the envelope landed.

**Failures carry a code, not just prose.** `services/op_errors.py`: `MISSING_ARG`, `BAD_ARG`, `NO_SUCH_DOCUMENT`, `NO_SUCH_FEATURE`, `PRECONDITION`, `EVAL_FAILED`, `UNKNOWN_OP`, `INTERNAL`. They subclass `ValueError`, so the routers' existing `except ValueError` handlers keep working. Pick `PRECONDITION` when the fix is a *different previous step* (a flat pattern on a part with no sheet-metal feature), not different arguments.

**`add_modifier("fillet"|"chamfer")` is an edge treatment, not a smooth blend.** It used to build a `FilletFeature` and wrap the root in it — leaving that feature with exactly one child, and a smooth union of one body is that body, so `resolve_suppressed`'s single-child collapse deleted it before evaluation. The op returned `ok`, a feature id and an unchanged volume; a no-op that reports success is the worst thing this action space can contain. It now builds an `EdgeFilletFeature`/`EdgeChamferFeature` over every edge (`_blend_whole_body`), and without the exact kernel says so as a `PRECONDITION`. `fillet_edges` / `chamfer_edges` / `shell_faces` accept `"all"` in place of an id list for the same reason. Nothing in the catalog constructs a multi-body smooth blend any more, so `decompiler` steps through an inert single-child one and declines a real one.

**The exact edge treatments roll back rather than warn.** `_apply_exact` is the exception to the rule below: a 500 mm fillet on a 50 mm cube has no later step that rescues it, so the edit is undone and the failure is a typed `PRECONDITION` naming the radius — not an `ok` whose volume happens to be zero.

**A tree that does not evaluate is a warning, not a failure.** Binding a feature to a parameter expression before defining the parameter is a supported workflow, and `derived.error` exists precisely so a malformed edit never wedges a document. `execute` reports it in `warnings` and `state.error`. `rollback_on_eval_error=True` opts into the stricter contract an RL environment may want; it is off by default because it breaks that workflow.

**Feature ids are deterministic.** `feature_ops.IdAllocator` assigns the lowest unused `f<n>` for the tree in hand, so replaying an operation sequence reproduces the document byte for byte — which is what makes trajectory replay and the decompiler's verification possible. Never mint an id from the clock or a uuid; legacy timestamp/uuid ids are kept in the allocator's used-set so nothing collides.

**Every operation is recorded when a session is open.** `services/trajectories.py` hooks `execute()` and writes `(goal, obs_before, op, args, result, delta, obs_after)` to a `trajectory` row. Opt-in via `trajectories.recording(doc_id, goal=…)`, so the normal edit path costs one ContextVar read. `services/decompiler.py` goes the other way: it walks any feature tree, emits the op sequence that would build it, replays it and compares mesh hashes — only an exact match is kept, because a demonstration that rebuilds the wrong part teaches the wrong thing.

**A spec says what the part was supposed to be; everything else only says the body is well formed.** `services/design_spec.py` holds measurable requirements on `document.spec`, driven by `set_spec` / `get_spec` / `verify_against_spec`, and returns `DfmCheck` rows plus a `score` — the fraction met, which is the reward function. Two rules keep it honest: **every kind is something the kernel can actually measure** (there is no `looks_like`), and **an unmeasurable requirement fails loudly rather than passing quietly** — an unknown kind, a missing bound, a body the exact kernel cannot address all return `fail` with the reason, because a spec scoring 1.0 because half of it was skipped is the worst thing a reward function can do. `hole` counts *cylindrical faces on the body*, not `HoleFeature` nodes: a bore a later boolean swallowed is not a hole, and one inside an imported STEP solid is.

**DFM is measured on the body, not on the mesh that renders it.** `services/dfm_topology.py` casts rays through the exact B-rep for wall thickness, draft, undercuts and 3-axis tool access, counts inner shells for trapped voids, and reads the smallest bore and shortest edge off the topology. `dfm` takes a `process` (`additive` | `milling` | `molding` | `casting`); the additive report keeps overhang/support/quote — honest tessellation questions — and the non-additive ones are topology only, with **no quote**, because there is no cost model for them and an invented number is a signal a model would learn to game.

The measurements are only worth having because of four details, each of which was wrong first:

- **`feature_size` was never about the part.** It reported the shortest edge of a marching-cubes mesh, so it moved with `resolution`. Where the body is exact, `wall_thickness` **replaces** that row rather than sitting beside it — two disagreeing "minimum feature" numbers is worse than one.
- **Samples sit on the surface, not on the triangulation.** This is the one that matters. A chord across a convex face lies *inside* the material, so every escape ray from it starts buried — the outer wall of a plain tube came back as 50% of the part needing 5-axis, and a measured 0.4 mm wall read 0.34 mm. The triangulation now only decides *where* to sample; the (u,v) centroid is evaluated on the real surface for both the point and the normal. An opposing-normal filter was written first to suppress the symptoms and then removed: across nine shapes it never changed a reading.
- **Escape rays start 10 µm off the face.** A ray running along the surface it leaves is tangent to it, and OCC reports the grazing contact as a hit — which called the wall of every through-bore unreachable.
- **A wall parallel to the tool axis is reachable.** An end mill cuts with its flank, so a through-bore is machined by a cutter going down it. Requiring the face to point at the tool called every drilled hole unreachable.

**Verifying geometry** is `services/geometry_check.py` (watertight, winding, volume, connected bodies — via `trimesh`, which was a dependency long before anything used it for this) and `drawing.check_drawing`. Both return `DfmCheck` rows so they render and score like the manufacturability checks.

**`simulate` is a real solve now, and says so when it isn't.** It was kept out of the catalog while `_approximate_simulation` was all there was — `total_force / volume^(2/3)` clamped to [0.01, 500] MPa, which cannot tell a cube from a cantilever, and a verifiable signal that means nothing is worse than none because a model will optimise it. `services/fea.py` is an 8-node hexahedral linear-static solve reusing `simp_optimizer`'s element stiffness, DOF map and solver — SIMP has been assembling and solving this system all along; what it never needed was **stress**, so the added part is strain and Cauchy stress at each element centroid. The estimate survives as a labelled fallback: `solver` says which ran and `estimate_only` marks it, because the alternative is a meaningless number being scored as a measurement.

Four things there are load-bearing:

- **Loads and supports act over a patch of fixed physical size** (5% of the body diagonal), not one node. A point force on a continuum is a singularity: applied at a node, a 200×20×20 cantilever's tip deflection went 1.40 → 1.72 → 2.11 mm across three refinements, *away* from the 1.0 mm beam formula. Patched, it converges to 0.854 mm — and that figure is right, because the patch clamps ~10 mm of root and 200·0.95³ = 0.857.
- **The safety factor comes from the peak element stress, not the percentile.** `stress_p99` is the stable number and the tempting one to headline, but it sits *below* the real stress, which pushes the factor up. An optimistic safety factor is the one failure mode this must not have; on a voxel mesh the peak overstates, so the factor is conservative.
- **Above 20k free DOFs the direct solve is abandoned for Jacobi-preconditioned CG.** A sparse LU fills in badly in 3D — a 98k-DOF beam ran five minutes and 5 GB before being killed, where CG is seconds and O(n) memory.
- **CalculiX was considered and rejected.** It is an external binary a deployment either has or doesn't, and every optional dependency here (OCP, LLM, Stripe) degrades to something that still works. An analysis that silently disappears in production is worse than one with a documented discretisation caveat.

**Walking a serialized tree** goes through `core/feature_tree`: `node_children`, `walk_nodes`, `find_node_by_type`, `root_of`. Children live under `params.children` — every hand-rolled walk that reached for a top-level `children` key has been a bug.

**Generative optimization is async + in-memory.** `POST /api/documents/{id}/optimize` spawns a FastAPI `BackgroundTask` and returns a job id; the frontend polls `/api/documents/{id}/optimize/{job_id}`. Jobs live in a bounded process-local dict in `routers/documents.py`. The actual work is `services/simp_optimizer.py` — a real SIMP compliance-minimization solve on a hexahedral voxel mesh (density ops use PyTorch/CUDA when available; the sparse linear solve uses scipy). The result folds back into the tree as an `OptimizeFeature` and stays editable.

**Text-to-CAD has two engines.** `services/text_to_cad_engine.generate_part_tree` (reached from `document_service.create_document(prompt=…)`) produces the feature tree. Geometry comes from a rule-based, credential-free parser (`services/text_to_cad_geometry.py`) by default; an **optional** OpenAI-style LLM client (`services/llm_client.py`) is used only when configured. Text-to-assembly is `services/text_to_assembly.py`, reached from `/api/assemblies/from-prompt`. Anything touching this must degrade gracefully when no LLM key is set.

Note this path emits a **whole tree in one shot** — it never touches the operation catalog, so it produces no tool-use data. `services/decompiler.py` is the bridge: it recovers the op sequence for any such tree and verifies it by replay. The document keeps its `source_prompt`, which is what makes the recovered sequence a training example rather than a log.

**No canned shapes in the parser.** It reads dimensions, never subject nouns. A hardcoded "RC chassis" used to short-circuit any prompt containing `chassis`/`rc`/`rally`/`buggy` and return a fixed plate — and because `"rc" in prompt` is a substring test, *circle*, *arch*, *torch* and *force* all triggered it too. Adding a shape shortcut keyed on a word is how that happens; parse the numbers instead.

**Image-to-CAD mirrors it.** `services/image_to_cad.py`: a vision LLM builds the tree when configured, otherwise `trace_to_tree` thresholds the image (alpha channel wins when present), contours the silhouette, and extrudes it with interior contours subtracted as holes. Output is an ordinary editable tree — deliberately *not* an `ImportFeature`, which is an opaque voxel body. It never infers depth from a single view; thickness is a parameter.

**Import has two paths, and which one runs decides what you get.** `services/import_service.py`: **STEP and IGES go through OpenCascade** (`cad_kernel/io/step_import.py`) and stay exact — the body arrives as a `BrepBodyFeature` carrying serialized OCC geometry, so its faces and edges are addressable, `mass_properties` is computed rather than sampled, and it re-exports as an editable solid. Meshes are voxelized into an `ImportFeature`, because an STL has nothing better to recover. STEP falls back to the mesh path when OCC is absent or the file defeats it; IGES has no fallback and says so.

Three things there are load-bearing:

- **Units come from the file's header, not from the mesh path's `extent > 5 m` guess** — which reads a 3 mm cube as a 3 m block. An explicit `units` argument still overrides, reinterpreting the file's raw coordinates.
- **`xstep.cascade.unit` is read through a *cached* global that `STEPControl_Writer` resets to millimetres.** So a read after any export came back 1000× too large with the static still reading `M`. Both sides are now pinned per call: the reader via `STEPControl_Reader.SetSystemLengthUnit`, the writer via `model.SetLocalLengthUnit(1.0)` / `SetWriteLengthUnit(1.0)`.
- **A result that is not a closed solid with positive volume is refused.** OCC will read a file it only half understands and hand back a few loose faces — which looks like a body, measures as nothing, and would displace a voxel import that worked. IGES sewing runs at 10 µm for the same reason: at 1 µm a plate's bore is left unsewn and the shell closes over it, giving a solid plate with no hole.

**Materials are one catalog.** `services/materials.py` is the single source (43 grades across 12 categories, each tagged `tier: maker|professional`). Properties are SI throughout; `to_spec_material()` converts to the MPa/GPa view the solver schema wants — never hand-convert at a call site. Material ids are persisted on documents, so **existing ids are never renamed**.

**Sheet metal is modelled as bends, not as a generic solid.** `cad_kernel/ops/sheet_metal.py` holds a base flange plus edge flanges (inside radius + angle), evaluated by `SheetMetalSDF` or, when OCC is present, built exactly by `brep._sheet_metal`. That structure is what makes `flat_pattern()` computable — the developed blank needs bend geometry that can't be recovered from an arbitrary body. Bend allowance uses the K-factor convention. Anything that *cuts* a sheet-metal part must first call `sheet_metal.unfold_tree` (profile-cut and nesting do): sectioning the folded body traces a silhouette that doesn't fold back into the part.

**Drawings: the kernel projects, the caller decides.** `services/drawing.py` takes a `DrawingSpec` — which of the six orthographic views, which section planes and where they cut, what to dimension, what to call out, projection angle, sheet, scale. Projection, section-cutting and hidden-line extraction are pure functions of the body; everything else is a drafting decision and belongs to whoever is driving. It used to be inverted: hardcoded front/top/right, a section pinned to mid-Y, and one boolean for the caller.

Three things there are easy to undo by accident:

- **A body only the exact kernel can build has no field to project.** `_field` falls back to voxelising its mesh through `GridSDF` — the one place a drawing is quantised. It affects only the traced outline of edge-filleted/chamfered/shelled parts; the printed figures come from `derived.bbox` either way.
- **Outlines contour the signed field, never a boolean occupancy mask.** A mask pins the outline to cell boundaries, and where a sample lands exactly on the surface `sdf <= 0` counts it inside and the silhouette gains half a cell — a 35 mm plate-and-boss drew as 35.33 mm.
- **Printed dimension figures come from `derived.bbox`**, not from measuring the traced silhouette, so a drawing and `bounding_box` cannot disagree about the part's size. Dimension endpoints are given in model mm on the view's own two axes, so the figure stays true at any scale.
- **Hidden lines come from `_occluded`**: along each ray, the largest field value between where it enters and leaves the body. Solid all the way through and that stays negative; pass through a void and it goes positive. The `min_gap` floor is load-bearing — without it a ray grazing the surface reads its own boundary sample as a cavity and the view fills with spurious dashes.

**Optimization can be told what not to delete.** SIMP removes any material that isn't carrying load, which routinely eats mounting bosses and bolt-hole walls. `feature_ops.set_keep` flags a feature; `run_simp_from_solid` rasterizes those features onto the element grid and pins them solid (exact geometry, not the sphere `keep_in` regions use, and *not* also a fixed support). Every leaf feature is then audited after the solve, and anything left under `_FEATURE_INTACT_FRACTION` is reported in `info["removed_features"]` — surfaced on the job and persisted on the `OptimizeFeature`, so a part never comes back quietly missing something.

**State & persistence.** The durable source of truth is a **SQLAlchemy datastore** (`app/db.py`): accounts, documents, candidates and assemblies persist as transactional rows via `services/persistence.py` (same function signatures as before — the file-JSON backing was swapped out underneath). Dev/tests use embedded **SQLite** (WAL mode) at `backend/data/forge.db`; set `DATABASE_URL` to a `postgresql+psycopg://…` URL for production (MVCC concurrent writers, pooling, TLS, horizontal scale) — no code change. On startup `main.py` calls `init_db()` + `migrate_json_files()` (one-time lossless import of any legacy `backend/data/*/*.json`). Purely in-memory (rebuilt/lazily re-derived, lost on restart): the optimize job dicts (`_jobs`/`_results`), the evaluated-solid cache `_SOLID_CACHE`, and the `CANDIDATE_DATA`/`ASSEMBLY_DATA` working caches; on startup only candidate/assembly **metadata** is restored and solids are re-evaluated lazily. Tests run against an isolated temp DB (`tests/conftest.py` sets `DATABASE_URL` before app import).

The datastore is one table — `records(kind, id, owner_id, data)` — so a new persisted type is a new `kind` and no migration. That is how `trajectory` was added. Filter inside a row with `persistence._rows_by_json_field`, which is dialect-aware (SQLite `json_extract`, Postgres `->>`); loading every row to test one field means deserializing whole feature trees.

**Geometry is only comparable when the conditions match.** `DerivedCache` records `resolution`, `sampler` (`brep` | `native` | `numpy`) and `mesh_hash`. On the SDF paths `resolution` is a *total voxel budget*, not per-axis, and `capped_resolution` clamps it per plan — so the same document yields different geometry depending on who asks. On the B-rep path it only controls tessellation smoothness: a flat body meshes identically at any resolution, and a curved one converges. Pin it for anything comparing meshes across runs.

**Units in exported files.** The kernel works in metres; STEP is written in **millimetres** with an explicit `SI_UNIT(.MILLI.,.METRE.)` context, by both writers. The faceted one previously emitted raw metre values with no unit context at all and passed an `AXIS2_PLACEMENT_3D` where a representation context belongs, so an 80 mm plate imported as 0.08 mm.

## LLM configuration

Optional, opt-in via `backend/.env`: `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL` (provider-agnostic OpenAI-style chat protocol; defaults target Moonshot/Kimi, also works with OpenRouter). With no key, the rule-based parser handles everything.

## OAuth sign-in configuration

Optional, opt-in via `backend/.env` (same pattern as billing/LLM — unset means the feature is off and password login is the only path): `OAUTH_GOOGLE_CLIENT_ID`/`OAUTH_GOOGLE_CLIENT_SECRET` and/or `OAUTH_GITHUB_CLIENT_ID`/`OAUTH_GITHUB_CLIENT_SECRET`, plus `PUBLIC_BASE_URL` (register `{PUBLIC_BASE_URL}/api/auth/oauth/{provider}/callback` as the redirect URI in the provider console). The flow (`services/oauth.py`) is Authorization Code + PKCE (S256) with a server-side single-use `state`; token exchange is server-to-server and provider tokens are never stored or sent to the browser — the SPA receives a single-use 60-second handoff code and redeems it at `POST /api/auth/oauth/exchange` for the session JWT. Only verified provider emails are accepted; identities link to accounts by provider subject id first, then verified email (`Account.oauth_ids`).

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

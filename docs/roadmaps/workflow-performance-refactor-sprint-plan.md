# The Forge: Workflow and Performance Refactor Sprint Plan

Repository: [joedean75/cad_system](https://github.com/joedean75/cad_system)  
Prepared: September 5, 2026  
Status: **Implemented.** All six sprints are on the branch. What differed from
the plan, and what the work found, is below; the plan itself is unedited.

## What shipped, and where it differed

| Sprint | Landed as | Deviation worth knowing |
|---|---|---|
| 1 | `services/instrumentation`, `benchmarks/{fixtures,run}.py`, `tests/test_workflow_regressions.py`, `docs/FINGERPRINTS.md`, a CI benchmark job | The recorder is a module global, not a ContextVar — the harness drives the app through `TestClient`, whose request handling runs on a portal thread that does not inherit the caller's context, so a ContextVar would have recorded nothing under the one thing that uses it. The planned SDF fixture was to be a smooth `FilletFeature` blend; the catalog can no longer build one, so it is a shelled body, which the exact kernel declines for the same reason. |
| 2 | `services/derived_cache`, `document.revision` + `expected_revision`, `document_service.writing`, the store split into three slices with request tickets | The per-document write lock was not in the plan and is required by it: the concurrency test showed two edits both reading revision 3, both passing the check, and both reporting 4 — so `expected_revision` was returning a number the caller's own write never produced. |
| 3 | `services/jobs` + `services/job_handlers`, generic `/jobs` routes, `store/document/polling.ts` | `JOB-01` names Redis as a candidate; the queue is the datastore instead, for the reason argued in `jobs.py`. Cross-machine fan-out is a `claim` adapter away and is not needed at this scale. |
| 4 | `services/blobs` (memory + filesystem adapters), `services/singleflight`, G-code as an artifact | An object-store adapter is the same three methods and is not written: nothing in this deployment has a bucket, and an adapter with no user is an adapter with no test. |
| 5 | `services/workflow`, FEA on the queue as `start_analysis`, `job_status`/`cancel_job`, `tests/test_shared_types_contract.py` | `ARCH-02` (extracting touched handlers into domain modules) was **not** done. The handlers this work touched are four out of seventy in one registry, and splitting four of them buys a second place to look while leaving the other sixty-six where they are. Deferred deliberately, as the plan's capacity rule allows. |
| 6 | `docs/SCALING-AUDIT.md`, account/OAuth state moved off-process, `benchmarks/load.py`, `benchmarks/budgets.py` gated in CI, `DEPLOY.md` ops sections | Multi-worker is **not** enabled and the audit says why: the document write lock and the collaboration rooms are still per process. `RELEASE-01` makes that conditional rather than required, which is the outcome. |

## What the work found that the plan did not predict

Each of these was a live defect, found by writing the measurement or the test
the plan asked for rather than by looking for bugs:

- **`set_material` did not reliably persist.** It relied on the evaluate path to
  save, and Sprint 2's "an edit that changed nothing is not a write" made that
  conditional on the *mass* moving. Two aluminium grades have all but identical
  densities, so binding one to a part made of the other silently lost the
  assignment on reload — intermittently, keyed on the densities involved.
- **`dfm`'s `resolution` argument is not the geometry's.** It meshes the additive
  rows. Fingerprinting the body on it made a report stale the instant anyone
  asked with a different one, and current when the body had been rebuilt at
  another. Fingerprints now always use the resolution the body was evaluated at.
- **The filesystem blob sweep deleted the file and left the cache in front
  serving it** — a retention policy that applied only to the other instances.
- **`catalog_ref` had never been mirrored into `shared/types.ts`.** Found by the
  contract test on its first run, which is what that test is for.
- **Job handlers registered only if something happened to import them**, so a
  job that submitted fine through the router raised `KeyError` from a script.

## The measurements

Taken with `benchmarks/run.py --repeat 3` on a 4-core x86_64 container with OCP
present and the native SDF wheel absent. Not budgets — the numbers a budget
would be argued from.

| Scenario | Cold | Warm p50 | Warm p95 | Payload |
|---|---|---|---|---|
| `exact_plate.evaluate` | 16.2 ms | 1.3 ms | 1.6 ms | 15.9 KiB |
| `exact_plate.edit` | 7.3 ms | 72.6 ms | 73.4 ms | 21.0 KiB |
| `feature_tree.edit` (17 features) | 521.9 ms | 459.1 ms | 540.9 ms | 200.3 KiB |
| `sdf_shell.edit` | 1833.6 ms | 1242.1 ms | 1363.0 ms | 4402.0 KiB |
| `sdf_shell.export_step` | 13.2 s | 14.4 s | 15.9 s | 272.4 MiB |
| `library.list` (30 docs) | 2.5 ms | 1.1 ms | 1.1 ms | 12.2 KiB |

Two of those are the prioritised bottleneck list `PERF-01` asked for. On the
17-feature fold the **kernel is 452 ms of a 461 ms edit** with the B-rep node
cache hitting 11 of 28 lookups — so the fold, not the tessellation or the
encode, is where an edit's time goes. And a faceted STEP of the shelled body is
**272 MiB and fourteen seconds**, which is the pre-generation triangle budget
`BLOB-04` requires kept, measured rather than asserted.

Under the mixed load (`benchmarks/load.py --seconds 20`, two editors, two mesh
fetchers, a browser and an analysis job on two workers): edit p50 **956 ms**
against 459 ms in isolation, mesh fetch p50 **0.04 ms** (a blob-cache hit),
library listing p50 34 ms, no errors. The ~2x edit contention is the number a
capacity decision should start from.


## Goal

Keep modeling responsive while expensive CAD work runs reliably, make derived results consistently match the current document, and simplify the frontend workflow without replacing the existing kernels or operation API.

## Planning Assumptions

- Six two-week sprints, provisionally staffed by one backend and one frontend engineer with shared QA/platform support. Re-estimate after Sprint 1; a solo implementation needs a longer schedule or narrower scope.
- Preserve React/Vite/Zustand, FastAPI, SQLAlchemy, the exact B-rep/native SDF/numpy evaluation paths, and the shared operation catalog.
- Ship incrementally behind reversible configuration switches. Do not increase API worker count until all shared-state prerequisites pass.
- This plan follows the repository inspection already completed. No runtime benchmark, load test, or browser profiling was performed; suspected bottlenecks are hypotheses until measured. Reconfirm the starting commit at kickoff.
- Owners below are roles, not assigned people. Acceptance criteria are proposed targets, not claims about current performance.

## Delivery Sequence

| Sprint | Focus | Lead | Dependency | Demonstrable Outcome |
| --- | --- | --- | --- | --- |
| 1 | Baselines and workflow contracts | Backend + frontend | None | Repeatable measurements and regression fixtures |
| 2 | Revision-safe derived state and frontend boundaries | Backend + frontend | Sprint 1 | Edits, undo, and late responses cannot display mismatched results |
| 3 | Durable jobs for optimization and release packages | Backend/platform | Sprint 2 | Heavy jobs survive API restarts without blocking modeling |
| 4 | Shared artifacts and bounded compute caches | Backend/platform | Sprints 2-3 | Generated downloads work across instances and restarts |
| 5 | Consistent analysis/manufacturing workflow | Frontend + backend | Sprints 2-4 | Unified progress, stale-result handling, and reliable recovery |
| 6 | Load validation and scaling readiness | Platform + QA | Sprints 1-5 | Measured release candidate with rollback and operating guidance |

## Sprint 1: Measure and Protect the Existing Workflow

**Goal:** Establish where time and memory go before moving compute or changing state ownership.

**Backlog**

- PERF-01: Build deterministic fixtures for a simple exact part, a larger feature tree, an SDF-heavy model, an optimization workload, and a large document library. Record geometry size, resolution, and hardware for each run.
- PERF-02: Instrument request latency, parameter resolution, kernel evaluation, tessellation, mesh encoding, export generation, cache hit/miss rates, peak process memory, and native/fallback path selection.
- PERF-03: Profile document open, parameter edit, viewport updates, mode switching, and undo/redo. Record network payload sizes, render counts, interaction latency, and production bundle size.
- FLOW-01: Add regression scenarios for edit -> evaluate -> analyze -> export, edit during an active job, undo during evaluation, and switching documents while a request is pending.
- ARCH-01: Define revision and input fingerprints for geometry and downstream analysis/manufacturing results. Document which changes invalidate which products.
- CI-01: Add benchmark reporting to CI. Use fixed-runner repeated measurements before introducing timing gates; retain functional tests on every pull request.

**Primary files:** `backend/app/services/document_service.py`, `backend/app/services/cad_operations.py`, `backend/app/services/document_analysis.py`, `frontend/src/store/useDocumentStore.ts`, `.github/workflows/ci.yml`.

**Acceptance Criteria**

- Cold and warm runs report p50/p95 latency, payload bytes, and memory for every selected fixture, with repeat count and runner details.
- Each representative edit can be traced from API operation through evaluation to mesh readiness.
- Native SDF and numpy fallback correctness are covered independently.
- The team approves fixture-specific latency and memory budgets, plus a prioritized list of measured bottlenecks.

**Demo:** Run the benchmark suite and walk through the baseline editor workflow.

## Sprint 2: Revision-Safe State and Cache Ownership

**Goal:** Make every result attributable to its inputs and reduce coupling in the frontend store.

**Backlog**

- STATE-01: Add a document revision contract and atomic expected-revision checks for writes. Introduce a separate compute fingerprint so unrelated metadata edits do not force geometry recomputation.
- CACHE-01: Extract a small derived-cache service from `document_service.py`. Centralize cache keys and invalidation without moving all document persistence at once.
- CACHE-02: Include resolved geometry, resolution, relevant settings, and kernel/algorithm version in fingerprints as applicable. Include material, loads, constraints, machine/build settings, and solver configuration for downstream products.
- STATE-02: Reject obsolete result publication; keep a result associated with the revision it actually computed. Preserve or explicitly label prior valid results while a new evaluation is pending.
- UI-01: Split `useDocumentStore.ts` incrementally into document/history, selection/sketch, and compute/job slices using existing Zustand patterns. Keep a compatibility facade during migration.
- UI-02: Use targeted subscriptions and request identity checks so late responses cannot overwrite another document or a newer edit. Preserve the existing undo synchronization behavior.

**Primary files:** `backend/app/services/document_service.py`, `backend/app/services/persistence.py`, `backend/app/services/cad_operations.py`, `frontend/src/store/useDocumentStore.ts`, `shared/types.ts`.

**Acceptance Criteria**

- Tests cover rapid edits, out-of-order responses, document switches, concurrent saves, undo/redo, and failed evaluation.
- No stale result overwrites newer document state or appears as current analysis.
- Equivalent compute inputs reuse cached work; every tested geometry or analysis dependency change invalidates the appropriate result.
- Selection-only changes do not initiate geometry evaluation. Store extraction does not increase measured render counts or break existing consumers.

**Demo:** Start a computation, edit and undo the document, then show the completed result cannot silently replace the current state.

## Sprint 3: Durable Background Jobs

**Goal:** Separate expensive computation from API request handling, starting with existing optimization and release-package jobs.

**Backlog**

- JOB-01: Select one maintained queue implementation through a bounded architecture spike. Redis-backed workers are a candidate, not a requirement to add multiple queue frameworks.
- JOB-02: Define a shared job contract: queued, running, succeeded, failed, cancel-requested, and cancelled, with progress, timestamps, input fingerprint, result reference, and structured error.
- JOB-03: Persist immutable job inputs and authorized job metadata; make status and results available after API restarts. Add bounded worker concurrency, backpressure, timeouts, and retention.
- JOB-04: Move optimization and release-package execution out of FastAPI background tasks. Workers load snapshots instead of receiving process-local document objects.
- JOB-05: Make retry/publication idempotent and revision-aware. Use cooperative cancellation at safe checkpoints; do not imply that every native operation is immediately interruptible.
- UI-03: Centralize polling with backoff, stop polling terminal jobs, reconnect to active jobs after reload, and show actionable errors.

**Primary files:** `backend/app/routers/documents.py`, `backend/app/services/document_service.py`, `frontend/src/store/useDocumentStore.ts`, `shared/types.ts`, deployment configuration.

**Acceptance Criteria**

- Restarting the API does not lose queued/running job visibility. Worker interruption produces a recoverable retry or explicit terminal failure, never a permanent false-running state.
- Retried deliveries cannot apply optimization results twice or overwrite a changed document.
- Job reads, cancellation, and result publication enforce current authorization and document ownership rules.
- Ordinary document reads meet the Sprint 1 latency budget while representative optimization jobs run at the configured concurrency limit.

**Demo:** Submit optimization, restart the API, reopen the editor, and observe completion for the correct document revision.

## Sprint 4: Durable Artifacts and Memory Budgets

**Goal:** Decouple generated bytes from the process serving the download and bound memory by size as well as entry count.

**Backlog**

- BLOB-01: Introduce an artifact storage interface with a development filesystem adapter and a shared production object-store adapter. Preserve existing URL contracts where practical.
- BLOB-02: Move mesh, thumbnail, and export bytes behind that interface. Record content digest, media type, byte size, input fingerprint, and retention metadata.
- BLOB-03: Keep document authorization separate from content addressing. A known digest must not grant access to another user's artifact.
- BLOB-04: Define expiration, orphan cleanup, missing-artifact recovery, and document-deletion behavior. Store G-code as an artifact rather than a base64 value in transient document state.
- CACHE-03: Add byte budgets and eviction metrics where measurable; use conservative estimates and RSS monitoring for native B-rep memory. Keep live geometry objects local to compute processes.
- PERF-04: Add per-key in-flight deduplication where benchmarks show repeated identical evaluations. Include failure cleanup so callers cannot wait indefinitely.

**Primary files:** `backend/app/services/artifacts.py`, `backend/app/services/document_service.py`, `backend/app/services/document_analysis.py`, `backend/app/routers/documents.py`, `DEPLOY.md`.

**Acceptance Criteria**

- Artifacts generated by one instance can be downloaded through another and remain available after an API restart within the declared retention policy.
- Unauthorized cross-document fetches fail even when the digest is known.
- Repeated workload tests remain within the approved memory budget; entry counts alone are not treated as proof of bounded memory.
- Oversized STEP exports retain their pre-generation triangle-budget rejection.
- Storage failures and missing artifacts produce a recoverable response rather than a permanently broken download URL.

**Demo:** Generate a mesh and export, restart the serving instance, and retrieve both through the normal authorized routes.

## Sprint 5: Consistent Workflow Across Modes

**Goal:** Make Model, Optimize, Analysis, Make, Validate, and Machine share predictable result and job behavior.

**Backlog**

- FLOW-02: Define prerequisites and result freshness per mode. Distinguish missing inputs, queued work, running work, current results, stale results, and failures.
- JOB-06: Extend the job contract first to the highest-cost measured analysis/manufacturing path. FEA, slicing/CAM, and heavy drawings/exports are candidates; retain fast operations inline when evidence supports it.
- UI-04: Keep editing and navigation usable during jobs. Surface progress, supported cancellation, retry, and artifact download consistently across migrated workflows.
- API-01: Reuse the existing operation envelope and normalize frontend error handling. Add schema/contract tests to catch drift in the hand-mirrored shared types.
- ARCH-02: Extract only the operation handlers touched by this work into domain modules, preserving catalog names, metadata, and UI/agent/MCP behavior. Defer a wholesale rewrite of `cad_operations.py`.
- QA-01: Add end-to-end coverage for modeling through manufacturing, stale-result detection, reload/reconnect, and failure recovery.

**Primary files:** `frontend/src/store/useDocumentStore.ts`, `backend/app/services/cad_operations.py`, `backend/app/services/document_analysis.py`, `backend/app/routers/documents.py`, `shared/types.ts`.

**Acceptance Criteria**

- All migrated workflows share one tested job lifecycle and clearly identify which inputs produced their results.
- Editing relevant geometry, material, loads, or manufacturing settings invalidates dependent results consistently.
- Analysis fallback estimates remain explicitly marked as estimates, not validated FEA results.
- The chosen additional heavy workflow runs outside request handling and can recover after a page reload.
- Existing operation names and response contracts remain compatible for UI and agent clients.

**Demo:** Model a part, run analysis, change a relevant input, regenerate, and download a manufacturing artifact without losing editing context.

## Sprint 6: Load Validation and Release Readiness

**Goal:** Validate actual improvement and close shared-state gaps before considering multiple API workers.

**Backlog**

- SCALE-01: Audit and address process-local account/API-key indexes, rate limits, OAuth state/handoffs, document caches, and WebSocket collaboration. Prefer authoritative DB reads or established shared storage; define cross-worker invalidation and event delivery explicitly.
- SCALE-02: Verify optimistic concurrency across processes and prohibit stale cached documents from overwriting newer persistent state.
- PERF-05: Run mixed-load tests covering browsing, editing, optimization, artifact downloads, and the additional migrated workflow. Compare cold/warm p50/p95 latency, memory, errors, and throughput with Sprint 1.
- CI-02: Gate deterministic payload and bundle budgets on pull requests. Add stable-runner performance checks using agreed noise tolerance and repeated samples.
- OPS-01: Document worker sizing, queue saturation, job recovery, artifact retention, migrations, deployment order, alerts, and rollback. Preserve a supported single-API-instance configuration.
- RELEASE-01: Release gradually with a small initial deployment and explicit stop conditions. Multi-worker enablement is conditional, not required to claim the refactor complete.

**Primary files:** `DEPLOY.md`, `render.yaml`, `Dockerfile`, `docker-compose.yml`, `.github/workflows/ci.yml`, plus shared-state services identified in the audit.

**Acceptance Criteria**

- Approved fixture-specific latency and memory budgets pass under declared concurrency on recorded hardware.
- Multi-worker tests cover authentication, OAuth, rate limits, job polling, artifact fetches, document edits, and collaboration before enabling a second API worker. Any gap keeps production single-worker.
- Restart, timeout, retry, cancellation, storage outage, and rollback exercises have documented outcomes.
- Benchmark results report measured gains and regressions without substituting architecture changes for evidence of speedup.

**Demo:** Replay the baseline workflows under mixed load, show the before/after results, and perform a controlled rollback exercise.

## Scope and Release Rules

- Preserve the kernel architecture, feature semantics, face/edge references, access controls, and current operation catalog. Kernel replacement, microservices, and broad visual redesign are out of scope.
- Reserve approximately 20% of each sprint for tests, integration, and unexpected geometry/state edge cases. Do not use unvalidated story points as a delivery promise.
- If capacity is constrained, defer additional job types and multi-worker rollout first. Keep revision correctness, durable initial jobs, authorization, and artifact reliability in scope.
- Every sprint ends with a working demo, relevant automated tests, documented compatibility impact, and a reversible release path.
- Changes must pass backend tests plus frontend lint/build. Add focused contract and browser tests as these boundaries change.
- Before queue or storage rollout, document behavior for already-running jobs and already-issued URLs; drain or migrate deliberately.

## Repository Evidence

These are the main inspected sources grounding the backlog. Links follow `main`; pin them to the kickoff commit when implementation begins.

- [Architecture](https://github.com/joedean75/cad_system/blob/main/docs/ARCHITECTURE.md): layered backend, operation API, evaluation paths, and process-local collaboration/jobs.
- [Deployment guidance](https://github.com/joedean75/cad_system/blob/main/DEPLOY.md): single-instance constraints and shared-state scaling prerequisites.
- [Document service](https://github.com/joedean75/cad_system/blob/main/backend/app/services/document_service.py): evaluation, local caches, revisions, and optimization.
- [Document routes](https://github.com/joedean75/cad_system/blob/main/backend/app/routers/documents.py): background jobs, polling endpoints, and artifact routes.
- [Artifact service](https://github.com/joedean75/cad_system/blob/main/backend/app/services/artifacts.py): content-addressed exports held in memory.
- [Frontend document store](https://github.com/joedean75/cad_system/blob/main/frontend/src/store/useDocumentStore.ts): combined document/history/selection/compute responsibilities and polling.
- [Operation catalog](https://github.com/joedean75/cad_system/blob/main/backend/app/services/cad_operations.py): shared mutation envelope and operation handlers.
- [CI workflow](https://github.com/joedean75/cad_system/blob/main/.github/workflows/ci.yml): existing backend, native sampler, frontend, and Docker checks.

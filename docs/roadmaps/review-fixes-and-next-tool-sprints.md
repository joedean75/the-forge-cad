# Review Fixes and Next Tool Sprints

Repository: [joedean75/cad_system](https://github.com/joedean75/cad_system)
Prepared: September 6, 2026
Status: Proposed follow-on plan

## Goal

Close the review findings from the workflow/performance refactor commits, then continue with three practical tool and feature sprints that build on the now-measured CAD workflow: safer writes, clearer job outcomes, and more useful RC/drone/robotics design tooling.

## Review Fix Sprint: Correctness Before New Surface Area

**Goal:** Fix the three review findings before adding more feature work.

### FIX-01: Lock Direct Document Updates

**Finding:** `PUT /api/documents/{doc_id}` calls `document_service.update_document` directly, and that service checks `expected_revision` outside `document_service.writing(doc_id)`. The operation path serializes mutating writes, but the direct update path can still let two overlapping whole-document updates pass the same revision check.

**Plan**

- Wrap the full `get_document -> check_revision -> mutate -> persist/evaluate` sequence in `document_service.update_document` with `writing(doc_id)`.
- Keep the lock in the service rather than only in the router, so all direct service callers get the same guarantee.
- Add a regression test with two concurrent `PUT /api/documents/{id}` requests carrying the same `expected_revision`; exactly one should succeed and the loser should get a 409.
- Add a second test for rename-only or metadata-only updates so the lock also covers non-geometry writes.

**Acceptance Criteria**

- Two concurrent direct updates cannot both report success for the same expected revision.
- Existing operation-route optimistic concurrency tests still pass.
- No deadlock occurs when a tree-changing direct update evaluates and persists.

### FIX-02: Preserve Handler Success Messages

**Finding:** Job handlers return richer success messages, but `jobs.run` overwrites all successful jobs with generic text like `optimize complete`. Optimize loses its removed-feature warning, and release packages lose the revision-specific completion text.

**Plan**

- In `jobs.run`, when the handler returns a dict, prefer `result.get("message")` as the stored job message.
- Keep a generic fallback for handlers that return no message.
- Decide whether `message` should remain inside `job.result`; either preserve it for compatibility or strip it only after all callers are audited.
- Add tests for optimize-style and release-package-style handler results proving `job.public()["message"]` carries the handler's message.

**Acceptance Criteria**

- Optimize job polling shows removed-feature guidance when applicable.
- Release package polling shows the packaged revision message.
- Existing clients that read `result.message` are not broken without a deliberate migration.

### FIX-03: Make Stale Optimization Non-Retryable

**Finding:** `run_optimize` converts `RevisionConflict` into `RuntimeError`, so the generic job runner retries it. A stale optimization result is not transient; retrying with the same submitted revision will fail again and burns worker capacity.

**Plan**

- Introduce a typed non-retryable job error, for example `jobs.NonRetryable`, carrying a structured error code and remedy.
- Raise it from the optimize handler when `RevisionConflict` occurs.
- Have `jobs.run` finish the job as `failed` immediately for non-retryable errors, without sending it through `_retry_or_fail`.
- Include the submitted revision and current revision in the error detail when available.
- Add tests proving stale optimization fails once, does not increment into a retry loop, and gives a rerun-against-current-document remedy.

**Acceptance Criteria**

- A stale optimization job reaches a terminal failure on the first detected revision conflict.
- The job error clearly distinguishes changed inputs from solver/runtime failure.
- Worker retry behavior remains intact for actual worker-loss or transient execution failures.

## Tool Sprint 1: Purchased Component Catalog That Carries Real Mass

**Goal:** Make RC/drone/robotics assemblies use real component mass and mounting envelopes rather than geometry-derived guesses for purchased parts.

**Backlog**

- TOOL-01: Wire `parts_catalog.Entry.typical_mass_g` into BOM and assembly mass calculations for catalog-backed documents.
- TOOL-02: Add explicit mass-source metadata: geometric, catalog, user override, or unknown.
- TOOL-03: Add catalog entries for common micro servos, 28xx-class BLDC motors, ESCs, flight controllers, receivers, battery envelopes, bearings, shafts, and standard hardware used in 1/16 RC builds.
- TOOL-04: Treat LiPo batteries as user-specified envelopes with voltage/cell count/capacity metadata, not guessed dimensions.
- TOOL-05: Add tests for CG and inertia when heavy purchased components use catalog mass instead of envelope volume.

**Acceptance Criteria**

- BOM mass, assembly mass, CG, and inertia use catalog mass where available.
- Every mass report states where each mass came from.
- A NEMA/motor/servo-style purchased component no longer reports misleading mass from its simplified CAD envelope.

**Demo:** Build a simple drivetrain assembly with a motor, servo, battery, and printed bracket; show BOM mass and CG with source labels.

## Tool Sprint 2: RC Drivetrain and Steering Toolkit

**Goal:** Add reusable operations and templates for small RC vehicle mechanisms, especially the Baja-style and 1/16 differential work already in flight.

**Backlog**

- TOOL-06: Add drivetrain templates for solid axle, simple differential placeholder, dogbone/CVD shaft envelope, universal joint envelope, spur/pinion layout, and motor mount pattern.
- TOOL-07: Add steering templates around common micro servos: servo tray, horn clearance, tie rod envelope, knuckle pivot markers, and Ackermann guide geometry.
- TOOL-08: Add gear direction and shaft-axis annotations that can be exported into drawings.
- TOOL-09: Add parameter presets for common 1/16 scale dimensions: wheelbase range, track width range, M2/M2.5 hardware, 3 mm shafts, 5x10/5x11 bearings, and common servo sizes.
- TOOL-10: Generate STEP/STL plus a shop-style drawing package from each template.

**Acceptance Criteria**

- A user can create a printable RC drivetrain/steering layout from operations rather than hand-building every envelope.
- Templates remain editable feature trees, not opaque meshes.
- Drawings include rotation arrows, shaft labels, bearing sizes, and hardware callouts.

**Demo:** Generate a 1/16 rear module with motor mount, axle supports, shaft envelope, servo tray, and an annotated drawing.

## Tool Sprint 3: Manufacturing-Ready Package Flow

**Goal:** Turn a useful CAD model into files someone can print, machine, inspect, and revise without manually assembling artifacts.

**Backlog**

- TOOL-11: Add a package preset for 3D printed RC parts: STL, STEP, drawing PDF, hardware BOM, recommended print orientation, support-risk notes, and tolerance callouts.
- TOOL-12: Add split-body and fastener-clearance checks for printable chassis parts: bed fit, screw access, bearing press-fit, nut-trap clearance, and minimum wall warnings.
- TOOL-13: Add drawing-style presets matching the user's preferred technical illustration style: exploded view, labelled parts, gear-direction arrows, section/detail bubbles, and dimension tables.
- TOOL-14: Add release-package manifest fields for intended process, material, printer/nozzle assumptions, and revision notes.
- TOOL-15: Add workflow status for package readiness: blocked by missing material/BOM/drawing, ready, running, current, stale, or failed.

**Acceptance Criteria**

- A release package can be generated for a printable RC subassembly with manufacturing assumptions recorded.
- Package status goes stale when geometry, material, BOM, or drawing inputs change.
- The drawing output is detailed enough to guide assembly without opening the CAD editor.

**Demo:** Package the rear module from Tool Sprint 2 into a printable/manufacturing handoff bundle.

## Delivery Rules

- Fix Sprint ships first. Do not start tool work until the three review findings are resolved and covered by regression tests.
- Preserve the existing operation catalog and feature-tree editability.
- Prefer measured fixture updates over broad rewrites.
- Every sprint ends with tests, a demo model, and updated roadmap status notes.
- If capacity is tight, defer broader catalog coverage before deferring real-mass correctness or revision-safe writes.

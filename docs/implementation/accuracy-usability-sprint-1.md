# Accuracy and usability — Sprint 1, first implementation

## Scope

This is the first delivery from the three-sprint plan, not completion of all
three sprints. It builds on main's assembly mate fix and per-run model picker.

Implemented:
- Exact B-rep bounds, volume, area, centroid and inertia feed document summaries
  and mass reports. Specification checks read the same engineering measurements.
- Display meshes retain their current topology and triangle labels. SDF
  measurements remain approximate and are labelled as mesh-based.
- Measurement cache version changes invalidate older derived readings.
- Assembly navigation uses Z-up and an XY ground plane, freezes the display
  transform during edits, and provides an explicit Fit action.
- A failed assembly refresh retains stale state and exposes Retry. Obsolete
  responses cannot overwrite another assembly, newer edits or new invalidation.
- Measurement panels identify exact geometry versus approximate mesh.
- Regression coverage exercises engineering readings and refresh recovery.

## Validation

Before the local workspace reset, 195 focused backend tests and the frontend
production build passed. Six refresh behavior cases also passed with the
initial local runner. The restored branch uses Node's built-in test runner and
the existing TypeScript dependency for those cases; GitHub CI is the authority
for validation of the saved branch. Browser verification remains outstanding.

Backend regression selection:
`test_engineering_measurements`, `test_design_spec`, `test_body_kind`,
`test_part_mass`, `test_published_mass`, `test_mass_budget_urdf`,
`test_evaluation_cache`, `test_shared_types_contract`,
`test_revision_and_fingerprints`, `test_brep_kernel`,
`test_assemblies`, `test_assembly_mates`.

## Remaining work

Sprint 1:
- RC-specific copilot evaluations and live-provider success measurements.
- Broader retention of precision inputs and selections after rejected edits.
- Browser verification of camera stability, Fit and retry presentation.

Sprint 2:
- Reproduce shadow lines; distinguish self-shadowing, normals and overlays.
- Correct rendering normals without changing the welded analysis/export mesh.
- Clean CAD lighting, shared appearance settings and visual regression fixtures.

Sprint 3:
- Extend current performance baselines before selecting timing targets.
- Extract operation domains while preserving one registry and execution boundary.
- Consolidate viewport/resource ownership and remove repeated normal computation.
- Profile store subscriptions and face highlighting; simplify obsolete code/docs.

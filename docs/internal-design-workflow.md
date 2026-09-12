# Internal Design Workflow

Forge is an internal design tool. Priorities are fast iteration, reusable
components, trustworthy geometry, and clear recovery from failed operations.
Public distribution, commercial onboarding and billing changes are outside this
change. Existing access controls continue to protect design files.

## Adafruit Components

In the assembly component picker, select **Adafruit**, search a product name or
number, set quantity, and click **Place**. The server downloads the pinned file,
verifies its Git blob hash, imports it, and reuses the account's document on later
placements. No ZIP download or manual upload is required. Cached files continue
to work offline while they remain in the cache. First placement needs outbound
HTTPS to raw.githubusercontent.com.

The download cache is bounded by **bytes**, not by a file count, and evicts
least-recently-used files once it exceeds `FORGE_ADAFRUIT_CACHE_MB` (default
256). The whole pinned catalog is 866 MB — 484 files averaging 1.8 MB, the
largest 57 MB — so a file count would be no budget at all. Eviction is always
safe: every file is re-downloadable and re-verified against the pinned checksum,
so a deleted one costs a fetch and never correctness. A single file larger than
the whole budget is kept rather than refused, or a 57 MB model would be a part
that could never be placed.

The catalog includes 480 STEP/STP files and four STL alternatives for folders
without STEP, pinned to Adafruit_CAD_Parts revision
`af042a90c642be09752ebf9d99ba10f84f8daf0b`. MIT attribution is bundled under
`backend/app/services/data/ADAFRUIT-LICENSE.txt`. Fusion/3MF-only variants are not
advertised as directly importable. Unknown dimensions and mass are not invented.

A row's `geometry` states what placing it will actually produce rather than what
its suffix implies. STEP and IGES import exactly **only when OpenCascade is
present** — without OCP they voxelise like any mesh — so a listing that read the
suffix alone told all 480 STEP rows they were `exact` on a deployment with no
exact kernel at all, and `geometry` is the field that decides whether a caller
can address, fillet or measure a face. With OCC present it remains a prediction,
since a STEP file OCC cannot read still falls back to voxels; the placed
document is the authority either way.
Imported geometry still uses the application's mass estimator, which is not a
manufacturer-supplied electronics mass; assign measured mass for balance studies.

Search is debounced and paginated. Generated RC-part parameters are preserved.
Personal imports remain account-scoped. Adafruit files use immutable cached
bytes and separate editable documents per account. Same-key import serialization
uses the application's process-local locking, so deployments with multiple
workers may still create duplicate part documents under simultaneous first use.

## Agent Execution

The existing typed operation registry, schema checks, permission checks and
trajectory storage remain the execution path. The agent now:

- Checks geometry after mutations before accepting completion.
- Rechecks declared design requirements on every completion attempt.
- Stops after three identical failed actions or its bounded step/time budget.
- Exposes typed error details and bounded nested tool-result summaries.
- Records distinct completion, cancellation, planner-error, verification-failure
  and budget outcomes instead of treating all terminal runs as success.
- Accepts **Stop** in the Copilot panel, with cancellation checked before the next
  mutation, including after a slow model response.

Cancellation and the ten-minute deadline are cooperative: a running native CAD
operation is not forcibly killed. The model HTTP timeout remains 60 seconds.
The existing trajectory records persist actions; the live Copilot job registry
is still process-local and does not provide restart/resume or cross-worker
cancellation. These changes do not add a shell executor or a second orchestrator.

## Verification

Run the focused backend tests:

```bash
cd backend
python -m pytest tests/test_adafruit_parts.py tests/test_copilot.py tests/test_supplier_parts.py tests/test_parts_catalog.py tests/test_assembly_operations.py -q
```

Then run `npm run build` from `frontend`. Tests cover catalog pagination, checksum
and cache behavior, account-scoped reuse, exact STEP import using a generated
fixture, unknown keys, repeated failures, cancellation, and completion gates.

# Sprint 3: readability and performance — first delivery

## Face highlighting

Previously every changed face selection scanned all triangle labels. A lazy
lookup now maps each face ID to its triangles once, and later selections visit
only those triangles. A WeakMap keyed by the immutable labels avoids storing
the lookup in Three.js userData and allows collection with the labels. When
clones share labels, they can share the lookup. Replacement label arrays get
new lookups; in-place label mutation is unsupported (render labels are immutable).

Multi-face selections retain body triangle order and ignore duplicate IDs.
Highlight vertices remain compact and independent of the body GPU buffer.
The change does not alter mesh transport, measurement, export or face IDs.

## Measured baseline

Local Node v24.19.0, 200,000 synthetic triangles, 2,000 faces, 100 triangles per
selected face, 50 warmups and 500 timed selections. Compared against main at
e0c05a1a987ccfe2250a53f3b09a527a4735ebeb using the same fixture.

| Measurement | Before | After |
| --- | ---: | ---: |
| First highlight | 3.288 ms | 4.000 ms |
| Warm median | 1.115 ms | 0.034 ms |
| Warm p95 | 1.451 ms | 0.072 ms |

The indexed version retained approximately 2.3 MiB of additional JS heap after
the first highlight. Heap deltas are noisy; the baseline delta was -16 KiB after
GC. The memory cost scales with triangle count. These are single local-run
observations, not frame-rate claims or timing gates. First-use work remains
linear, and selecting most faces still requires processing most triangles.

Reproduce from frontend:

```sh
npm ci
npm run benchmark:highlight
# Optional comparison with a saved prior forgeMesh.ts source:
node --expose-gc benchmarks/face-highlighting.mjs /path/to/baseline-forgeMesh.ts
```

CI reports current timing and heap observations with its own Node version. The
regression gate checks behavior and verifies warm hovers do not reread all face
labels; it does not use an environment-sensitive timing threshold.

## Validation and remaining scope

All 20 frontend behavior tests passed locally, including four new lookup tests.
Lint, full type checking and production build are checked on the PR by CI.
Browser visual verification and interactive frame-time measurement are pending.

Earlier Sprint 2 commits extracted display fitting and face overlay components,
shared lighting/materials, and added explicit geometry cleanup. Next Sprint 3
work: profile store subscriptions, reduce duplicated operation-domain code
behind the existing registry, and assess loader-cache ownership for assemblies.
This delivery does not claim completion of those remaining tasks.

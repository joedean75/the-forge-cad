# Revisions and fingerprints: which change invalidates which result

*The contract behind `ARCH-01`. Implemented in `backend/app/services/derived_cache.py`;
the tests that hold it are `backend/tests/test_revision_and_fingerprints.py`.*

Everything downstream of the feature tree — the mesh, the FEA solve, the DFM
report, the toolpath, the spec score, the drawing — is **a reading of a body**.
The body moves on every edit. So a stored reading is only meaningful next to a
record of what produced it, and only showable next to an answer to "does that
still describe the document?".

## Two counters, two questions

|                        | `document.revision`                     | fingerprints                                       |
| ---------------------- | --------------------------------------- | -------------------------------------------------- |
| Counts                  | writes                                  | inputs                                              |
| Moves on a rename       | yes                                     | no                                                  |
| Moves on a material change | yes                                  | the geometry one no; the analysis one yes           |
| Moves on an analysis run | no (`persist_derived`)                 | no                                                  |
| Answers                 | "did somebody else write since I read?" | "is this result about the part I am looking at?"    |
| Enforced by             | `expected_revision` on a write          | `derived_cache.publish` / `freshness`               |

Conflating them makes either a rename rebuild the body, or a lost update
invisible. They are deliberately independent.

### Revision

`document.revision` is an integer bumped by `document_service._persist`, so
**every** persisted mutation moves it — including ones that cannot move the body.
It is bumped in one place rather than at each of the dozen mutators, because a
revision that is *sometimes* current is worse than none: an expected-revision
check would then pass on a stale read.

Two things deliberately do **not** bump it:

- **A re-evaluation that changed nothing.** `evaluate_document` on a cache hit
  used to persist a new `updated_at` regardless, so merely *looking* at a
  document through a path that ensures geometry invalidated somebody else's
  optimistic write. Mass is the one derived number a material moves, so a change
  in it is the one thing worth writing for.
- **Filing a derived reading** (`persist_derived`). Running an analysis is not
  an edit. Bumping for it would make every query a source of spurious 409s on a
  shared document, which is how people learn to stop passing the revision.

**Optimistic writes are opt-in.** `POST /api/documents/{id}/op` and
`PUT /api/documents/{id}` both accept `expected_revision`; omitting it keeps
last-write-wins, which is right for the single-tab case that is most of the
traffic. When it is given and does not match, the write is refused — 409 on the
PUT, a typed `PRECONDITION` in the operation envelope — and both numbers come
back, so the caller learns how far behind it was without a re-read. Every
operation response carries the revision it produced, so a caller can chain edits
without re-reading at all.

The check and the write it guards run under **one per-document lock**
(`document_service.writing`). Without that they can be separated: two edits
arriving together both read revision 3, both pass the check, both apply, and
both report revision 4 — handing a caller a number its own write never produced.
The lock is per document (two people editing two parts have nothing to
serialize), reentrant, and reference-counted rather than capped, because a
capped registry evicts the lock a caller is currently *holding* under enough
churn and loses mutual exclusion exactly when the system is busiest.

### Geometry fingerprint

`derived_cache.geometry_fingerprint(doc, resolution)` — stored on the document as
`derived.eval_key`. It hashes everything that changes what an evaluation would
produce:

- the feature tree,
- the resolution (a *total voxel budget* on the SDF paths, tessellation
  smoothness on the B-rep one),
- the active configuration,
- the parameter definitions (minus their resolved `value`, which is an output of
  the evaluation being keyed, not an input to it),
- the configuration table.

It is deliberately generous, and the model dumps are taken **minus named
outputs** rather than as a list of fields to include — the second form is how a
later schema addition silently stops being part of the key.

It excludes only what provably cannot change geometry: the material, the name,
the spec, the revision counter, and everything under `derived`.

### Analysis fingerprints

`derived_cache.analysis_fingerprint(doc, kind, resolution, settings)`. Every
product starts from the geometry fingerprint — nothing downstream survives the
body moving — and adds the document state and the call settings that product
actually reads. `PRODUCTS` is the table, and it is the invalidation matrix:

| Product     | Document inputs beyond geometry | Call settings                  |
| ----------- | ------------------------------- | ------------------------------ |
| `analysis`  | material                        | `elements`, `load_cases`, `solver` |
| `dfm`       | —                               | `process`, `build_axis`, `resolution` |
| `cam`       | —                               | `machine`, `tool`, `stock`     |
| `spec`      | material, spec                  | —                              |
| `drawing`   | —                               | `spec` (the drawing spec)      |

Two entries are worth their reasons:

- **`analysis` reads the material and `dfm` does not.** Stress is a property of
  the material as much as of the shape. None of the topology measurements DFM
  makes — wall thickness, draft, undercuts, tool access — depends on what the
  part is made of, and calling that report stale on a material change trains
  people to ignore the staleness marker.
- **`dfm` fingerprints the *requested* process**, never `DfmReport.process`,
  which is a display label (`LPBF`, `MILLING`). Fingerprinting the label would
  make an additive report and a milling report indistinguishable to the check.
- **`analysis` reads the load cases, not the whole `optimization` setup.** Most
  of that object — volume fraction, objective, keep regions — is about a SIMP
  run and changes nothing about a linear-static solve.

An **unknown kind raises** rather than defaulting to the geometry fingerprint. A
product fingerprinted on geometry alone looks current forever after the input it
actually depends on changes, which is the failure the whole mechanism exists to
prevent.

## Publication and freshness

```python
fingerprint = derived_cache.analysis_fingerprint(doc, "analysis", resolution, settings)
result      = ... # slow work
kept        = derived_cache.publish(doc, "analysis", result,
                                    resolution=resolution, settings=settings,
                                    fingerprint=fingerprint)
```

`publish` recomputes the fingerprint at publication time and refuses the result
when it no longer matches — the inputs moved while the work ran. **A refusal is
not an error.** The result is a correct reading of a body that is no longer the
current one; the caller may still show it to whoever asked, labelled. What it may
not do is become the document's current answer.

`freshness(doc, kind, resolution, settings)` returns `current` | `stale` |
`missing`. `stale` covers both halves of the same question — the body moved, or
the settings this reading was taken under are not the ones now selected —
because they are the same answer to the reader ("this is not about what you are
looking at"), and splitting them invites a UI that treats one of them as fine.

A result larger than `MAX_RESULT_BYTES` (256 KiB) is recorded as `truncated`
rather than inlined. A drawing sheet or a G-code program is an **artifact** — it
has a URL and a content hash — and inlining one into the document is the mistake
P1 undid for the mesh.

## Where the caches sit

`derived_cache` owns all four, so keys and invalidation are decided in one file:

| Cache        | Key                       | Why that key                                          |
| ------------ | ------------------------- | ----------------------------------------------------- |
| `SOLIDS`     | `doc_id`                  | one live body per document                            |
| `BREPS`      | resolved-tree hash        | a stale exact body is nastier than a slow one; shared across documents that resolve to the same tree |
| `MESHES`     | `(doc_id, mesh_hash)`     | a *superseded* hash still names exactly those triangles, so it stays reachable |
| `THUMBNAILS` | `(doc_id, mesh_hash)`     | a thumbnail is a picture of those triangles           |

`document_service` keeps module-level aliases (`_SOLID_CACHE`, `_BREP_CACHE`, …)
bound to the same objects, so existing callers and tests reach the live caches.

`forget_document(doc_id)` drops everything keyed *by document id* on deletion —
not the exact-body cache, which is keyed by resolved-tree hash, is shared on
purpose, and is bounded and self-evicting.

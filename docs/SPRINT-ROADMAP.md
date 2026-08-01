# Sprint roadmap — closing out the exact-kernel programme

Sprints 1–10 are on `main` (641 tests). **The programme is done**; what is left
is the four honourable mentions.

[PRODUCT-ROADMAP.md](../PRODUCT-ROADMAP.md) is the product-level document. This
one is narrower and concrete: what is left in the work that turned the SDF
editor into an exact-kernel CAD system with a machine-checkable action space,
with the trap that will actually bite in each.

## What is already true

Worth stating, because the remaining sprints only make sense on top of it.

| Landed | What it bought |
|---|---|
| Exact B-rep kernel alongside the SDF (`evaluator/brep.py`) | Addressable faces/edges, exact dimensions, analytic STEP |
| Edge fillet/chamfer, face shell (`ops/exact.py`) | The edge-selection idiom parametric CAD runs on |
| STEP/IGES import as `BrepBodyFeature` | Imported parts edit like modelled ones |
| Whole-body fillet fixed; `_apply_exact` rollback | No operation reports success while doing nothing |
| Design spec + `verify_against_spec` | A reward function with ground truth |
| Topology DFM (`dfm_topology.py`) | Wall thickness, draft, undercuts, tool access — resolution-independent |
| Sheet metal on the exact kernel | The last common part with no addressable topology |
| Real FEA (`fea.py`), `simulate` in the catalog | A verifiable signal that means something |
| True ruled loft (`RuledLoftFeature`) | Frustum volumes exact to 1e-6 |
| Sprint 9: B-rep cache, bounded LRUs, `rollout.py` | 4 ops → 1 OCC build; reproducible rollouts |
| Sprint 8: generative history (`core/history.py`) | A reference follows the topology instead of re-measuring it |
| Sprint 10: five panels under `frontend/src/features/*` | Every capability above is reachable without the API |

Deliberately unchanged: `LoftFeature`, `FilletFeature`/`ChamferFeature` and
`ShellFeature` keep their distance-field meaning, because changing what an
existing document evaluates to based on whether OCC is installed is worse than
having two features.

---

## Sprint 8 — generative-history topological naming  ✅ *shipped*

Every face and edge carries a **tag** — an identity, `{feature_id}:{kind}:{index}`,
minted by the feature that created it and propagated through OCC's
`Modified`/`Generated`/`IsDeleted` at each stage of the build
(`cad_kernel/core/history.py`, threaded through `evaluator/brep.py`).
`naming.resolve_references` follows the tag first and falls back to descriptor
matching, returning a verdict per reference: `matched`, `split`, `deleted` or
`unresolved`. `check_references` publishes those verdicts, and `execute` warns
when an edit leaves a body that still builds but reaches fewer edges than it was
given — valid geometry that quietly stopped matching the request is the failure a
geometry check cannot see.

Three places the plan below needed amending:

* **Tags, not a `BRepTools_History` object.** The accumulator records
  `sub-shape → tag` per stage rather than holding a history handle, because the
  question at rebuild time is not "what did this builder do" but "which feature
  originally made this entity". `BRepTools_History` would have worked as the
  transport and adds nothing over calling the builders directly.
* **`IsDeleted` had to be *distrusted* by default**, which the plan did not
  anticipate. `BRepFilletAPI_MakeFillet` reports untouched box edges as deleted;
  believing it would declare every edge of a filleted part destroyed. Only the
  `BRepAlgoAPI_*` booleans are believed (`advance(..., trust_deleted=True)`).
* **The verdict has to outlive the failure it explains.** A fillet whose edges
  were destroyed is a build failure, and the diagnosis is collected on the way
  to it — so `build_with_history` returns the reference verdicts even when the
  shape is `None`, and `check_references` reads them either way.

The trap the plan called — design for absent history, and do not delete
descriptor matching — was the right one and is what the fallback order encodes.
Reported deletion deliberately *skips* the descriptor fallback: given a face an
upstream edit destroyed, measuring will happily find something 0.5 mm away and
move the fillet onto it, and there is a test pinning exactly that.

**Cost**, measured rather than asserted: recording is always on and adds **6–8%**
to an exact build (2 features 4.3 → 4.5 ms, 13 features 68.0 → 73.5 ms — flat,
not growing with the tree). It was **+346%** first, and the whole difference is
one line in `history._drain`: `list(occ_list)` on a `TopTools_ListOfShape` costs
~0.26 ms *whether or not the list is empty*, because pybind builds an iterator
either way, and the module asks two such questions of every sub-shape at every
stage — almost all of which answer empty. Checking `Size()` first costs a
thousandth of that.

The original plan follows, for the reasoning rather than the instructions.

**Why now.** This is the last correctness gap in the exact kernel, and it is the
famously hard problem in parametric CAD. `core/naming.py` matches entities by
*descriptor* — kind, size, and position normalised to the body's bounding box —
with `_MATCH_TOL = 0.08`. Normalising to the bbox was the insight that made
resize work: stretch a plate 80 → 120 mm and its corner edges move 20 mm
absolute but zero normalised, so a fillet placed on them stays put.

What descriptors cannot survive is an operation that *re-topologises*. Cut a
pocket through a filleted face and the face is split into two; both halves have
new sizes and new normalised positions, and the reference either resolves to
one arbitrarily or is reported unresolvable. Neither is right — the correct
answer is "this reference now names two faces".

**What to do.** OCC already tracks exactly this. Every `BRepAlgoAPI_*` and
`BRepFilletAPI_*` builder exposes `Modified(shape)`, `Generated(shape)` and
`IsDeleted(shape)`, and 7.x collects them into `BRepTools_History`. Thread a
history object through `brep.py`'s builders, so each evaluation records how the
previous topology maps to the new. A stored reference then resolves by
*following the map* rather than by re-measuring:

1. Add a `History` accumulator alongside the shape in `_BUILDERS`, so
   `brep_from_feature` can return `(shape, history)` internally while keeping
   the public signature.
2. Persist a per-feature entity id at creation (not a descriptor — an identity)
   and record `id → {modified, generated, deleted}` per rebuild.
3. `naming.resolve` tries the history chain first and falls back to descriptor
   matching, which stays as the answer for imported bodies with no history.

**How you know it worked.** The test descriptors cannot pass: fillet an edge,
then cut a slot across the face it borders, and assert the fillet still names
*both* resulting edge segments and the body rebuilds. Second: change an upstream
dimension so a face disappears entirely, and assert the reference reports
`deleted` rather than snapping to a neighbour.

**Traps.** `BRepTools_History` is not populated by every builder — `MakeBox` has
no history because it has no input. Design for absent history rather than
assuming it. And do not delete descriptor matching: it is the only thing that
works for a `BrepBodyFeature` read from a STEP file, which has no construction
history by definition.

**Size.** Large. This is the biggest remaining item and the one most likely to
need a second pass.

---

## Sprint 9 — caching and rollout throughput  ✅ *shipped*

`document_service.get_exact_shape` owns the B-rep build and caches it on the
hash of the **resolved** tree — not the document id, because parameters are
resolved *into* the tree rather than stored in it, so hashing the raw tree would
serve a body that silently disagrees with the document. A four-operation
read-only inspection went from 12 recursive OCC builds to 3.

Both geometry caches are LRU-bounded at 64 entries. `services/rollout.py` turns
metering off and pins resolution for a training loop; pinning is the half that
matters, because the plan cap otherwise makes the same document evaluate
differently for different callers. Neither switch touches access control.

## Sprint 10 — surface the new capabilities in the UI  ✅ *shipped*

All five panels landed, in the order below. What the plan did not anticipate:

* **A face picker cannot be a 3D overlay here.** `list_faces` returns ids, but
  the display mesh is one welded triangle soup — `Kernel.from_mesh` makes a
  single `Face` with `surface=None` — so a click on it can say *where* you
  clicked, never *which face*. Inventing a nearest-entity mapping would be right
  most of the time, which is the worst available accuracy for a selection. The
  picker is therefore the list, with the measurements that tell entities apart,
  and the viewport marks the selection so the two are tied together. Marking
  every *listed* entity was tried and dropped: big enough to see is clutter, and
  small enough not to be clutter is invisible.
* **`DfmReport.process` is a display label, not the requested id.** It reads
  `LPBF` for additive and `MILLING` for milling, so `report.process === process`
  never matches and a stale report reads as current on every tab. The store
  records what was *asked for* (`dfmProcess`) instead. Two labels in the report
  header had the same bug and are keyed the same way now.
* **`shared/types.ts` declared `DesignSpec` twice** — the generative-optimization
  spec and the requirements spec — and TypeScript *merges* same-named interfaces
  rather than complaining, so a single type demanded `name`, `design_space` and
  `requirements` at once. The requirements one is `PartSpec` now, which is also
  what `DesignDocument.spec` actually holds.
* **The FEA panel immediately exposed a backend hole**, which is what a UI that
  shows `estimate_only` prominently is for: `fea.solve` rasterised through the
  SDF, so *every* part with a real edge fillet, chamfer, face shell or imported
  STEP solid — the whole point of the exact kernel — silently got the
  closed-form estimate. `fea._occupancy_field` now asks the B-rep directly via
  `BRepClass3d_SolidClassifier` when there is no field. It agrees with the
  analytic path to 0.003% on a body that has both, at about 30 µs a point.
  Voxelising the tessellation was tried first, following `drawing._field`'s
  precedent, and is wrong here: `trimesh.voxelized().fill()` dilates the body by
  a voxel all over (4% heavy at the element pitch, 11% at four times finer,
  because the shell stays one voxel thick), and mass feeds straight into stress.

The acceptance bar below was driven end to end in a real browser: model a
bracket, fillet a picked edge, set a spec, verify it, run a milling DFM and
generate a sectioned drawing, without touching the API — with no console or HTTP
errors.

Original plan follows.


**Why now.** Six sprints of capability are reachable only through the operation
API. A human driving the editor cannot select a face, cannot choose a
manufacturing process, cannot see a spec score, and cannot read an FEA result
that is now real. The frontend has drifted behind the backend by roughly the
whole programme.

**What to do**, in dependency order — each is a panel under
`frontend/src/features/*`, all going through the typed `api` object:

1. **Face/edge picker** (`model`). The unlock everything else needs. `list_faces`
   and `list_edges` already return stable ids with kind, size and position;
   render them as selectable overlays and feed the selection to `fillet_edges`,
   `chamfer_edges` and `shell_faces`. `"all"` is accepted in place of an id list,
   so "round every edge" is one control.
2. **Process selector for DFM** (`make`). `runDfm` already takes a `DfmProcess`;
   it is hardcoded to `additive` at the one call site. Show the topology rows
   and — importantly — show *no quote* for the non-additive processes rather
   than a blank field, because the absence is deliberate.
3. **Spec panel** (`validate`). `set_spec` / `verify_against_spec` with the
   per-requirement pass/fail rows and the score. This is the screen that tells a
   user whether the part is *right*, not merely valid.
4. **Analysis results** (`analysis`). `simulate` returns `solver`, both stress
   figures, the mesh used and the notes. Surface `estimate_only` prominently —
   a fallback that looks like an analysis is the thing the sprint was written to
   prevent.
5. **Drawing spec** (`make`). `DrawingSpec` lets the caller choose views,
   sections, dimensions, projection angle and sheet. The UI still sends the old
   boolean.

**How you know it worked.** `npm run build` is the type-check gate and there is
no frontend test suite, so this one is driven by using it. A reasonable bar:
model a bracket, fillet a picked edge, set a spec, verify it, run a milling DFM
and generate a sectioned drawing — without touching the API directly.

**Traps.** `shared/types.ts` mirrors the Pydantic models **by hand**. Every
sprint here has already added its types (`DfmProcess`, `DesignSpec`,
`SpecReport`); check them rather than redeclaring.

**Size.** Large in aggregate, but genuinely incremental — each panel ships
independently.

---

## Honourable mentions

Real work, not filler; they did not make the ten because each is narrower.

**Drawing completeness.** The spec-driven drawing engine has views, sections,
hidden lines, dimensions and GD&T leaders. Missing: cutting-plane lines with
direction arrows on the parent view, detail and auxiliary views, PDF output, a
path from `drawing.py`'s polylines into `io/dxf.py` (which today is wired only
to sheet-metal flat patterns), and BOM balloons for assemblies. The DXF path is
the cheapest and the most useful — it makes a drawing manufacturable rather
than viewable.

**Exact assembly interference.** `services/interference.py` samples surface
points into the other part's SDF. It works, and Sprint 1's exactness already
forced one fix there (an exact box has 8 vertices, none strictly inside an
overlapping box — triangle centroids were added). With both bodies exact,
`BRepAlgoAPI_Common` gives the actual intersection volume instead of a sample
count, which turns "these parts clash" into "these parts clash by 0.4 mm³".

**Multi-body documents.** The durable unit is one `DesignDocument` with one
root. Real parts are sometimes several disjoint solids, and
`geometry_check.check_solid` already counts connected components — it reports
multi-body as a finding because nothing downstream can represent it.

**Trajectory dataset export at volume.** `decompile_all` and `export_jsonl`
exist and work on single documents. What has not been done is running them over
a corpus, measuring the replay-verification hit rate, and reporting which
feature types fail to decompile. That number is the actual health metric for the
training-data half of this project, and nobody has looked at it.

---

## Suggested order

**8 before 10.** Both 9 and 8 are done, in that order — 9 was cheap and made
every later iteration faster, which is exactly why it was pulled forward, and 8
was the hardest thing left and wanted that fast rebuild loop.

Sprint 10 went last, which was right: it had to be built against a settled
backend, and a face picker written before references gained history would have
needed revisiting. It surfaces the `split` and `deleted` states
`check_references` reports, because a selection that lost its target is
invisible in the viewport and leaves a body that `check_geometry` passes.

The honourable mentions are all independent of both and can be picked up at any
time. The cheapest with real value is the DXF path out of `drawing.py`.

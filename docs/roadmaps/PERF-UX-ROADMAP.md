# Performance & experience roadmap — three sprints

> **Status: P1, P2 and P3 are done.** What each actually cost, measured the same
> way as the numbers below:
>
> | | before | after |
> |---|---|---|
> | Document JSON per edit | 1164 KB | 1.5 KB + a mesh fetched once |
> | `set_material` | 90 ms + full mesh | 16.6 ms, mesh URL unchanged |
> | Edit the last of 12 features | 467 ms | 268 ms (1 boolean, not 11) |
> | `get_exact_body` after a display build | a second full build | free |
> | 300-curve profile extruded at res 128 (numpy SDF) | 23.5 s | 0.51 s |
> | First-paint JavaScript | 1,435 KB / 388 KB gz | 195 KB / 62 KB gz |
> | Undo | full round trip + re-evaluation | local, ~400 ms to the restored body |
> | Clicking the body | nearest analytic proxy (a guess) | the feature that made the triangle |
>
> **One item was deliberately not done: P2's "evaluate off the request."** After
> the per-node cache, an edit is 268 ms and about 85% of that is OpenCascade's
> mesher — work that streaming would *move* rather than remove. Removing it from
> the request means the response cannot carry `mesh_hash`, `volume` or `bbox`,
> all of which are computed *from* the mesh, so every derived reading becomes
> provisional. That is exactly the trap this sprint's own notes flag ("a body
> that is becoming current needs its own state"), and it is a larger change than
> the rest of P2 put together. Doing it properly needs volume and bbox to come
> from the B-rep rather than the mesh — a change to what those numbers *mean* —
> and a `refining` state the store, the STALE rule and the decompiler all
> understand. It is worth its own sprint, not a corner of this one.
>
> **P2's spatial index was kept but is honest about its limits.** A bounding-box
> cull leaves 57 of 300 curves per cell for a circle and 29 for a lobed outline:
> real, but not the "O(samples × local)" the plan assumed, because points deep
> inside a closed profile are genuinely near-equidistant from all of it. The 46×
> on the quoted 27 s case came from *not evaluating the same (u, v) once per
> extrusion slice*, which the plan did not anticipate.


The exact-kernel programme and the sketch programme both went after
*capability*: things the system could not do. This one goes after the two
things that decide whether the capability is usable — how long an edit takes,
and what you can do with your hands.

Same format as the other roadmaps: what to do, how you know it worked, and the
trap that will actually bite. Every number below was measured against `main` on
2026-08-02, and the measurement is quoted so it can be re-run rather than
believed.

## What is already true

| Landed | Where |
|---|---|
| Sketch solve is linear in the sketch and bounded against a DoS | `services/sketch_solver.py` (`_compile`, `check_size`) |
| The exact kernel is tried first and tessellates far better than marching cubes | `evaluator/brep.py` |
| Derived readings are invalidated the moment geometry moves | `useDocumentStore` `STALE` |
| Evaluated solids are cached on the resolved tree's hash | `document_service._SOLID_CACHE` |
| Optimize runs async with a job id the frontend polls | `routers/documents.py` |
| A command palette, and Esc / Delete / F / Ctrl+S | `CommandPalette`, `ToolRibbon`, `DocumentViewport`, `EditorHeader` |

That is a system that computes the right answer. What follows is what it costs
to get one.

---

## Sprint P1 — Stop shipping the part on every edit

**Why now.** This is the largest single number in the system and it is paid on
every interaction. Measured, for a part of three primitives:

```
document JSON                1164 KB
  gzipped (what downloads)    528 KB   <- per edit
  mesh data-URI              1163 KB   (99% of the body)
  decoded binary STL          872 KB   (17,865 triangles)
```

The mesh is base64-inlined into `derived.mesh_url`, so it is part of the
document body. Every operation returns the whole document, so **every edit
re-downloads the entire part**, and the cost grows with the model:

```
three primitive adds, server time and response body
   32 ms      3 KB
  180 ms    108 KB
  326 ms    516 KB
```

Worse, edits that change no geometry pay it too:

```
set_material  90 ms + full mesh re-send
set_spec      85 ms + full mesh re-send
```

Neither touches the solid. Both re-evaluate and re-serialise it.

**What to do.**

1. **Serve the mesh from its own endpoint, keyed by `mesh_hash`.** The document
   carries a URL, not a payload. Because the hash *is* the identity, the
   response can be immutable and cached forever — an edit that does not move
   geometry then costs nothing at all, and an undo back to a shape you have
   already seen is free.
2. **Do not re-evaluate when nothing geometric changed.** `set_material`,
   `set_spec`, `set_tolerances`, rename and lifecycle changes cannot move the
   body. `solid_hash` already exists to prove it.
3. **Send positions, not STL.** Binary STL is unindexed triangle soup carrying a
   per-face normal that three.js recomputes anyway: 872 KB of STL is 628 KB of
   raw float32, and indexing the shared vertices roughly halves it again.
4. **Coarse first, fine after.** `evaluate_document` costs 32 ms at resolution
   64 and 394 ms at 256. Return the coarse body immediately and refine in the
   background, so the viewport moves while the good mesh is still being built.

**How you know it worked.** Change the material on a three-primitive part and
watch the network panel: today 528 KB, after this a couple of kilobytes. Add a
primitive, undo, redo — the mesh transfers once, not three times. And the
`set_material` round trip drops below 10 ms, because it stopped doing geometry.

**Traps.**

- **A hash-keyed URL must really be immutable.** If two different bodies can
  ever produce the same `mesh_hash`, the browser will show the wrong part and
  no amount of reloading will fix it. `mesh_hash` is already computed for the
  decompiler's verification, which is a *stronger* requirement than caching
  needs — but check it covers resolution and sampler, because a body meshed at
  64 and at 256 is the same shape and a different mesh.
- **A data URI needs no server.** `tests/conftest.py` drives `cad_operations.execute`
  directly with no HTTP at all, and `useMeshTriangles` fetches whatever string
  it is handed. A URL has to keep working in both, which argues for the document
  carrying a *relative* URL and the tests serving it from the datastore.
- **`shared/types.ts` mirrors the Pydantic models by hand.** Changing
  `DerivedCache` means editing both, and `mesh_url` is read by
  `DocumentViewport`, `AssemblyViewport` and `PartBackdrop`.
- Do not "fix" this by lowering the default resolution. That trades a bandwidth
  problem for a fidelity one, and the fidelity is the product.

**Size.** Medium. The endpoint is small; the care is all in cache correctness.

---

## Sprint P2 — The evaluation you do not have to do

**Why now.** With the payload fixed, server time is what is left. Measured:

```
evaluate_document, three primitives
  resolution  64     32 ms
  resolution 128    113 ms      <- the default
  resolution 256    394 ms
```

Every mutating operation re-evaluates the **whole tree from the root**. Filleting
the top of a twelve-feature part re-evaluates all twelve. `_SOLID_CACHE` is keyed
on the hash of the entire resolved tree, so any edit anywhere misses it
completely.

Two more, both cheap to fix and both currently paid on every regen:

```
resolved_tree                 4.5 ms   (three separate deepcopies of the tree)
300-curve profile @ res 128    27 s    (loops_sdf is O(curves x samples))
```

That last one is not a regression — `_poly_sdf_2d` has the same shape and is
slower still (4.5 s vs 2.7 s on the same 300 curves) — but it is the reason a
traced outline or a large generated sketch makes a document feel broken.

**What to do.**

1. **Cache per node, not per tree.** Hash each subtree and cache its evaluated
   solid. An edit then re-evaluates the edited node and its ancestors, not its
   siblings. The ids are already deterministic (`IdAllocator`), which is what
   makes a subtree hash stable across a replay.
2. **Give the 2D profile field a spatial index.** A uniform grid over the curves
   turns `min distance to N curves` from O(samples × N) into O(samples × local),
   which is the difference between 27 s and interactive for a traced outline.
   Both `loops_sdf` and `_poly_sdf_2d` want it, and they should share it —
   `geom/profile2d` is already the one place containment is decided.
3. **Collapse the deepcopy chain.** `resolve_suppressed → resolve_sketches →
   resolve_datums` each defensively deep-copy the whole tree. One copy at the
   top of `resolved_tree` and in-place transforms after it is the same contract
   for a third of the cost.
4. **Evaluate off the request.** A 400 ms operation that blocks the response
   feels slower than a 400 ms operation that returns immediately and streams the
   geometry. The optimize job already has the shape to copy.

**How you know it worked.** Build a twelve-feature part and edit the *last*
feature: the evaluation time should be roughly what that one feature costs, not
what the part costs. Extrude a 300-curve traced profile and the document should
evaluate in well under a second. And `resolved_tree` on a tree carrying a
200-point sketch should be nearer 1.5 ms than 4.5 ms.

**Traps.**

- **A subtree hash has to include everything that changes the subtree's
  geometry** — parameters, the active configuration, suppression, and the
  substituted sketch. Miss one and the cache returns a stale body, which is the
  worst failure mode available here because it looks like a working edit.
- **`resolved_tree` is the one place resolution happens**, and it is that way
  because five copies of the sequence had already drifted. Making the transforms
  in-place must not turn it back into something a caller can half-apply.
- The spatial index must not change the field's *values*, only the time taken to
  find them. `test_sketch_profiles` compares both kernels to closed-form volumes
  at 1e-9 and 2e-4 — those tolerances are the guard rail, so run them before
  believing a speedup.
- Streaming evaluation makes "is the geometry current?" a question with a third
  answer. `STALE` in the store clears derived readings when geometry moves; a
  body that is *becoming* current needs its own state, or a stale safety factor
  will be shown next to a fresh mesh.

**Size.** Large. The per-node cache is the substantial piece; the other three
are contained.

---

## Sprint P3 — Give it hands

**Why now.** The modelling surface is complete and the interaction surface is
not. Three concrete gaps, all verifiable in a minute:

- **You cannot click the part.** `DocumentViewport` has no pick handler on the
  body: selection happens only in the feature tree. Clicking a boss to select
  the boss is the first thing anyone tries in a CAD tool.
- **There is no Ctrl+Z.** `undo`/`redo` exist in the store and have buttons, but
  the only keyboard bindings in the editor are `Esc`, `Delete`, `F` and
  `Ctrl+S`. Undo also does a full server round trip and re-evaluation, so it is
  as slow as the edit it reverses.
- **First paint carries everything.** One 1,437 KB bundle (388 KB gzipped), no
  route splitting — the sign-in screen pays for three.js, the slicer, the
  drawing viewer and the sketch canvas. Reload to geometry on screen measured
  2.5 s in dev.

**What to do.**

1. **Pick the body.** Tessellate per leaf feature and tag the triangles, so a
   raycast hit names a feature. This is *not* the face-picking problem: faces
   were left as a list because `Kernel.from_mesh` produces one `Face` with
   `surface=None` and a nearest-entity guess would be right most of the time,
   which is the worst possible accuracy for a selection. A *feature* has no such
   ambiguity — the triangle either came from that feature's solid or it did not.
2. **Drag what you picked.** Move and resize handles on the selected feature,
   previewing locally and committing one operation on release. The whole edit is
   already expressible as `set_feature_params`; what is missing is a way to say
   it with a mouse.
3. **The keyboard a CAD tool is expected to have.** Ctrl+Z / Ctrl+Y / Ctrl+Shift+Z,
   and make undo local: the store already keeps the last 50 trees, so an undo
   that re-uses a cached mesh (P1) and a cached subtree (P2) does not need the
   server at all until the next real edit.
4. **Split the bundle by route.** Sign-in, library, editor and assembly are
   separate screens; the sketch canvas, slicer and drawing viewer are separate
   panels within one. None of them needs to be in the first chunk.

**How you know it worked.** Click a boss in the viewport and the feature tree
selects it. Drag it 10 mm and the inspector shows the new centre. Ctrl+Z puts it
back with no network request. And the sign-in screen downloads well under
100 KB of JavaScript.

**Traps.**

- **Per-feature tessellation is not free**, and it interacts directly with P1:
  N feature meshes are more bytes than one merged mesh, and the merged mesh is
  what the viewport draws. The likely answer is one mesh plus a per-triangle
  feature index, which is a byte per triangle rather than a second copy.
- **A drag is many edits or one.** Committing on every mouse-move would send
  hundreds of operations and pollute the trajectory recording with noise; preview
  locally and commit once on release, or the training corpus learns that moving a
  boss takes 300 calls.
- **Undo that skips the server has to stay honest.** The store's `past` holds
  feature trees, not documents — everything derived (mass, DFM, simulation) must
  still be marked stale, or an undo will restore geometry next to another
  operation's readings.
- Code splitting a screen that the router can reach synchronously produces a
  blank frame. Every lazy boundary needs a fallback that looks deliberate, and
  the editor's is the one users will see most.

**Size.** Large. Picking and dragging are the bulk; the keyboard and the split
are each an afternoon.

---

## Suggested order

**P1 → P2 → P3**, and the dependencies are real rather than tidy.

P1 first because it is the biggest measured number, it is paid on every single
interaction, and it is the least entangled — a mesh endpoint and a "did the
geometry actually change" check touch almost nothing else. It also makes P2's
wins *visible*: shaving 100 ms off evaluation is invisible behind a 528 KB
download.

P2 second because per-node caching is what makes editing a large part feel the
same as editing a small one, and because P3's drag interaction is only pleasant
if the commit at the end of the drag is fast. A drag that commits into a 400 ms
whole-tree rebuild will feel worse than the inspector field it replaced.

P3 last because picking wants P1's mesh transport decided (per-triangle feature
index versus per-feature meshes is a transport question as much as a rendering
one), and because local undo depends on both caches to be worth doing.

**Deferred, and worth saying so:** WebGL instancing for patterns, a worker for
mesh parsing, server-side mesh simplification for the library thumbnails, and
collaborative cursors. None is blocked by the above; all are cheaper once the
mesh has an identity and a URL.

**Not in scope, but do not lose it:** the operation endpoint has no request-body
size limit and no rate limit — only auth is throttled. That is a whole-API
decision (STL import legitimately posts large bodies), not something to fold
into a performance sprint, but it is the one finding from the last review that
is still open.

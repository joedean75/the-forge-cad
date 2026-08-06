# MVP feature roadmap — three sprints

> **Status: M1, M2 and M3 are done.** What landed, measured the same way as the
> numbers below:
>
> | | before | after |
> |---|---|---|
> | Assembly operations in the catalog | 0 of 58 | 9 of 69 |
> | Assembly UI | place a component by typing three numbers | mates, clash and BOM panels |
> | Library card | icon + name + relative time | shaded thumbnail, state, revision, material, mass |
> | Library organise | name substring only | sort ×3, filter ×2, search name/prompt/material |
> | Editor below 1024 px | three squeezed columns | one column with a pane switcher |
> | Operations that export a file | 0 of 58 | 5 formats + the package, all URL-addressed |
> | Drawing formats | SVG, DXF | + PDF, with the text the DXF drops |
> | Handover package | absent | STEP + drawing + BOM + spec report, pinned to a letter |
> | An order's attachments | a process, a quantity and an id | the released package |
>
> **The PDF is written rather than depended on.** The sprint's trap said a
> renderer is a dependency and its absence must be an honest `PRECONDITION`.
> That rule is right for a renderer that must exist; it is the wrong trade for a
> drawing sheet, which is polylines, leaders, hatching and monospaced text on one
> page. `cad_kernel/io/pdf.py` is the whole writer and there is no absent case.
>
> **Two things were found and fixed that the sprints did not ask for.**
> `assembly_service.add_component` accepted any part id, so an assembly could
> hold a document its owner cannot open and read that geometry back out through
> the BOM and the interference check — the check now lives in the service, where
> both callers get it. And `document_service.EPHEMERAL` exists because
> evaluating a revision snapshot as a document would otherwise have persisted a
> phantom row in the library from any of four places in the evaluate path.
>
> **Item 4 of M3 is narrower than it reads.** "Point the quote/order path at the
> package" is done, but an order on a part with no released revision is *not*
> refused — it records why it has no package. Refusing it would invent a process
> requirement no shop asked for.
>
> **Still not in scope, and now the oldest open finding by two roadmaps:** the
> operation endpoint has no request-body size limit and no rate limit. Only auth
> is throttled.

The last four programmes went after the engine: an exact kernel, a sketch
solver, speed and interaction, and an action space a model can drive. The engine
is in good shape. What this one goes after is the product — the places where a
user (or an agent) cannot finish the job, and, more often than is comfortable,
the places where the **backend already can and nothing reaches it**.

That pattern is worth naming, because it is the single largest source of gaps
here. Sprint 10 of the exact-kernel programme existed for the same reason: every
capability was implemented and half of them had no way in.

Same format as the other roadmaps: what to do, how you know it worked, and the
trap that will actually bite. Every number below was measured against `main` on
2026-08-03 and the measurement is quoted so it can be re-run rather than
believed.

## What is already true

| Landed | Where |
|---|---|
| Exact B-rep kernel, edge selection, topological naming | `cad_kernel/evaluator/brep.py`, `core/history.py` |
| Constraint-solved sketches with on-canvas dimensions | `services/sketch_solver.py`, `features/sketch/` |
| Real FEA, topology DFM, design specs with a score | `services/fea.py`, `dfm_topology.py`, `design_spec.py` |
| 58 catalog operations, strict schemas, typed failures, remedies | `services/cad_operations.py`, `op_schema.py`, `op_errors.py` |
| A benchmark, and a surface that refuses the five known bad calls | `evals/`, `services/units.py`, `action_space.py` |
| Mesh as an immutable hash-addressed resource; per-node build cache | `io/mesh_binary.py`, `evaluator/brep._NODE_CACHE` |
| Click-to-select by feature, drag handles, local undo, split bundle | `services/picking.py`, `components/MoveHandles.tsx` |

That is a system that models, verifies and drives well. What follows is what a
user still cannot do with it.

---

## Sprint M1 — An assembly that holds together

**Why now.** Assemblies are the one place where the gap between what the backend
does and what the product offers is total. Measured:

```
backend            services/assembly_mates.solve   fasten | coincident | distance | concentric
                   services/interference.check_assembly   overlap volumes + clearance
                   services/bom.bill_of_materials / bom_csv
routes             POST /assemblies/{id}/mates, DELETE …/mates/{id},
                   GET …/interference, GET …/bom, GET …/bom.csv

frontend           api/client.ts      addMate: absent · interference: absent · bom: absent
                   useAssemblyStore   mates: absent
                   AssemblyScreen     insert, remove, setTransform — hand positioning only
```

So an assembly today is a set of parts you nudge into place with numbers. Nothing
holds them together, nothing tells you they interpenetrate, and nothing lists
what to order. All three are implemented, tested and unreachable.

There is a second half. **Assemblies are absent from the operation catalog
entirely** — 58 operations, not one of which creates an assembly, inserts a
component or adds a mate. The copilot, the MCP server and every agent driving
this system can model a part and cannot compose two.

**What to do.**

1. **Reach the mates.** `addMate` / `removeMate` in the typed `api` object,
   mates in `useAssemblyStore`, and a mate list beside the component list. Four
   types exist; expose exactly those four rather than inventing a fifth in the UI.
2. **Show the clash.** `interference.check_assembly` returns overlapping pairs
   with volumes and a clearance figure. A badge on the assembly and a per-pair
   row is most of the value; the viewport highlight is the nice half.
3. **Show the parts list.** BOM and BOM-CSV both exist. A panel and a download
   button is a morning's work and it is the artefact a purchasing conversation
   actually starts from.
4. **Put assemblies in the catalog**, so an agent can build one. This is the
   expensive item and it should be sized honestly — see the traps.

**How you know it worked.** Insert two plates, mate them concentric on a bore,
and moving the first moves the second. Overlap them and the assembly says so
before you export it. And `evals` grows an assembly task that the copilot can
actually attempt.

**Traps.**

- **A hand transform and a mate both claim to own a component's position.**
  `setTransform` writes the position directly and `solve()` overwrites it. Decide
  which wins *before* building the UI, and make it visible: a component driven by
  a mate must not look draggable, or the user will drag it, see it snap back, and
  conclude the tool is broken. This is the same class of problem as a sketch
  dimension versus a dragged point, and the sketch canvas already answers it by
  showing constraint status rather than hiding it.
- **Mates name bounding-box faces, not topology.** `base_face`/`moved_face` are
  the six extremes — `services/datum.py`'s symbolic scheme, not the addressable
  faces `list_faces` returns. That is fine for plates and will not survive "mate
  to that counterbore". Say so in the UI rather than implying more precision than
  exists, and store the whole descriptor the way sketch-on-face does, so the
  exact form can arrive later without a migration.
- **Interference is sampled, not exact.** `check_assembly` samples surfaces and
  reports an overlap volume at a clearance. Report what it measured — a clash
  check that says "clear" when it means "no sample landed inside" is the silent
  success this codebase keeps having to remove.
- **Assemblies in the catalog need a second document kind in the action space.**
  Every operation's signature is `(doc_id, args)` and every observation is a
  `DesignDocument`. An assembly is neither. Doing this properly means
  `observation.summarize` grows an assembly form and the catalog grows a notion
  of *which* kind of thing an operation applies to — that is the real cost, and
  it is larger than the three UI items put together. It may deserve its own
  sprint; what it must not do is arrive as a special case that only the copilot
  knows about.

**Size.** Medium for items 1–3, large for item 4. Ship 1–3 first; they are
independently valuable and they tell you whether anyone assembles anything.

---

## Sprint M2 — See your work without opening it

**Why now.** The library is where every session starts, and it shows a generic
icon per card. Two parts are indistinguishable until you open one. Measured:

```
LibraryScreen.Card    icon + name + "3 parts" + relative time
                      no thumbnail, no state, no revision, no material, no mass
search                name substring only
sort / filter         none
responsive utilities  16 in the entire frontend  (the editor is three fixed columns)
```

There is a specific reason this is worth doing *now* rather than earlier. A
thumbnail is a picture of a **specific body**, and until P1 the geometry had no
identity to key it on. It does now: `mesh_hash` names exactly those triangles,
and the mesh endpoint already demonstrates the pattern — immutable, cached
forever, 404 on a hash the document does not have.

The cost constraint is real and measured: a one-view drawing render is
**2.9 s**. Whatever produces the thumbnail, it cannot run on library load.

**What to do.**

1. **A thumbnail keyed on `mesh_hash`**, served exactly like the mesh:
   `/api/documents/{id}/thumbnail/{mesh_hash}`, `Cache-Control: private,
   immutable`, 404 rather than "here is the current one instead". Rendered once,
   lazily, and cached.
2. **Say what the card is.** Lifecycle state, revision letter, material and mass
   are all on the document already and none of them reach the card. A released
   part and a draft should not look identical.
3. **Organise.** Sort by name/edited/created, filter by state and by
   shared-with-me, and search the material and the source prompt as well as the
   name — `source_prompt` is on every generated document and is often the only
   thing the user remembers.
4. **Decide what the app does on a narrow screen.** Not "add breakpoints": the
   editor is a three-column layout around a WebGL canvas, and below some width
   the honest answer is one column with a mode switcher, not a squeezed
   viewport.

**How you know it worked.** Open the library with a dozen parts and find the one
you want without reading a single name. And the library still paints instantly
with no thumbnails rendered yet.

**Traps.**

- **A thumbnail that shows the wrong body is worse than an icon**, and it is the
  default outcome of keying it on the document id. Key it on `mesh_hash`.
- **Silhouette or shaded — pick one and mean it.** `services/drawing` already
  projects an outline with no GPU, which is the cheap route; but an outline is
  not what someone scanning a library recognises. A client-side render on save
  (the viewport is already there, the geometry is already loaded) is the other
  option and produces something people actually recognise. Both are defensible;
  what is not is discovering halfway through that the cheap one is unrecognisable.
- **The library must not wait on thumbnails.** Render lazily, fall back to the
  icon, and never block the list on a 2.9 s job — the whole point of P1 was that
  the fast path stops paying for the slow one.
- **A generated thumbnail is a derived artefact and belongs in the LRU, not the
  document.** `_persist` strips `step_url` and gcode for exactly this reason.

**Size.** Medium. The thumbnail endpoint is small and the care is all in cache
keying; the responsive question is the open-ended one and can be scoped down to
"the library and sign-in work on a tablet" without touching the editor.

---

## Sprint M3 — Getting the part out of the building

**Why now.** The product's thesis is model → make. The last step is a file
somebody else can open, and the catalog cannot produce one. Measured:

```
operations that export a file    0 of 58
GET /api/documents/{id}/step     exists — a human can export, an agent cannot
drawing formats                  SVG (a sheet to read), DXF (geometry to edit)
                                 PDF — absent, and it is what a shop receives
a package: STEP + drawing + BOM + spec report, pinned to a revision — absent
```

`release_revision` already cuts an immutable lettered revision, and
`verify_against_spec` already scores the body against what it was supposed to be.
Those are the two halves of a handover and nothing puts them in an envelope.

**What to do.**

1. **`export` in the catalog** — `step | stl | dxf | svg` — returning a URL keyed
   by the content hash, the way the mesh does. This closes the last place where
   the UI can do something an agent cannot.
2. **Drawing → PDF.** SVG is a sheet to read in a browser; DXF is geometry for
   the next tool. Neither is what gets attached to a purchase order.
3. **A release package**: the STEP body, the drawing, the BOM, the spec report
   and the revision letter, as one download, built **from a released revision**
   rather than from the document as it stands.
4. **Point the existing quote/order path at the package**, so "get it made" sends
   the same artefacts a human would have attached.

**How you know it worked.** Release revision A of a part, download the package,
and everything a supplier needs is inside it — including the score against the
spec, which is the thing that says the part is not merely valid but right. And
`export` appears in `GET /api/operations` with a schema.

**Traps.**

- **A package pinned to "now" is not a release**, it is a screenshot of a moving
  target. Build it from the revision snapshot, and if the document has moved on,
  say which letter the package is for rather than silently exporting the current
  body.
- **Do not return the bytes inline.** The operation result carries a URL; the
  payload is fetched separately and cached by hash. Inlining an export is the
  exact mistake P1 spent a sprint undoing, and a STEP file is bigger than the
  mesh was.
- **STEP is written in millimetres with an explicit `SI_UNIT(.MILLI.,.METRE.)`
  context, by both writers.** The faceted one emitted raw metre values with no
  unit context and an 80 mm plate imported as 0.08 mm. Anything new that writes
  a file inherits that requirement, and it is not visible until someone else
  opens the file.
- **A PDF renderer is a dependency.** Every optional dependency here degrades to
  something that still works — OCP to the SDF, the LLM to the rule-based parser,
  Stripe to local self-serve. An export that silently disappears in production is
  worse than one that is honestly unavailable, so the absent case has to be a
  stated `PRECONDITION`, not a 500.
- **A package is a fan-out of slow jobs.** Drawing is 2.9 s, STEP is a build, the
  spec report meshes the body. Build it as a job with an id the client polls —
  the optimize path already has the shape to copy — rather than a request that
  times out at the worst moment.

**Size.** Medium-large. Items 1 and 3 are the substance; the PDF is bounded by
whichever renderer is chosen.

---

## Suggested order

**M1 → M2 → M3**, and the dependencies are weaker than in the other roadmaps,
which is itself worth saying: these are three largely independent product gaps
and the order is about value, not about what unblocks what.

M1 first because it is the largest ratio of shipped capability to reachable
capability anywhere in the system. Three of its four items are wiring, and the
thing they wire up has tests already.

M2 second because it is cheap now and was not cheap before — the whole
thumbnail design falls out of P1's decision to give geometry a content hash, and
doing it earlier would have meant inventing that identity twice.

M3 last because a handover package should package the finished thing, and M1
adds the BOM to what "finished" contains.

**Deferred, and worth saying so:** a standard-parts library (fasteners, bearings,
extrusion profiles) — every real assembly needs screws, and M1 will make that
obvious rather than theoretical; comments and review threads; and an in-app
onboarding tour. All three are real, none is blocking.

**Not in scope, but do not lose it:** the operation endpoint still has no
request-body size limit and no rate limit — only auth is throttled. That has been
carried on two previous roadmaps now. It is a whole-API decision rather than a
feature, and it is the oldest open finding in this repository.

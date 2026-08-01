# Sketch roadmap — three sprints

> **Status: all three landed.** S1 in `Sketch S1: stop flattening a sketch
> before the kernel sees it`, S2 in `Sketch S2: a solver that says what is
> wrong`, S3 across the backend and UI commits that follow. What each one
> actually shipped, and where it differed from the plan, is recorded at the end
> of its section. The tests are `backend/tests/test_sketch_profiles.py`,
> `test_sketch_solver_diagnosis.py` and `test_sketch_feature.py`.

The exact-kernel programme is finished (`SPRINT-ROADMAP.md`). Sketching is the
part of the modelling surface it did not touch, and it is now the weakest link:
the kernel underneath can hold exact arcs, addressable bores and real STEP, and
the sketch pipeline throws all three away before the geometry ever reaches it.

Same format as the sprint roadmap — what to do, how you know it worked, and the
trap that will actually bite. Every claim below was measured against the code on
`main`, and the measurement is quoted so it can be re-run rather than believed.

## What is already true

| Landed | Where |
|---|---|
| Least-squares constraint solver over points + circle radii | `services/sketch_solver.py` |
| 11 constraints: fixed, coincident, horizontal, vertical, distance, parallel, perpendicular, equal_length, radius, point_on_line, arc_radius | `_residuals` |
| Arcs stay circular without an explicit constraint (implicit equal-radius residual) | `_residuals` |
| Loop ordering that tracks traversal direction, so a `b→a` arc still sweeps the right way | `sketch_to_polygon` |
| Sketch-on-face that follows an upstream resize | `services/datum.py` |
| `add_sketch` / `update_sketch`, with the editable spec kept on the feature | `cad_operations` |
| A drawing canvas with line/rect/circle/arc, point snapping and 10 constraint buttons | `frontend/src/features/sketch/SketchCanvas.tsx` |

*(That table is the state before these three sprints. What each added is in its
"What landed" section.)*

That is a real sketcher's skeleton. What follows is what it cannot do.

---

## Sprint S1 — a sketch that survives contact with the kernel

**Why now.** This is a correctness sprint, not a feature sprint, and everything
else waits on it. `sketch_to_polygon` flattens every sketch to
`{"type": "polygon", "points": [...]}` before it reaches either kernel: arcs are
tessellated at 24 segments (`_arc_points(segments=24)`) and circles are dropped
entirely. Three measurements, all on `main`:

```
circle-only sketch  -> SketchError: "sketch has no lines or arcs to form a profile"
rect + one arc      -> 27-point polygon (3 line ends + 24 arc segments)
                    -> extruded on the exact kernel: 29 faces, 0 cylindrical
```

The first is the plainest sketch anyone draws. The third is the expensive one,
because this repo has spent nine sprints making cylindrical faces mean something:

- `design_spec._c_hole` counts **cylindrical faces**, deliberately — "a bore a
  later boolean swallowed is not a hole". A sketched bore is 24 planes, so a
  spec requiring it fails on a part that has it.
- `dfm_topology`'s smallest-bore reads the same faces, and misses it the same way.
- `naming` hands out 24 tiny face ids where there should be one, so `fillet_edges`
  cannot be pointed at "the bore".
- STEP export of that region is 24 planar faces — faceted output from the kernel
  whose entire purpose is not being faceted.

So the sketch is currently the one path that *downgrades* geometry on its way in.

**What to do.** Stop flattening. A profile becomes loops of typed curves rather
than a ring of points:

```
{"type": "loops", "loops": [{"curves": [{"kind": "line", ...},
                                        {"kind": "arc", "center": …, "ccw": …},
                                        {"kind": "circle", "center": …, "r": …}]}]}
```

1. `sketch_solver.sketch_to_loops` replaces `sketch_to_polygon`: walk **every**
   connected loop, not the first; emit circles as their own single-curve loop;
   keep arcs as arcs.
2. `evaluator/sdf.profile_sdf_2d` learns the new type — per-loop signed distance,
   combined outer-minus-inner. The 2D arc/circle distance functions are three
   lines each; the loop combination is the part to get right.
3. `evaluator/brep._profile_wire` learns it too: `GC_MakeArcOfCircle` for arcs,
   `gp_Circ` for circles, and inner loops added to the face with
   `BRepBuilderAPI_MakeFace(outer).Add(inner)` so a hole is a hole rather than a
   second boolean.
4. Nested loops mean **profile with a hole** falls out for free, which is the
   other thing every real sketch needs and today costs two extrudes and a cut.

**How you know it worked.** A circle-only sketch extrudes, and `list_faces`
reports **one** cylindrical face of the right radius. A `hole` spec requirement
is met by a sketched bore. A rect-with-a-fillet extrudes to 6 planes + 1 cylinder
rather than 29 planes. And the two kernels agree: the SDF and B-rep volumes of a
loops profile match to the tolerance `tests/test_brep_kernel.py` already uses for
every other feature.

**Traps.**

- **Existing documents are persisted.** `{"type": "polygon"}` must keep meaning
  exactly what it means today, tessellation and all — a document that evaluates
  to a 24-gon now must not silently become a true arc, for the same reason
  `LoftFeature` was left alone when `RuledLoftFeature` was added.
- **Both kernels or neither.** A loops profile that only the B-rep path
  understands makes a document evaluate to two different shapes depending on
  whether OCP is installed, which is the failure the `LoftFeature` note in
  `CLAUDE.md` exists to prevent.
- **Loop orientation decides inside from outside.** An inner loop wound the same
  way as its outer is a second boundary, not a hole; normalise winding rather
  than trusting the order the user drew in.
- The canvas previews arcs at 32 segments and the kernel builds 24, so what you
  see is already not quite what you get. Once arcs are exact the preview should
  stop being the more accurate of the two.

**Size.** Large. Three representations have to agree — solver output, SDF, B-rep.

### What landed

As planned, plus two things the plan did not anticipate.

Containment turned out to want a module of its own — `cad_kernel/geom/profile2d.py`
— rather than a branch in each evaluator. Even-odd parity is computed *there and
only there*, and both `evaluator/sdf.profile_sdf_2d` and `evaluator/brep._loops_faces`
read it, because two implementations of "which loop is a hole" is exactly the
divergence the sprint was meant to prevent. Nesting therefore works to any depth
and an island inside a bore falls out as its own face.

`_loop_wire` takes a *placement function* rather than a plane name, so revolve
and ruled loft build from loops too — the plan only covered extrude, and leaving
revolve behind would have made S3's "hand a sketch to any of them" impossible.

Measured across four profiles (circle-only, plate + bore, island-in-bore, rounded
corner): the exact kernel matches the closed-form volume to **1e-9 relative** and
the distance field to **2e-4**. A rounded corner is 6 planes and 1 cylinder, not
29 planes. A sketched Ø20 bore satisfies a `hole` requirement and its two rims
fillet by name.

One bug found on the way that the plan had no reason to predict: `/api/sketch/solve`
declared no `arcs` field, so a sketch containing an arc solved over HTTP as if
the arc were not there.

---

## Sprint S2 — a solver that says what is wrong

**Why now.** The solver currently reports success for the most common failure.
Measured:

```
under-constrained (4 free DOF, no constraints) -> converged: True,  residual: 0.0
two conflicting distances on one line          -> converged: False, residual: 0.0106
                                                  (names neither constraint)
```

`converged` is `residual < 1e-6`, and an unconstrained sketch satisfies that
trivially — it is the same class of bug as the hardcoded `ok: True` the operation
envelope was built to kill. For an action space meant to be machine-checkable,
"could a model tell from the response alone whether it succeeded, why not, and
what changed?" is currently answered *no* for sketches.

The second half is the missing constraints. There is no **tangent** — the one a
rounded slot, a filleted corner and a D-profile all need — nor concentric,
symmetric, angle, midpoint, or equal_radius.

**What to do.**

1. Replace the boolean with a **status**: `well_constrained` /
   `under_constrained` / `over_constrained` / `conflicting`, plus `dof` (free
   variables) and `redundant` counts. `least_squares` already returns `result.jac`
   at the solution; the rank of that Jacobian versus the variable count is the
   whole diagnosis — rank deficiency is redundancy, full rank with a large
   residual is conflict.
2. **Name the culprits.** For a conflict, report the constraints whose residual
   rows are large; for redundancy, the ones spanning the Jacobian's null space.
   "distance L1=50 and distance L1=80 cannot both hold" is a fixable message;
   "residual 0.0106" is not.
3. Add the missing constraints: **tangent** (line↔arc and arc↔arc), concentric,
   symmetric about a line, angle between lines, midpoint, equal_radius,
   point_on_circle. Each is one residual function in `_residuals`.
4. Surface all of it: `add_sketch`/`update_sketch` return the status and DOF, and
   an over-constrained sketch is a typed `BAD_ARG` naming the constraints rather
   than a quietly wrong body.

**How you know it worked.** Three sketches with known answers: a rectangle with
width, height and one corner fixed reports `well_constrained, dof: 0`; the same
rectangle without the corner fixed reports `under_constrained, dof: 2`; the same
rectangle with both diagonals dimensioned reports `over_constrained` and names
the diagonal. And a rounded slot — two lines and two arcs, tangent at all four
joints — solves to a residual under 1e-9, which is the test that tangent actually
works rather than merely existing.

**Traps.**

- **Do not let the status break the existing contract.** `add_sketch` returns
  `converged`/`residual` today and things read them; keep both and add alongside.
- **Under-constrained is not an error.** A sketch is under-constrained for most
  of the time a person is drawing it, and refusing to solve one would make the
  canvas unusable. It is a *reported state*, and only `over_constrained` with a
  genuine conflict should refuse.
- Rank has to be computed with a tolerance scaled to the sketch, not an absolute
  one: a sketch in metres and the same sketch in millimetres must diagnose the
  same, and the kernel works in metres while the canvas works in mm.
- Tangency between two arcs has two solutions (internal and external). Pick one,
  document which, and let the sign of the residual carry the other.

**Size.** Medium. The constraints are small; the diagnosis is the work.

### What landed

All four items, and the three reference sketches report exactly what the plan
predicted: `well_constrained, dof 0`; `under_constrained, dof 2`; and
`over_constrained` naming both diagonals. The rounded slot solves to residual
**0.0** with all four tangencies satisfied.

Two corrections to the plan's design:

- **Redundancy names a group, not a culprit.** The plan said "report the ones
  spanning the Jacobian's null space", which it does — but the honest reading of
  that answer is "these five constraints carry four constraints' worth of
  information", not "constraint #7 is the extra one". Every member of a
  dependent cycle is individually removable, so nominating one would be an
  invention. The group is ordered by null-space weight and the constraint that
  created the cycle leads it, which is as close to naming a culprit as the maths
  allows. A *conflict* is different and is named exactly, by residual.
- **The rank tolerance is applied to a row-normalised Jacobian.** Scaling it
  "relative to the sketch" is not enough on its own: residual rows are not
  commensurate (`parallel` is a cross product, `distance` is a length, `angle`
  is radians), so a metre-to-millimetre rescale moves them by different factors.
  Row normalisation removes that and cannot change the row space. Metres,
  millimetres and micrometres now diagnose identically, by test.

Two things fixed along the way. `fixed` defaulted a missing axis to the point's
*live* coordinate, giving a residual that is identically zero with a zero
derivative — it pinned nothing and still counted as an equation, so pinning only
`y` reported a phantom redundancy. And `least_squares` ran at its default 1e-8
tolerances, which left an under-constrained solve sitting around 5e-9 — an order
of magnitude from the 1e-6 `converged` claims. These systems are a few dozen
unknowns, so 1e-14 costs nothing.

Extra: a `check_sketch` query re-solves a saved or unsaved sketch and changes
nothing, so an agent can ask "is this sketch sound?" without committing an edit.

---

## Sprint S3 — a sketch that is a thing, not an argument

**Why now.** A sketch today exists only inside `ExtrudeFeature.params.sketch`. It
cannot be shared, cannot exist unconsumed, cannot be revolved or swept, and
cannot be placed on any face the exact kernel knows about. Concretely:

- `add_revolve`, `add_sweep` and `add_loft` all take raw `profile` dicts. There is
  no way to hand a constrained sketch to any of them, so the solver's output
  drives exactly one operation.
- Two features cannot share a profile. Editing "the same" outline in two places
  means editing it twice, and nothing notices when they drift.
- `datum.FACE_MAP` offers six faces — top/bottom/left/right/front/back — resolved
  from bounding-box extremes. Sprint 8 gave every face a stable id and a tag that
  survives re-topologising, and a sketch cannot be placed on one.

**What to do.**

1. A **`SketchFeature`** in the tree: the spec, the plane or face reference, and
   the solved result. It evaluates to nothing on its own — it is geometry other
   features consume, which the tree already tolerates (`resolve_suppressed`
   passes a childless modifier through).
2. `add_extrude` / `add_revolve` / `add_sweep` / `add_loft` accept
   `sketch: <feature_id>` in place of `profile`. One sketch, several features, and
   editing it updates all of them — which is the whole point of a sketch being a
   feature.
3. **Sketch on a real face.** `on_face` accepts a face id from `list_faces`
   alongside the existing `{feature_id, face}` symbolic form. Resolve it through
   `naming.resolve_references` so it follows a re-topologising edit and reports
   `deleted` when the face is gone — exactly as `fillet_edges` does now. The datum
   path stays for bodies with no exact form; it is the only thing that works there.
4. The canvas catches up: DOF and status readout, dimensions drawn on the sketch
   rather than listed in a sidebar, buttons for the S2 constraints, construction
   geometry, and trim/extend/offset/mirror.

**How you know it worked.** Draw one profile, extrude it *and* revolve it, change
one dimension, and both bodies move. Place a sketch on a face id, cut a slot
across that face upstream, and the sketch reports `split` or `deleted` rather than
landing somewhere plausible. And the bar the UI sprint used: do all of it without
touching the API by hand.

**Traps.**

- **`decompiler` has to recover a shared sketch**, or every document using one
  drops out of the training corpus — and `corpus_report` will say so, which is
  what it is for. A sketch consumed twice decompiles to *one* `add_sketch` and two
  references, not two sketches, or the replayed tree will not hash-match.
- Feature ids are deterministic (`IdAllocator`); a sketch id must be too, or
  trajectory replay breaks.
- A sketch with no consumer must not be pruned as dead geometry, and must not
  contribute to the body. Check what `resolve_suppressed` and the boolean
  single-child collapse do to a childless node before relying on either.
- `shared/types.ts` mirrors the Pydantic models by hand. A new feature type and a
  new profile shape means editing both.

**Size.** Large, but genuinely incremental — the feature, the consumers, the face
binding and the canvas each ship on their own.

### What landed

All four items. `SketchFeature` is in the tree and is *substituted and pruned*
before evaluation (`feature_ops.resolve_sketches`), which is what let every
downstream consumer — booleans, the mesher, the bounding box, DFM, the drawing
projector — go on seeing exactly the tree shape it always saw. The trap the plan
flagged about `resolve_suppressed` and single-child collapse was real, and the
answer was to reuse the same prune-and-collapse walk rather than teach every
consumer about a bodiless node.

Three differences from the plan:

- **`add_sweep` was left out.** It has no profile to give it — `SweepSDF` is a
  union of capsules along a 3D path, so `sketch:` would have nothing to bind to.
- **The face reference stores the whole descriptor, not the id.** The plan said
  "`on_face` accepts a face id from `list_faces`", and the first version did
  exactly that — and lost the face on every resize, because an id is a hash of
  the face's current measurements. `_selected` had already learned this for
  fillet edges; the sketch datum now stores what that stores.
- **`list_faces` grew a `feature` argument.** A sketch is placed on a face of
  *the feature it names*, matching the symbolic path's scoping, and a sub-body's
  face ids are its own — so there had to be a way to list them. Scoping it to a
  feature is also what keeps a sketch's own extrusion out of the datum it sits
  on.

The decompiler emits sketches up front, once each, with consumers referencing
the step, so a shared sketch survives into the corpus as one `create_sketch` and
two references.

On the canvas: a status/DOF chip in the header, the eight new constraints as
buttons, dimensions drawn *on* the sketch with click-to-edit figures,
construction geometry (dashed, solved, excluded from the profile), and
trim/extend/offset/mirror. A "Sketch only" toggle saves a reusable
`SketchFeature` instead of extruding, and with one selected the Extrude/Revolve/
Loft tools build from it. Verified end to end in a browser: one sketch driving an
extrude *and* a revolve, then one dimension changed on the canvas moving both.

Trim and extend edit the *drawing*, not the constraint system — a constraint
naming an entity a trim replaced is dropped rather than left to fail in the
solver. Re-anchoring constraints through a trim needs the solver to own the
geometry rather than the canvas, and that is a larger change than this sprint.

---

## Suggested order

**S1 → S2 → S3**, and the dependencies are real rather than tidy.

S1 first because it is the only one that is a *correctness* problem: the sketch
path silently downgrades geometry the rest of the system has been carefully built
to keep exact, and every measurement that depends on a cylindrical face is wrong
for sketched parts today. It also has no prerequisites.

S2 second because tangency is worth little until arcs survive S1 — a tangent
constraint whose arc becomes 24 line segments buys a nicer solve and the same
faceted body. The diagnostics half could go first, and if only one thing gets
done it should be the status field, because a solver reporting success on an
unconstrained sketch is the sort of thing a model learns to exploit.

S3 last because it has to be built against a settled profile representation (S1)
and a solver whose answers can be trusted (S2), and because binding sketches to
face ids is the one part that would need revisiting if either changed.

**Deferred, and worth saying so:** splines, sketch patterns, external references
between sketches, and driven (reference) dimensions. None of them is blocked by
the above; all of them are more useful once a sketch is a first-class object.

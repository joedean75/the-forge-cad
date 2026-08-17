# Where we lose to Onshape, Fusion 360 and SolidWorks

An honest audit, written against the code as it stands. Competitor capabilities
are from their current shipping documentation and 2026 release notes.

> **Superseded as a plan, still accurate as an audit.** The product is now aimed
> at being an **internal tool for RC, drone and robotics work** rather than a
> general-purpose CAD product competing for seats. This document is still right
> about *what is missing*; it is no longer right about **which of it matters** —
> surfacing in particular drops a long way, and a whole column of things a CAD
> competitor is never judged on (vehicle mass budget, simulator export, motors
> and flight controllers in the catalog) rises above it. The current plan is
> [`roadmaps/RC-DRONE-ROBOTICS.md`](roadmaps/RC-DRONE-ROBOTICS.md).

The point of this document is not a feature-count contest — we would lose one, and
losing it does not tell us anything. The point is to separate **gaps that make a
real part impossible to finish** from **gaps that are only conspicuous**, because
those get funded differently.

---

## 0. Read this first: what we are actually selling

Every one of the three competitors is a *human-driven* modeller with thirty years
of feature depth and an ecosystem attached. We are an *agent-driven* modeller with
a machine-checkable action space. Those are different products that happen to both
produce solids.

That framing decides which gaps are fatal:

- A gap in **what geometry can exist** is fatal. If the part cannot be built, no
  amount of agent ergonomics matters.
- A gap in **how a human drives the geometry** is survivable, because the human is
  not our primary driver.
- A gap in **ecosystem, PDM, and interop** is a go-to-market problem, not a
  product problem, until someone tries to put us in an existing shop.

Roughly: **Tier 1 blocks parts. Tier 2 blocks adoption. Tier 3 we should not
close.**

---

## 1. Scorecard

| Area | Us | Onshape | Fusion 360 | SolidWorks |
|---|---|---|---|---|
| Solid modelling (extrude/revolve/sweep/loft/boolean) | ✅ exact via OCC | ✅ | ✅ | ✅ |
| Fillet / chamfer / shell on real edges & faces | ✅ (`ops/exact.py`) | ✅ | ✅ | ✅ |
| Sketch solver + diagnostics | ✅ 18 constraints, named conflicts | ✅ | ✅ | ✅ |
| Parametric expressions | ✅ | ✅ | ✅ | ✅ |
| Configurations | ✅ (`set_configurations`) | ✅ | ✅ | ✅ |
| Sheet metal + flat pattern | ✅ folded↔flat | ✅ (incl. lofted) | ✅ | ✅ |
| **NURBS surfacing / freeform** | ❌ **none** | ✅ + G0–G3 analysis | ✅ + T-splines | ✅ |
| **Draft, rib, thread, helix, dome, wrap** | ❌ **none** | ✅ | ✅ | ✅ |
| Hole types (cbore/csink/tapped) | ❌ plain cylinder only | ✅ | ✅ | ✅ |
| Patterns | ⚠️ linear + circular only | ✅ incl. curve/table/geometry | ✅ | ✅ |
| **Assembly mates** | ⚠️ **4 types, no mate connectors** | ✅ mate connectors | ✅ joints | ✅ 12+ mates |
| Motion / DOF / kinematics | ❌ | ✅ | ✅ | ✅ |
| **In-context / top-down design** | ❌ | ✅ Part Studios | ✅ | ✅ |
| Drawings | ⚠️ 6 ortho + sections, no iso/detail/aux | ✅ | ✅ | ✅ |
| GD&T | ✅ ISO 286 / 2768 + FCFs | ✅ | ✅ | ✅ |
| FEA | ⚠️ **linear static only** | via partners | ✅ + nonlinear | ✅ Premium |
| Modal / thermal / buckling / CFD | ❌ | ❌ native | ✅ | ✅ |
| CAM | ⚠️ 2-axis profile + nesting + posts | ❌ native | ✅ 3/5-axis, turning | ✅ |
| Topology optimisation | ✅ real 3D SIMP, editable result | ❌ | ✅ | ✅ |
| Additive: slice, DFM, quote, print | ✅ end to end | ⚠️ | ✅ | ⚠️ |
| Interop | ⚠️ STEP/IGES + meshes | ✅ + Parasolid/JT/native | ✅ | ✅ |
| PDM / branching / release | ⚠️ revisions + release packages | ✅ branch & merge | ✅ | ✅ PDM |
| Real-time collaboration | ⚠️ presence, single instance | ✅ | ✅ | ⚠️ |
| Custom features / scripting | ✅ **the whole catalog is the API** | ✅ FeatureScript | ⚠️ Python API | ✅ add-ins |
| **Agent-native action space** | ✅ **unmatched** | ❌ | ⚠️ GIE (closed) | ❌ |

---

## 2. Tier 1 — gaps that make real parts impossible

### 2.1 There is no surfacing. At all.

We have no `Surface`, `Knit`, `Trim`, `Extend`, `Offset Surface`, `Fill`, `Boundary`
or `Thicken` feature. Every body in this system is a solid from the first
operation to the last.

This is the single largest hole, and it is not a "nice to have" — it is the
difference between *mechanical parts* and *products*. Anything with a moulded
enclosure, a handle, an ergonomic grip, an aerodynamic fairing, an automotive
Class-A surface, or a consumer housing is built surface-first and thickened. We
cannot represent the intermediate state where the geometry is a sheet.

Onshape ships this with **Reflection analysis** (a zebra-stripe check against 11
environment maps) and **G0/G1/G2/G3 connection analysis** specifically so a
designer can prove curvature continuity across a seam. Fusion adds T-spline
freeform on top. We have no vocabulary for any of it.

Mitigating fact: OCC already has the geometry (`BRepOffsetAPI_ThruSections`,
`BRepBuilderAPI_Sewing`, `BRepOffset_MakeOffset`). The kernel can do this; we
never exposed it. The blocker is that `brep_from_feature` returns a solid or
nothing, and the whole `Kernel.evaluate_feature` contract, `geometry_check`,
`mass_properties` and the SDF fallback assume closed volumes. A sheet body has
no signed distance field and no volume — so this is a **kernel contract change**,
not a feature addition. That is why it is expensive and why it should still be
first.

### 2.2 Mates are four keywords where competitors have a coordinate system

`assembly_mates.py` resolves exactly `fasten`, `coincident`, `concentric`,
`distance`. There is no revolute, slider, cylindrical, planar, ball, pin-slot,
gear, cam, width, symmetric, or path mate. There is no DOF accounting, no motion,
and no drag-to-position that respects constraints.

The deeper gap is structural, not a count. Onshape's **Mate Connector** is an
addressable coordinate frame that can be placed in a Part Studio, patterned,
mirrored, and referenced from anywhere. Fusion's Joints are the same idea. Ours
takes geometry references directly, so there is nothing to place, nothing to
pattern, and nothing to reuse. Adding eight more mate types to the current scheme
would be adding rows to the wrong table.

Consequence: we can hold a bolted stack together and detect interference. We
cannot represent a mechanism. Any assembly with a moving part is out of scope.

### 2.3 The everyday part features nobody notices until they are missing

| Missing | Why it bites |
|---|---|
| **Draft** | Every moulded and cast part needs it. `dfm_topology` *measures* draft and then cannot add it — we diagnose a disease we have no treatment for. |
| **Rib / web** | The standard way to stiffen a plastic or cast part. Currently: hand-built as thin boxes unioned in, with no thickness/draft relationship maintained. |
| **Thread** | `parts_catalog` explicitly ships fasteners as `fidelity: as-standard` with *no thread helix* because we cannot model one. Correct call, real limitation — no thread study, no tapped hole, no thread callout driven by geometry. |
| **Helix / coil / spring** | No sweep along a helical path. Springs, augers, worm gears all impossible. |
| **Hole types** | `HoleFeature` is `radius`, `depth`, `center`, `axis`. No counterbore, countersink, tapped, drill point angle, or through-all-to-face. `clearance_hole` gets the ISO 273 diameter right and then drills a plain cylinder. This is a **cheap** gap to close and it is embarrassing. |
| **Variable / face fillet** | `EdgeFilletFeature` is one radius per call. No variable-radius, face-to-face, or full-round blends. |
| **Pattern types** | Linear and circular only. No pattern along a curve, table-driven, sketch-driven, or geometry-pattern. |

None of these is architecturally hard — OCC has `BRepOffsetAPI_DraftAngle`,
`BRepFilletAPI_MakeFillet::SetRadius(law)`, `GeomFill_Pipe`. They are absent
because nothing forced them. Together they are probably the highest
value-per-week work available.

### 2.4 No in-context or top-down design

Onshape's Part Studio lets several parts be modelled together in one sketch-driven
context; SolidWorks and Fusion do the same with in-context references and
master-model layouts. It is how anything with mating halves gets designed —
enclosure top and bottom sharing one profile, a bracket built to the shape of the
thing it bolts to.

Our unit is one document, one feature tree, one body set. `bodies` can report
several disjoint solids and the spec can declare the count, but there is no way to
say "this face of part B is derived from that face of part A" and have it update.
The assembly holds *references to finished documents*, which is bottom-up only.

### 2.5 Ceilings that a real part will hit

| Limit | Value | Where | Verdict |
|---|---|---|---|
| Sketch entities / constraints | 600 / 600 | `sketch_solver.py:408` | Defensible — it is a DoS bound on an O(rows²·vars) SVD, documented as such. A traced logo or a real weldment layout will still hit it. |
| Text-to-assembly parts | 6 | `text_to_assembly` | Low ceiling for the marquee demo. |
| FEA elements (default) | 8 000 | `fea.py:52` | Fine for a bracket, coarse for anything with a fillet that matters. |
| Resolution cap by plan | 64 / 192 / 256 | `capped_resolution` | Only affects SDF paths; exact bodies are unaffected. Correct design, but a free-tier user optimising a part gets visibly blockier geometry than a paying one. |
| Import file size | 50 MB | `import_service.py:42` | A real supplier assembly STEP exceeds this routinely. |

None of these is *wrong*. They are all bounds someone will hit on their first
serious part, and each currently fails as a refusal rather than as a degrade.

---

## 3. Tier 2 — gaps that block adoption, not parts

### 3.1 Simulation is one solver deep

`fea.py` is a genuine 8-node hex linear-static solve, patch-loaded, mesh-converged,
honest about its discretisation, and it refuses to return an unconverged answer.
That is better engineering than most of what ships. It is also *the only analysis
we have*.

Absent: modal / natural frequency, thermal, thermal-stress, buckling, nonlinear
(large deflection, contact, plasticity), fatigue, drop test, CFD, injection-mould
flow. SolidWorks Simulation Premium ships nonlinear + dynamic response +
composites; Fusion 2026 added real-time physics *inside* the modelling
environment.

For our wedge this is survivable — a linear static stress and a safety factor is a
usable reward signal for an optimising agent, which is what we need it for. It is
not survivable for "replace my SolidWorks seat".

### 3.2 CAM is 2-axis

We have `cam/toolpath.py`, `cam/profile_cut.py`, `cam/nesting.py` and
`cam/post.py` — profile cutting, nesting, posts. That is laser/waterjet/router
territory. There is no 3-axis roughing or finishing, no turning, no mill-turn, no
5-axis, no tool library, no simulation of the cut. Fusion's CAM is the reason many
shops buy Fusion at all, and their 2026 release added automated deburring and
5-axis tilting.

We should probably never chase this. See Tier 3.

### 3.3 No PDM, no branching, no multi-instance collaboration

We have real pieces: `release_revision` (letters), `set_lifecycle_state`,
`release_package` (a zip built from the *revision's* tree, not from "now", which
is the detail most tools get wrong), and `realtime.py` presence broadcasts.

We do not have: branch and merge (Onshape's headline collaboration feature),
where-used, check-in/check-out, ECO workflow, approvals, or a vault. And
`realtime.py` is explicitly single-instance — the room is a process-local dict, so
the moment we run two workers, two co-editors can land in different rooms and see
nothing of each other. That is a scaling bug waiting in production, documented in
`DEPLOY.md` alongside the rate limiter's identical problem.

### 3.4 Interop stops at STEP

STEP and IGES come in exactly via OCC — genuinely exact, addressable faces, real
`mass_properties`. Meshes voxelise. That covers the neutral-format world honestly.

What is missing is everything native: Parasolid (`.x_t`), JT, SolidWorks
(`.sldprt`/`.sldasm`), Inventor, Creo, CATIA, and DWG. A shop evaluating us cannot
open its own library. Some of these are licensable, none are free, and
`.sldprt` in particular is the practical barrier to displacing SolidWorks.

### 3.5 No add-in ecosystem, and no reason to have one yet

SolidWorks has decades of third-party add-ins; Onshape has FeatureScript for
user-authored custom features with bend tables and the rest.

Our equivalent already exists and is arguably better factored: the operation
catalog is strict-schema'd, validated before dispatch, self-describing over HTTP,
and drives the UI and an agent through the identical path. What is missing is
**third-party authorship** — nobody outside this repo can add an operation. That
is a plugin-loading problem, not a design problem, and it is worth nothing until
there are users.

---

## 4. Tier 3 — gaps we should deliberately not close

Chasing these is how a focused product becomes a bad copy of a mature one.

- **5-axis CAM.** Fusion has a fifteen-year head start and shops already own it.
- **Full simulation suite.** CFD and mould-flow are separate companies' entire
  products.
- **Native format import.** Licensing cost and a permanent reverse-engineering
  treadmill, for parity we would never reach.
- **Certification programmes, training, PDM vaults.** These are enterprise sales
  machinery, not software.
- **Rendering / visualisation.** `thumbnail.py` paints a shaded three-quarter view
  in numpy with no GPU. That is exactly enough. KeyShot exists.

---

## 5. Where we are genuinely ahead

Worth stating plainly, because the gap list above is long and the asymmetry is the
whole thesis.

1. **The action space is machine-checkable, and theirs are not.** Every operation
   publishes strict JSON Schema, validates before the handler runs, fails with a
   typed code *and a remedy*, and returns a delta saying what changed. Onshape has
   an excellent REST API; it is not a validated action space with a reward
   function attached. Fusion's Generative Intelligence Engine is closed.
2. **The decompiler.** We can take any feature tree, recover the operation
   sequence that builds it, replay it, and compare mesh hashes — keeping only
   exact matches. That turns existing geometry into verified training
   demonstrations. Nobody else has this because nobody else needs it.
3. **Topology optimisation that stays editable.** SIMP folds back into the tree as
   an `OptimizeFeature`, with `set_keep` protecting bosses and bolt-bearing walls
   and a post-solve audit reporting anything it ate. Most tools hand back a mesh
   you re-model by hand.
4. **Topological naming that survives a rebuild, with honest failure.** Tags first,
   normalised descriptors second, and a reference that cannot be matched is
   reported `unresolved` rather than snapped to the nearest candidate. Every major
   CAD system has a topological-naming problem; ours at least refuses to lie about
   it.
5. **Additive end to end.** DFM measured on the B-rep (not on the render mesh),
   slice, quote, and print to a Moonraker machine. Fusion is the only one close,
   and it does not measure DFM on the exact body.
6. **A spec is a reward function.** `verify_against_spec` returns a fraction of
   measurable requirements met, and refuses to score anything it cannot measure.
   That single property is what makes automated design iteration possible.

---

## 6. What I would do, in order

Ranked by (parts unblocked) ÷ (weeks), not by scorecard optics.

1. **Hole types.** Counterbore, countersink, tapped, through-all. Days, not weeks
   — OCC has it, `clearance_hole` already computes the right diameters, and it
   removes a daily annoyance.
2. **Draft, rib, variable fillet.** All three are OCC calls we never wired up.
   Draft in particular closes the loop with the DFM report that already measures
   it.
3. **Mate connectors, then the mate types.** Build the addressable frame first;
   revolute/slider/cylindrical/planar fall out of it. Doing it the other way round
   means writing the mate types twice.
4. **Helix + thread.** Unblocks springs and real fastener geometry, and lets
   `parts_catalog` offer a third fidelity level honestly.
5. **Surfacing.** The big one. Sheet bodies through the whole contract:
   `brep_from_feature`, `geometry_check`, `mass_properties`, the SDF fallback,
   observation. Budget it as a kernel change, expect a quarter, and do it after
   1–4 have bought the credibility.
6. **Move `realtime` and `rate_limit` off process-local state.** Small, and it is
   currently the thing standing between us and a second worker.

Deliberately not on this list: CAM, simulation breadth, native import, PDM. Those
are Tier 2 and Tier 3, and they are how the roadmap gets eaten.

> On CAM specifically: NVIDIA's Newton physics engine was investigated as a way
> to add machining simulation and declined —
> [`CAM-SIMULATION-SOURCING.md`](CAM-SIMULATION-SOURCING.md). Material removal is
> a geometry problem, not a dynamics one, and the thing worth building is a
> toolpath *verifier* over the SDF `cam/toolpath.py` already samples.

---

## 7. The one-sentence version

We are missing the *surface* half of a modern CAD kernel, the *mechanism* half of
an assembly system, and about eight everyday part features that OCC would give us
almost for free — and in exchange we have the only CAD action space a model can
actually be trained against.

# Tier 1 roadmap — eight sprints against the competitive gaps

Source: [`docs/COMPETITIVE-GAPS.md`](../COMPETITIVE-GAPS.md). Tier 1 is the set
of gaps that make real parts **impossible to build** — not merely awkward. This
file is the order those get closed in, and what "done" means for each.

## The order, and why it is this order

Ranked by *parts unblocked ÷ weeks*, not by how conspicuous the gap is on a
feature matrix. Three constraints shaped it:

1. **Cheap OCC wiring first.** Sprints 1–4 are almost entirely
   `BRepPrimAPI_*` / `BRepOffsetAPI_*` calls the kernel can already make and we
   never exposed. They buy the most buildable parts per week of anything here,
   and they buy credibility for the expensive one.
2. **Build the frame before the things that stand on it.** Mate connectors
   (S5) come before the mate types, because revolute/slider/cylindrical/planar
   all *are* a pair of frames plus which DOF survive. The other order means
   writing every mate type twice.
3. **The kernel contract change last but one.** Surfacing (S7) is not a feature
   addition — it is teaching the whole system that a body may not be a closed
   solid. Doing it first would mean every subsequent sprint lands on shifting
   ground; doing it last would mean never.

| # | Sprint | Gap | Size | Risk |
|---|---|---|---|---|
| 1 | Hole types | §2.3 | S | low |
| 2 | Draft, rib, variable fillet | §2.3 | M | low |
| 3 | Helix and thread | §2.3 | M | medium |
| 4 | Pattern types | §2.3 | S | low |
| 5 | Mate connectors and mate types | §2.2 | L | medium |
| 6 | Ceilings that degrade | §2.5 | S | low |
| 7 | Surfacing — sheet bodies | §2.1 | XL | high |
| 8 | In-context / top-down design | §2.4 | L | high |

Sprints 6 is deliberately placed after 5 rather than first: several of the
ceilings (assembly part count in particular) only become worth raising once
there is something interesting to put in an assembly.

## Rules that apply to every sprint here

These are the house rules from `CLAUDE.md`, restated because each is a way one
of these sprints could go quietly wrong:

- **Two edits per feature type**, both data: the class under `@register_feature`
  in `ops/`, and one line in `_SDF_BUILDERS`. `test_registries` fails on one
  without the other.
- **One edit per operation**: `@operation(...)` in `services/cad_operations.py`.
  Fetch arguments with `op_errors.require`, never `args[...]`.
- **A new parameter name must be classified** in `services/units.py` — either a
  unit in `UNIT_BY_NAME` or membership of `UNITLESS`. `test_action_space` fails
  otherwise, which is the point: a length that carries no unit is the failure
  this system exists to prevent.
- **Exact-only features raise rather than approximate.** If a feature has no
  honest SDF form, it goes in `EXACT_ONLY` and its `_SDF_BUILDERS` entry raises
  a named error. A silently different body is worse than a refusal.
- **The decompiler must round-trip it.** A feature type the decompiler cannot
  emit drags every document containing it to a 0% rate and takes its neighbours
  with it (`blocking` in `corpus_report`). Adding a feature means adding its
  `decompiler` mapping in the same change.
- **A default is the answer the caller would have chosen.** Dimensional defaults
  are fine. Inventing *geometry* nobody named is not — see `_one_of`.

---

## S1 — Hole types

**Gap.** `HoleFeature` is `radius`, `depth`, `center`, `axis`. No counterbore,
countersink, tapped, drill-point angle, or through-all. `clearance_hole`
computes the correct ISO 273 diameter and then drills a plain cylinder;
`counterbore: true` bolts on a second unrelated cylinder feature.

**Scope.**
- `HoleFeature` gains `hole_type` (`simple` | `counterbore` | `countersink`),
  the recess dimensions, `through`, `tip_angle`, and a cosmetic `tapped` +
  `thread` designation.
- Both kernels build it: `brep._hole` composes the tool from cylinders and a
  cone; `_sdf_hole` composes the same tool from `CylinderSDF`/`ConeSDF`.
- A first-class `add_hole` operation, and `clearance_hole` re-expressed as one
  `HoleFeature` rather than two stacked primitives.
- Decompiler mapping; `design_spec._c_hole` and `dfm_topology` unaffected
  because both read the *body*, not the feature.

**Exit.** A counterbored M6 hole is one feature; `list_features` shows one row;
the decompiler round-trips it; both kernels agree on volume to 0.5%.

**Risk.** Low. The one real trap is the drill tip: a `through` hole must not
get one, and a blind hole's stated `depth` is to the *shoulder*, not the point,
or every depth in every existing document silently changes meaning.

---

## S2 — Draft, rib, variable fillet

**Gap.** `dfm_topology` measures draft angle and reports parts that will not
release from a mould — and there is no feature that can add draft. Ribs are
hand-built as thin boxes with no thickness relationship maintained.
`EdgeFilletFeature` is one radius for all its edges.

**Scope.**
- `DraftFeature` — exact-only, `BRepOffsetAPI_DraftAngle`, taking named faces
  and a pull direction. There is no honest SDF form: tapering a sampled field
  is not the same body.
- `RibFeature` — a thickened sketch line dropped onto the body, with draft and
  a rounded top. Both kernels; the SDF form is genuinely equivalent here.
- Variable radius on `EdgeFilletFeature`: `radius` stays a number, and a new
  `radii` list of `{edge, start, end}` drives `BRepFilletAPI_MakeFillet`'s
  radius law. Absent `radii`, the behaviour is byte-identical to today.

**Exit.** A draft-checked box passes `dfm` for `molding` after one
`add_draft`; the DFM report and the feature agree on the angle.

**Risk.** Low, with one sharp edge: `SetRadius` with a law changes the fillet
surface, so every existing document must take the constant-radius path
unchanged. A test pins the mesh hash of a constant fillet before and after.

---

## S3 — Helix and thread

> **Landed: the helix. Deferred: the thread.** See the note at the end.

**Gap.** No sweep along a helical path. Springs, augers and worm gears are
impossible, and `parts_catalog` ships every fastener with `fidelity:
as-standard` and no thread helix — the correct call given the limitation, and
the limitation is this.

**What shipped.** `HelixFeature` + `add_helix`: a round wire wound as a true
helix, the spine being a straight line in a `Geom_CylindricalSurface`'s
parameter space swept by `BRepOffsetAPI_MakePipeShell` in Frenet mode. Exact
against the closed form (section area × centreline length) to 0.1%, radially
on-coil to 50 µm, valid for booleans, and windable about any axis. Exact-only:
the distance to a helix has no closed form, and the usual field-side trick
drops the tangential term precisely where the pitch is interesting.

**What did not, and what was learned.** `ThreadFeature` is deferred. The
sweep machinery is proven — the same construction builds the ISO 261 groove
tool correctly (radially 3.30–4.43 mm for an M8, exactly the theoretical minor
to major+margin) — but cutting it from the core yields a body whose volume is
still the full major cylinder, and that was not resolved in the sprint. Two
findings are worth keeping so the next attempt does not repeat them:

- **`Bnd_Box` on a swept BSpline bounds the control points, not the surface.**
  It reported a 10 mm coil as 60 mm across and sent the first two hours of this
  sprint chasing a sweep that was correct all along. Measure the tessellation.
- **Only Frenet mode works for a helical spine.** `SetMode(gp_Dir)`, the
  auxiliary-spine modes and plain `MakePipe` all mis-place the profile; the
  contact modes fail outright. A helix has constant curvature and torsion and
  its Frenet normal always points at the axis, which is exactly the frame a
  wound wire keeps — so this is a property of the geometry, not a lucky guess.

`parts_catalog`'s third fidelity waits on the thread.

**Risk (as assessed, and it was right).** Medium — thread geometry is where OCC
boolean robustness gets tested.

---

## S4 — Pattern types

**Gap.** `PatternFeature.kind` is `linear` | `circular`.

**Scope.** `curve` (instances placed along a sketch path at equal arc length)
and `table` (an explicit list of transforms — which is also what makes a
pattern *recoverable* by the decompiler from an arbitrary tree).

**Exit.** Eight bosses along an arc are one feature; the decompiler emits it.

**Risk.** Low.

---

## S5 — Mate connectors and mate types

**Gap.** Four mate keywords, no addressable frame, no DOF accounting, no
motion. The structural half matters more than the count: Onshape's Mate
Connector is a coordinate frame you can place, pattern and mirror *in the part*,
and every mate is a relationship between two of them.

**Scope.**
- `MateConnectorFeature` — a bodyless feature (the third one, alongside
  `SketchFeature` and `DatumPlaneFeature`; it joins `BODYLESS_TYPES`), carrying
  an origin and orientation, placeable on a named face/edge or on a datum.
- `list_mate_connectors` / `add_mate_connector` operations.
- Mates re-expressed as *a pair of connectors plus which DOF survive*:
  `fasten` (0), `revolute` (1R), `slider` (1T), `cylindrical` (1R1T),
  `planar` (2T1R), `ball` (3R), plus the existing `coincident`, `concentric`,
  `distance` kept working unchanged on their current arguments.
- `dof` reported per component and for the assembly.

**Exit.** A revolute mate leaves exactly one rotational DOF, reported;
existing assemblies load and solve identically.

**Risk.** Medium. The solver in `assembly_mates.py` currently resolves
positions directly; DOF accounting needs a real constraint count. Backwards
compatibility is the hard requirement — legacy mates carry no connectors.

> **Landed.** All of the above. Two things came out differently from the plan:
>
> - **DOF is reported for the legacy mates too**, at their *constraint's*
>   freedom rather than at what the bbox solver pins down. `coincident` states
>   that a face sits on a face — one translation and two rotations — and the
>   solver's extra decision to centre the other two axes is a resting position,
>   not part of what was asked for. Reporting 6-removed for everything would
>   have been true of the numbers and false about the assembly.
> - **Connector frames are re-read on every resolve rather than cached.** The
>   plan did not say either way, and caching is the obvious implementation; it
>   is also wrong, because a connector is a feature of the part and a frame
>   copied at mate time goes stale the moment somebody edits it.
>
> Still open from §2.2: no motion simulation, and no drag-to-position that
> respects the constraints. The DOF count is what those would be built on.

---

## S6 — Ceilings that degrade rather than refuse

**Gap.** Five limits that a first serious part hits, each of which currently
fails as a flat refusal.

| Limit | Now | Target |
|---|---|---|
| Sketch entities/constraints | 600 / 600, hard refuse | Raise with a measured cost model; refuse only past the real DoS bound, warn before it |
| Text-to-assembly parts | 6 | 24, with the planner told the budget |
| FEA elements | 8 000 default | Scale with body complexity, capped by plan |
| Import file size | 50 MB | 200 MB for STEP through the streaming path |

**Exit.** Each limit either moved or gained a warning band below it; none of
them changed from a refusal into a *silent* truncation.

**Risk.** Low, but `MAX_ENTITIES` is a security bound, not a style preference —
the rank comes from an SVD that is O(rows²·vars) and `_redundancy_groups`
allocates rows². Any raise has to be measured, not guessed.

> **Landed — and measuring first changed two of the four answers.**
>
> - **The sketch cap was not raised, it was re-shaped.** Measured on two sketch
>   shapes, the cost tracks **entities × constraints**, not either alone: a
>   601-entity chain costs 0.41 s at one constraint per segment and 2.24 s at
>   three. The old 600/600 pair was about right *for the dense shape*, and
>   `routers/operations` allows 300 calls a minute, so a flat raise would have
>   multiplied a real DoS bound rather than moved a ceiling. The bound is now on
>   the product (600k units ≈ 2.2 s worst case) with generous per-axis caps, so a
>   1500-point traced outline — the case that was actually blocked, and which
>   solves in well under a second — now goes through. `size_warning` is the band
>   below it, surfaced as `size_note` on the operation result.
> - **The FEA budget scales with the body's thinnest dimension**, not with a
>   flat 8k. 8k is generous for a 50 mm cube and gives a 1.5 mm sheet one
>   element through its thickness, where the bending stress is not
>   underestimated but *absent* — and an absent stress is an optimistic safety
>   factor, the one output here that must never be that. `mesh.budget_chosen`
>   distinguishes "the solver picked this" from "you asked for this".
> - **Text-to-assembly: 6 → 16 parts, 16 → 48 placements**, and the truncation
>   is now *reported through `progress`* rather than being a silent list slice.
>   That was the worse bug: an assembly arrived missing its last parts and
>   looked like the planner's fault.
> - **Import: 128 MB for STEP/IGES, 50 MB kept for meshes.** The exact payload is
>   worth more — OCC keeps the analytic surfaces — where a 50 MB STL is already
>   a million triangles being voxelised. `body_limit.UPLOAD_MAX_BYTES` follows
>   it to 136 MB, with the memory cost written down rather than implied.

---

## S7 — Surfacing: sheet bodies through the contract

**Gap.** No `Surface`, `Knit`, `Trim`, `Extend`, `Offset`, `Fill`, `Boundary`
or `Thicken`. Every body is a closed solid from the first operation to the
last. This is the difference between mechanical parts and products.

**Why it is expensive.** `brep_from_feature` returns a solid or nothing.
`Kernel.evaluate_feature`, `geometry_check`, `mass_properties`,
`observation.summarize`, `design_spec` and the entire SDF fallback all assume a
closed volume. A sheet has no signed distance field and no volume. So the work
is a **contract change**, not a feature:

1. A body carries a *kind* — `solid` | `sheet` — end to end.
2. `mass_properties` on a sheet returns area, not volume, and says which.
3. `geometry_check` stops calling an open shell a defect when it is the point.
4. The SDF path declines sheets honestly rather than voxelising them.
5. `observation` states the kind, for the same reason
   `summarize_assembly` states `kind: "assembly"` — an agent that infers it
   from the presence of a key will eventually infer wrong.

Only then: `SurfaceFeature` (extrude/revolve/loft/fill as a sheet),
`KnitFeature`, `TrimFeature`, `ThickenFeature`.

**Exit.** A lofted surface knitted into a closed shell and thickened to 2 mm
produces a solid whose volume matches the analytic figure; a sheet body
survives export, drawing and observation without any of them pretending it is
a solid.

**Risk.** High, and it is the risk of *quiet* breakage — a sheet leaking into
a path that assumes volume gives a plausible wrong number rather than an error.
Mitigation: the kind is threaded first and every consumer is made to state its
assumption, before a single surface feature is added.

---

## S8 — In-context / top-down design

**Gap.** One document, one tree, one body set. There is no way to say "this
face of part B is derived from that face of part A" and have it update. The
assembly references *finished* documents, which is bottom-up only.

**Scope.** A `DerivedFeature` naming `{document_id, revision?, entities}` that
pulls geometry from another document at evaluation time, plus the dependency
graph that invalidates B when A changes. Access-checked at resolution the way
`assembly_service.add_component` is, because a derived reference reads the
source's geometry.

**Exit.** An enclosure top and bottom sharing one profile; editing the profile
in the parent updates both children on their next evaluation.

**Risk.** High. Cycles, staleness, and the cache: `_evaluation_key` currently
hashes what is *in* the document, and a derived document's geometry depends on
one that is not.

---

## Progress

| Sprint | Status | What landed |
|---|---|---|
| S1 Hole types | **done** | Counterbore, countersink, drill point, through, tapped; `add_hole`; `clearance_hole` rebuilt as one feature. Geometry enumerated once in `geom/holes.py` for all three samplers. |
| S2 Draft, rib, variable fillet | **done** | `DraftFeature` (exact-only, frustum-exact to 1e-5), `RibFeature` (both kernels, `geom/ribs.py`), `radius_end` on `EdgeFilletFeature`. |
| S3 Helix and thread | **part** | `HelixFeature` + `add_helix`, exact to 0.1%. Thread deferred — see the section above for the two OCC findings. |
| S4 Pattern types | **done** | `curve` and `table` kinds; placements unified into `geom/patterns.py`, which removes the "the SDF does it this way so we match" duplication. |
| S5 Mate connectors | **done** | `MateConnectorFeature` (bodyless), `add_mate_connector` / `list_mate_connectors`, six kinematic mate types built on frame alignment, and a `dof` operation. The legacy four are byte-identical. |
| S6 Ceilings | **done** | Sketch bound re-shaped onto `entities × constraints` with a warning band; FEA budget scaled to the thinnest dimension; text-to-assembly 6→16 parts and its truncation reported; STEP/IGES to 128 MB. |
| S7 Surfacing | — | |
| S8 In-context design | — | |

**A pattern across S1, S2 and S4 worth naming.** Each sprint ended by moving
geometry that two kernels had been computing separately into one `geom/` module
they both read — `holes.py`, `ribs.py`, `patterns.py`. That was not planned; it
is what adding a second variant to any of them made unavoidable. Two
transcriptions of the same arithmetic agree on the day they are written, and
the differential tests then compare two different parts without saying so.

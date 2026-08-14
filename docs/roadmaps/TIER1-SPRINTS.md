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

**Gap.** No sweep along a helical path. Springs, augers and worm gears are
impossible, and `parts_catalog` ships every fastener with `fidelity:
as-standard` and no thread helix — the correct call given the limitation, and
the limitation is this.

**Scope.**
- `HelixFeature` — a profile swept along `GeomAdaptor` helical path
  (`BRepOffsetAPI_MakePipeShell` with an auxiliary spine). Exact-only: a
  helical sweep has no closed-form SDF and a capsule-union approximation would
  be a visibly different body at any usable segment count.
- `ThreadFeature` — an ISO 261 metric profile (60°, `P` pitch from the
  designation) cut or added on a cylindrical face. Internal and external.
- `parts_catalog` gains a third fidelity, `threaded`, opt-in per part.

**Exit.** An M8×1.25 external thread measures the right pitch diameter;
a spring built from `add_helix` has the expected free length and wire count.

**Risk.** Medium. Thread geometry is where OCC boolean robustness gets tested —
a full-length 60° helical cut on a small diameter is exactly the tangency case
`_boolean` already raises `BrepUnsupported` for. Expect to need a tolerance
pass and expect some diameters to legitimately refuse.

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

| Sprint | Status |
|---|---|
| S1 Hole types | — |
| S2 Draft, rib, variable fillet | — |
| S3 Helix and thread | — |
| S4 Pattern types | — |
| S5 Mate connectors | — |
| S6 Ceilings | — |
| S7 Surfacing | — |
| S8 In-context design | — |

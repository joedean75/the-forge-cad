# Handoff — R2: the domain parts catalog (and the bug in front of it)

You are picking up an internal CAD tool for **RC, drone and robotics work**.
Six sprints of Tier-1 capability shipped (S1–S6), plus a toolpath verifier and
R1 (assembly mass budget + URDF export). This is the next sprint.

Read this file, then `CLAUDE.md`, then
[`RC-DRONE-ROBOTICS.md`](RC-DRONE-ROBOTICS.md) for why the plan points here.

---

## Orientation

| | |
|---|---|
| Branch | Work on **`main`**. It is at `5f2d9e9` and matches `origin/main`. |
| Do **not** use | `claude/sprint-8-implementation-1ilklb` — 41 commits behind, nothing unique on it. |
| Backend | `cd backend && python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt` |
| Tests | `pytest` from `backend/`. Last full run: **1187 passed**. Takes ~15 min. |
| Frontend gate | `cd frontend && npm run build` (the type-check *is* the gate; there is no FE test suite) |

**The container may arrive without dependencies installed.** If `import
pydantic` fails, that is the environment, not the code — reinstall before
concluding anything about the repo.

---

## Task 0 — `typical_mass_g` is declared and nothing reads it (do this first)

**This is a real bug and it is a prerequisite, not a warm-up.**

`parts_catalog.Entry.typical_mass_g` exists, is populated for the NEMA steppers,
and its docstring says it is there "so a BOM total is not quietly doubled".
Grep says otherwise:

```
$ grep -rn "typical_mass_g" app/ | grep -v parts_catalog.py
app/services/supplier_parts.py:318:   # writes it
```

Nothing **reads** it. `bom.py` uses `doc.derived.mass`; `assembly_mass.py` uses
`mass_properties.mass_properties(part_id)["mass_kg"]`. Both are the *geometric*
mass of a solid envelope.

Consequences today:

- A NEMA 17 stepper reports **669 g** against a real **350 g**.
- Every BOM total containing a motor is nearly double.
- **`mass_budget` — the entire point of R1 — is wrong**, and not just in
  magnitude: the CG is dragged toward whichever parts are worst-modelled, which
  are exactly the heavy purchased ones.

This gets much worse the moment R2 lands. A brushless motor envelope is a solid
aluminium cylinder; a LiPo envelope is solid something; an ESC envelope is a
solid block. Building the catalog on top of an unwired mass field would produce
a mass budget that is confidently, uniformly wrong.

**What to do.** A document created from a catalog entry carries `catalog_ref`
(`standard_parts.py:122`). Resolve that back to the entry, and where the entry
declares `typical_mass_g`, use it in place of the geometric mass in **both**
`bom.bill_of_materials` and `assembly_mass.assembly_mass_properties`.

Three rules:

- **The document keeps reporting its own geometric mass.** `mass_properties` on
  the part is a measurement of that part's geometry and must not start lying.
  The substitution belongs at the roll-up, where the question is "what does the
  real thing weigh".
- **Say which one you used.** Add a field (`mass_source: "geometry" |
  "published"`) to the BOM line and the `mass_budget` component row. A number
  whose provenance is invisible is one nobody can check.
- **The inertia tensor is not scaled.** It is tempting to multiply the tensor by
  `published / geometric`. Do not: a motor's mass is concentrated in the stator
  and magnets, not spread through the envelope, so the ratio is right for mass
  and wrong for the second moment. Report the tensor as geometric and note it.
  Getting this properly right needs a mass distribution the catalog does not
  have — say so rather than inventing one.

Test it against the existing NEMA 17: BOM and mass budget must both report
350 g, and a plain modelled part must be unaffected.

---

## Task 1 — R2: the domain parts catalog

### The problem

`parts_catalog.py` builds ~210 parts from **7 families**: socket screw, hex nut,
washer, dowel pin, ball bearing, NEMA stepper, T-slot extrusion. For RC, drone
and robotics work that is the wrong set. There is no brushless motor, no ESC, no
flight controller, no servo, no propeller, no LiPo, no standoff, no carbon plate
or tube.

Most of the time on a build of this kind goes on modelling *around* parts
somebody else made. This is the highest-value work available.

### Where the geometry comes from — read this before writing any table

**Do not scrape a supplier.** This has been settled twice and both findings are
in the repo:

- McMaster-Carr — `robots.txt` disallows `/catalog/*`; the API needs a
  per-customer client certificate. See the `parts_catalog` module docstring.
- 3Dfindit / TraceParts — see
  [`PURCHASED-PARTS-SOURCING.md`](../PURCHASED-PARTS-SOURCING.md). Both are
  barred by terms you accept on *use*, which an internal tool does not escape:
  3Dfindit's robots.txt names this crawler, and TraceParts' API terms forbid
  distributing the data on any storage medium.

- BOLTS (GPL-3) and FreeCAD Fasteners (GPL-2) are **not** barred, and the
  earlier note here saying they were was wrong: copyleft attaches to conveying
  a copy, and an internal tool conveys nothing. Reading them to *cross-check* a
  generated table is the best use — two derivations disagreeing has found a bug
  in one of them. Transcribing a table wholesale is allowed and leaves a lien:
  the codebase cannot be published until it comes out, so record the source on
  anything taken that way.

The existing catalog is generated from **published standards**, on the principle
that a dimension in a standard is a fact and a fact is not somebody's content.
R2's difficulty is that this domain has fewer formal standards — so the sorting
rule is:

> **Table what is genuinely standardised or a published de-facto convention.
> Parameterise what varies by manufacturer. Never invent a number to fill a
> table.**

Applying it:

| Part | Standardised? | So |
|---|---|---|
| FC / ESC mounting patterns | **Yes** — 30.5×30.5 (M3), 20×20 (M2), 16×16 (M2) are the industry patterns | Table the pattern; board outline is a parameter |
| Brushless motor sizing | **Convention** — the 4-digit code is stator ⌀ and height (`2207` = 22 mm × 7 mm) | Table the code→stator mapping and the mount patterns (16×16 / 19×19 / 25×25 by size class); bell diameter and total height are parameters |
| Servo bodies | **De-facto** — standard / mini / micro classes have settled body and ear dimensions | Table the three classes |
| Standoffs | **Yes** — M2/M3 hex, across-flats per the hex-nut standard already in the file | Reuse the existing `METRIC` table |
| Carbon plate / tube | Stock sizes, trivially derived | Parameterise entirely |
| Propellers | Diameter × pitch is a designation, hub bore is ~standard | Table nothing; parameterise. **The useful body is the swept disc**, not two blades — see R4 |
| **LiPo packs** | **No.** Dimensions vary by manufacturer for the same mAh/cell count | **Parameterise the envelope.** Do not table pack dimensions — you would be inventing them |

**Verify every number you table against a published source before committing
it, and put the source in `standard`/`notes`** the way the ISO entries do. If
you cannot find a source, that part is a parameterised envelope, not a table
row.

### Shape of the work

`parts_catalog.py` already has everything you need. Extend, don't restructure.

- **Builders** are plain functions returning a feature tree, composed from
  `_cyl` / `_box` / `_hex_prism` / `_union` / `_subtract` / `_tree`
  (lines 159–217). Add yours beside `stepper_motor` and `tslot_extrusion`.
- **`Entry`** (line 343) is the row: `key`, `category`, `designation`,
  `standard`, `material`, `fidelity`, `notes`, `build`, `typical_mass_g`.
- **`catalog()`** (line 376) is a flat list built by looping the tables. Add
  loops; keep the pattern.
- `_PART_CATEGORIES` in `cad_operations.py` already contains `motor`,
  `electronics`, `pneumatic`, `hardware` — use them rather than adding new ones
  unless something genuinely does not fit.

**`typical_mass_g` is mandatory for every powered or electronic part.** A
brushless motor, an ESC, an FC, a servo and a LiPo all have envelopes whose
geometric mass is meaningless. This is why Task 0 comes first.

**`fidelity` must be honest.** `envelope` for anything whose internals are not
modelled — which is all of these. Do not write `as-standard` for a motor.

---

## Task 2 — R3: mounting-pattern features (if budget allows)

Only start this once R2 is committed and green.

Today a 30.5×30.5 pattern is four hand-placed `add_hole` calls. Swapping the FC
for a 20×20 board means re-drilling by hand and hoping you got all four.

Add a **`mounting_pattern`** operation that drills a named pattern —
`fc-30.5`, `fc-20`, `fc-16`, `nema17`, `servo-standard`, and so on — at a given
centre and axis, sized for a given screw with the correct ISO 273 clearance.
`parts_catalog.clearance_hole` already computes the diameter; `add_hole` already
cuts counterbores.

The value is that it is **one feature**, so changing the pattern re-drills.
Consider building on `PatternFeature`'s `table` kind, which exists precisely to
express an arbitrary explicit arrangement — the patterns are a table of four
placements.

---

## House rules that will bite you

All from `CLAUDE.md`; these are the ones this particular work will trip over.

1. **A new parameter name must be classified** in `services/units.py` — a unit
   in `UNIT_BY_NAME` or membership of `UNITLESS`. `test_action_space` fails
   otherwise. Watch for name collisions: `size` is already a length everywhere,
   which is why a thread designation is called `screw` and not `size`.
2. **Adding a feature type is two edits** — the class under `@register_feature`
   in `ops/`, and one line in `_SDF_BUILDERS`. `test_registries` fails on one
   without the other. (R2 probably needs no new feature types; R3 might not
   either.)
3. **Adding an operation is one edit** — `@operation(...)` in
   `cad_operations.py`. Fetch args with `op_errors.require`, never `args[...]`.
4. **The decompiler must round-trip anything new.** A feature type it cannot
   emit drags every document containing it to 0% and takes its neighbours with
   it. Add the mapping in the same change.
5. **`x or default` swallows an explicit zero.** This bit me in the toolpath
   verifier: `float(args.get("diameter_mm") or 3.0)` turned a caller's `0` into
   the default and would have machined with a cutter nobody asked for. Use
   `default if value is None else float(value)`.
6. **Where two kernels compute one shape, the arithmetic goes in `geom/`** —
   `holes.py`, `ribs.py`, `patterns.py` are the precedent. Two transcriptions of
   one formula agree the day they are written.
7. **Do not edit source files while a background test run is in flight.** It
   produced 48 spurious failures in this session and cost a 15-minute re-run to
   diagnose.

---

## Exit criteria

**Task 0**
- BOM and `mass_budget` report **350 g** for a NEMA 17, not 669 g.
- Both carry `mass_source`, and a plain modelled part still reads `geometry`.
- The inertia tensor is unchanged and the note says why.

**Task 1**
- `list_standard_parts` returns brushless motors, ESCs, FCs, servos, standoffs,
  props and carbon stock alongside the existing families.
- Every powered/electronic entry declares `typical_mass_g` and
  `fidelity: envelope`.
- Every tabled dimension names its source in `standard` or `notes`.
- LiPo packs are parameterised, not tabled.
- `insert_standard_part` places one, and eight of them are one BOM line of
  qty 8 (the existing `document_for` reuse must keep working).
- A quadcopter assembly — 4 motors, 4 props, FC, ESC, LiPo, frame plate —
  produces a `mass_budget` whose AUW is within a sensible band of a real build.
  That last one is the test worth writing: it is the only one that checks the
  whole chain end to end.

**Both**
- `pytest` green from `backend/`.
- `npm run build` clean from `frontend/`.
- `shared/types.ts` updated by hand if any response shape changed — it mirrors
  the Pydantic models and is **not** generated.

---

## Known traps specific to this work

- **A solid envelope's mass is not the part's mass.** Stated three times above
  because it is the single thing that makes this catalog either useful or
  actively misleading.
- **`insert_standard_part` creates the part document under the *assembly
  owner's* account**, because `add_component` refuses a part its owner cannot
  open. Keep that if you touch `standard_parts.document_for`.
- **Don't put a part number on anything.** The catalog carries a *designation*
  and a search link. None of these were verified against a supplier's data, and
  a part number is what an order is placed against — inventing one is inventing
  an order.
- **Props are the one part whose useful geometry is not its shape.** For
  clearance you want the swept disc. Modelling two blades and calling
  `check_interference` on them answers a question nobody asked. That is R4;
  don't half-build it here.

---

## What comes after

From [`RC-DRONE-ROBOTICS.md`](RC-DRONE-ROBOTICS.md): R4 prop/rotor clearance,
R5 thread (deferred from S3, with the OCC findings written down in
[`TIER1-SPRINTS.md`](TIER1-SPRINTS.md) §S3 so the next attempt does not repeat
them), R6 surfacing, R7 in-context design.

Two small unrelated things also worth doing when convenient: move
`services/realtime.py` and `app/rate_limit.py` off process-local dicts — they
are what currently stops a second worker — and re-run the full suite once deps
are reinstalled to re-confirm the 1187.

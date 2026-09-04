# The plan, re-pointed: an internal tool for RC, drone and robotics work

This supersedes the ordering in
[`TIER1-SPRINTS.md`](TIER1-SPRINTS.md) and re-frames
[`COMPETITIVE-GAPS.md`](../COMPETITIVE-GAPS.md). Those were written against a
different goal — displacing a SolidWorks seat — and that goal is no longer the
one. The gap analysis is still accurate about *what is missing*; it is now wrong
about **which of it matters**.

## What changes

Not a general-purpose CAD product competing with Onshape, Fusion and
SolidWorks, but **an internal tool for building RC vehicles, drones and
robots**. Two consequences follow immediately and they point in opposite
directions.

**The bar for breadth drops.** Nobody has to migrate a legacy part library into
it, so native Parasolid/JT/SolidWorks import stops mattering. Nobody is selling
seats, so PDM vaults, certification programmes and an add-in ecosystem stop
mattering. There is no drawing office, so the drawing package is adequate as it
stands.

**The bar for the specific things this domain needs rises sharply**, and most of
them were nowhere on the old list because they are not what a CAD *competitor*
is judged on:

| What this domain needs | Was it on the old plan? |
|---|---|
| All-up weight, CG, inertia tensor for a **whole vehicle** | No — part-level only |
| Export to a robotics simulator (URDF / MJCF) | No |
| Motors, ESCs, flight controllers, servos, props, LiPos in the catalog | No — the catalog is ISO fasteners and bearings |
| Standard mounting patterns (30.5×30.5, 20×20, NEMA, servo horns) | No |
| Carbon plate / tube construction, profile cut, nesting | Partly — the CAM half exists |
| 3D-print DFM, slice, quote, print | **Already strong** |
| Joint articulation and DOF | **Shipped in S5** |

## The biggest re-ranking: surfacing drops

Surfacing was ranked the single largest gap and the #5 priority — a quarter of
work on a kernel contract change. **It should not be next.**

Surfacing is what you need for a moulded consumer housing or an aerodynamic
Class-A surface. An internal RC/drone/robotics tool builds brackets, motor
mounts, chassis plates, arms, gearbox housings and camera mounts — parts that
are cut from plate, printed, or milled from block, and that are exactly what the
solid kernel plus the S1–S6 features already handle. A drone canopy is the one
honest counter-example, and a canopy is a part you print or vacuum-form once,
not the thing that gates the tool's usefulness.

It stays on the list. It is no longer near the top of it.

## The new order

| # | Sprint | Why here |
|---|---|---|
| **R1** | **Mass budget and URDF** ✅ | AUW and CG are checked before every flight; the inertia tensor is what a controller is tuned against. URDF makes the CAD drive the simulator. Both were unlocked by S5's mate connectors. |
| **R2** | **Domain parts catalog** ✅ | Brushless motors, ESCs, FCs, servos, props, LiPos, standoffs, carbon plate and tube. Same generator pattern as `parts_catalog` — built from published dimensions, not scraped. |
| **R3** | **Mounting-pattern features** ✅ | 30.5×30.5 and 20×20 FC patterns, NEMA faces, servo horns, GoPro-style mounts, bearing seats. Parametric features rather than hand-drilled holes, so a swap re-drills. |
| **R4** | **Prop/rotor clearance and arm layout** ✅ | An interference check that knows a prop is a *swept disc*, not the two blades modelled. The one geometric check this domain has that general CAD does not. |
| R5 | Thread (deferred from S3) | Real fastener geometry and a third `parts_catalog` fidelity. Everything is bolted together here. |
| R6 | Surfacing | Still the largest capability gap; no longer the most valuable one. |
| R7 | In-context / top-down design | Mating halves of a printed enclosure. Real, and after the above. |

R2 and R3 together are worth more than anything else on the list: most of the
time on a build of this kind is spent modelling around parts somebody else
made, and getting the mounting pattern wrong is the error that costs a reprint.

## What the existing work is worth under the new framing

Better than it looked. Re-reading S1–S6 against this domain:

- **S5 mate connectors** turn out to be the foundation of R1, and of any motion
  study later. That was luck of ordering, not foresight.
- **S1 hole types** — counterbores for socket screws, tapped holes — is exactly
  the everyday work here.
- **S2 ribs and draft** matter less (draft is for moulding), but ribs are how a
  printed bracket gets stiff without getting heavy.
- **S3 helices** cover springs, which suspension and landing gear need.
- **The additive pipeline** — DFM on the exact body, slice, quote, print to
  Moonraker — was a nice-to-have against SolidWorks and is a core workflow here.
- **The agent-native action space** is unchanged in value and arguably worth
  more: an internal tool is exactly where you want to say "make me a mount for
  this motor with a 30.5 pattern" and get a part.

## R1 — Mass budget and URDF (shipped)

**`mass_budget`** on an assembly: all-up weight, centre of gravity and inertia
tensor in assembly space, plus each component ranked by how far it moves the
balance. Three pieces of arithmetic, none optional — mass adds; the CG is the
mass-weighted mean of the component CGs *carried into assembly space by their
placements*; and the inertia tensors are rotated (`R I Rᵀ`) before the
parallel-axis shift. Using bounding-box centres instead of real part CGs is the
tempting shortcut and it is wrong for every bracket. A scaled instance is
excluded and says why: mass goes as `s³` and inertia as `s⁵`, and density
belongs to the material rather than to the instance.

**`export_urdf`**: connector mates become real joints, bounding-box mates become
`fixed` because a face-on-face placement is not an articulation. What it refuses
to fake is the interesting half —

- URDF is a **tree**, so a closed loop (a four-bar, a differential) has no
  representation; the mate that closes it is named rather than dropped.
- URDF has **no cylindrical and no spherical joint**; those emit `fixed` and are
  reported, because a joint that silently loses a degree of freedom is a robot
  that behaves differently in simulation than on the bench.
- A `revolute` mate emits `continuous`, not `revolute`: URDF's revolute requires
  a `<limit>`, and inventing one puts a travel stop in the simulation that the
  machine does not have.

One thing that was wrong first and is worth knowing: **URDF's joint frame is the
child link frame**, so the joint origin has to carry *both* connectors —
`F_base · offset · F_moved⁻¹`. Emitting the base connector alone places the
child's origin on it, which is correct only when the moved connector happens to
sit at the part origin. And `<axis>` is expressed in the joint frame, which
reduces it to the moved connector's own z column.


---

## R2 — The domain parts catalog (shipped)

Brushless motors, flight controllers, 4-in-1 ESCs, RC servos, hex standoffs,
propellers, carbon plate and tube, and LiPo packs — 265 rows where there were
210, and the new ones are the ones this domain is built from.

**The sorting rule, because this domain has fewer published standards than the
fastener one.** Every number is in one of three buckets and the row says which:

- **Standardised, or a settled de-facto convention** — tabled, with the
  convention named. The 30.5 / 20 / 16 stack patterns, the four-digit
  brushless stator code (`2207` is a 22 mm stator, 7 mm tall), the three RC
  servo classes, the hex across-flats a standoff takes from the nut table that
  was already here.
- **Varies by manufacturer** — a **parameter**, whose default is typical for
  the class and whose description says so. A motor's bell diameter, a board's
  outline, a propeller's bore.
- **Neither** — not present. A LiPo pack is parameterised outright: two 4S
  1300 mAh packs from different makers differ by ten millimetres and forty
  grams, so a table keyed on capacity would be a table of invented numbers.

**Parameters are a channel, not a special case.** `Entry.params` declares what
a row takes, `insert_standard_part` accepts them, the listing publishes them.
An unknown name is refused rather than dropped — a parameter silently ignored
is a part built to a size nobody asked for and reported as a success. The reuse
key is the *reference*: the bare key for the default configuration, so every
document stored before parameters existed still resolves and four identical
motors are still one BOM line; the key plus its changed parameters otherwise,
so two differently sized packs are two parts with two names.

**A propeller is the disc it sweeps.** The question anyone asks of a propeller
in an assembly is whether anything is inside its arc, and two blades frozen at
one azimuth answer that only for that azimuth. R4 is the clearance check; this
is the body it will read.

**Task 0, the prerequisite.** `typical_mass_g` was declared, populated and read
by nothing: both roll-ups took the geometric mass of a solid envelope, so a
NEMA 17 reported 672 g against a real 350 g and every mass budget containing a
motor was nearly double. `standard_parts.published_mass_kg` resolves a
document's `catalog_ref` back to its entry and `bom` and `assembly_mass` prefer
the published figure, saying which they used in `mass_source`. The part
document goes on reporting its own geometry, and the inertia tensor is **not**
scaled by the ratio — a motor's mass is in its stator and magnets rather than
spread through the bell, so the ratio is right for the mass and wrong for the
second moment.

The end-to-end check is a quadcopter: four motors, four props, an FC, a 4-in-1
ESC, a pack and a frame plate come to **424 g**, against the **780 g** their
envelopes weigh.

## R3 — Mounting patterns (shipped)

**`mounting_pattern`** drills a named bolt pattern as **one** `PatternFeature`
of kind `table` — the FC stack patterns, the motor mounts, the six NEMA faces
and the three servo ear patterns. Bores are ISO 273 clearance for the pattern's
own screw, from `parts_catalog.clearance_hole`. Being one feature is the point:
swapping a 30.5 board for a 20 one is the same operation again with `feature`
and the new `pattern`, and all four holes move together — where four
`add_hole` calls are four holes to find and move by hand, with nothing to say
you missed one until the board does not bolt on.

Three details:

- **The placements are derived, not tabled a second time.**
  `parts_catalog.bolt_patterns()` builds them from `MOUNT_PATTERNS`, `NEMA` and
  `SERVOS` — the same tables the parts themselves are built from — and both
  `pattern_holes` (which cuts them into the catalog's own parts) and this
  operation read it. So a bracket drilled `nema17` and a NEMA 17 stepper agree
  about where 31 mm is by construction. A bolt circle transcribed twice fits in
  CAD right up until it is made.
- **A pattern is a union of its copies**, so it goes in as the *tool* of a
  subtract rather than wrapping the body. Wrapping would union four copies of a
  drilled body, which fills every hole back in.
- **Plain bores only.** A counterbore in the same feature would need a boolean
  under the pattern, and the decompiler emits a targeted `add_modifier` against
  the pattern's *first child* — so it would replay as one counterbore rather
  than four, and the mesh comparison would report `geometry_mismatch` for every
  document containing one. `add_hole` counterbores a single hole.


## R4 — Rotor clearance (shipped)

**`prop_clearance`** on an assembly. Every rotor is reduced to the **disc it
sweeps** — tip radius measured across the body, whatever the body is — and then
three questions: do two discs overlap, is anything standing inside one, and how
long do the arms have to be.

**Why not `check_interference`.** Two blades frozen at one azimuth clash or
clear depending on where the modeller left them, and where the modeller left
them is not a property of the aircraft. The test that justifies the operation
is two bladed props a quarter turn apart whose bodies genuinely miss each other
and whose discs overlap by 17 mm: general interference says clear, and the
aircraft still eats its own propellers.

Five things are load-bearing, and each was wrong first:

- **The gap reported is the dimension you would change.** The true signed
  distance between two overlapping coplanar discs is to whichever surface is
  nearest, and that is the flat face — so a quad whose arms were 14 mm short
  reported a 4 mm overlap, 4 mm being the blade's own thickness and a number
  about nothing. Rotors in parallel planes are resolved into a plan gap and an
  axial gap; the radial one is reported when they interpenetrate. Rim sampling
  stays for axes that are not parallel, where no single dimension is the fix.
- **Stacked coaxial rotors are not a clash.** An X8 overlaps in plan by design,
  and calling that a failure trains a reader to ignore the check. A stack too
  tight to be safe is still reported.
- **The swept region is an annulus.** The hub does not sweep: it is bolted to a
  shaft and that shaft reaches into it by design. Left as a disc, every
  correctly built quadcopter reported four obstructions reading "your propeller
  is mounted on its motor". The hub comes from the catalog row, or from
  `hub_mm` for a rotor somebody modelled, and each rotor reports which it used.
- **The near-miss band starts a clearance outboard of the hub.** Anything that
  has to reach the hub is within a clearance of the annulus's inner edge by
  construction, so asking for margin closer in is asking for the rotor not to
  be mounted. Material actually inside the annulus is a strike wherever it is.
- **The obstruction test samples the disc, not only the obstruction.** A 6 mm
  mast standing through an 8 mm disc has no vertex and no facet centroid inside
  that band, so the strike read as a clearance — the same bug `_surface_samples`
  was written for, one level up. The disc's own polar grid is evaluated against
  the part's exact field, and the surface pass stays beside it for bodies large
  enough that the grid only grazes them.

An assembly with no rotor this can identify **refuses** rather than answering
`clear`: a clearance report that means "I found nothing to check" is the one
thing it must not be mistaken for.

The layout arithmetic — `2 R sin(pi/N) - D` — is stated as the closed form for
N evenly spaced rotors and says so, because the measured pair gaps beside it owe
nothing to that assumption. The two have to agree: re-place a striking quad at
the arm the layout asks for and the tip gap lands on the clearance requested.

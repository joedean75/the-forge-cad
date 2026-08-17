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
| R2 | Domain parts catalog | Brushless motors, ESCs, FCs, servos, props, LiPos, standoffs, carbon plate and tube. Same generator pattern as `parts_catalog` — built from published dimensions, not scraped. |
| R3 | Mounting-pattern features | 30.5×30.5 and 20×20 FC patterns, NEMA faces, servo horns, GoPro-style mounts, bearing seats. Parametric features rather than hand-drilled holes, so a swap re-drills. |
| R4 | Prop/rotor clearance and arm layout | An interference check that knows a prop is a *swept disc*, not the two blades modelled. The one geometric check this domain has that general CAD does not. |
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

---
name: forge-assembly
description: Use when composing an assembly in The Forge — the second door for operations, why the interference check reads interferences and never clear, and what "done" requires
---

# Composing an assembly

The catalog addresses **two kinds of thing**, and every operation says which.
An assembly operation takes an assembly id and runs through
`POST /api/assemblies/{id}/op` (MCP: `run_assembly_operation`). A part operation
on that route is refused with a pointer rather than a 404 on a real id — if you
get that message, you are at the wrong door, not holding a bad id.

**Creating** an assembly is not an operation: it is `POST /api/assemblies`
(MCP: `create_empty_assembly`), for the same reason creating a document is not —
it would be the only operation whose id argument means nothing.

## Getting parts in

- `insert_part` places a document you already have.
- `insert_standard_part` is the catalog: ISO screws, nuts, washers, bearings,
  NEMA steppers, extrusion, plus brushless motors, ESCs, servos, propellers and
  LiPo packs. **Do not model a screw.** It is slower and less accurate than the
  row that already exists, and eight of one row is one BOM line of qty 8.
- `build_part` runs a whole part copilot on a fresh document and inserts the
  result. Its goal must stand alone — the part run cannot see the assembly, so
  "the base plate" builds nothing useful. Give dimensions in millimetres.

## Mates, and the two schemes

The original four — `fasten`, `coincident`, `concentric`, `distance` — read
bounding-box extremes along one world axis. They place a bolted stack and cannot
express a rotation.

Anything that turns needs **mate connectors**: named frames in the parts
(`MateConnectorFeature`), then `revolute`, `slider`, `cylindrical`, `planar`,
`ball` or `parallel`. `drive_mate` sets `angle_deg` / `offset_mm` on an existing
mate — that is how a hinge opens or a gear indexes. Driving does **not** consume
the degree of freedom: a door held open 30° is still a door, so `dof` reads the
same before and after.

## The interference check reads `interferences`, never `clear`

`clear` is false for a near miss as well as a real clash — and mating two parts
is precisely what puts their faces in contact, so a coincident mate holds them at
a 0 mm gap **by definition**. Gating on `clear` fails every correctly assembled
stack while the same result says "0 pair(s) of parts intersect". Read the
`interferences` count. A near miss is worth reporting and is not a refusal.

A component listed in `skipped` was **not checked** — that is unverified, not
clear. Imported STEP parts are checked against a voxelised copy of their mesh;
their pairs carry `tolerance_mm`, the band inside which a reading is not a clash.

## Read an imported part before asking for its drawing

`component_interfaces` reads a placed purchased part's shaft (diameter, signed
projection at each end, tips in assembly space), its mounting holes (bolt
circles, and every metric screw each could take) and its can, off the exact
body. Use it before asking the user for dimensions. It cannot tell you KV,
turns or a part number, and says so.

`prop_clearance` is a different question: a rotor is reduced to the annulus it
sweeps, because two blades frozen at one azimuth answer only for that azimuth.
Stacked coaxial rotors overlap in plan by design and are not a clash.

## What "done" requires

Every component must be named by a mate — a part that was placed and never
constrained sits at the origin inside whatever else is there — and no two parts
may intersect. Then `render_assembly` draws the components **in their placed
positions**, which is the only view that shows an arm mounted backwards; a
per-part picture cannot answer that question.

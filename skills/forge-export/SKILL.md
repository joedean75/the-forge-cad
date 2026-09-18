---
name: forge-export
description: Use when getting geometry out of The Forge — exports are URLs not payloads, and the URDF and package refusals are deliberate
---

# Getting it out

## An export is a resource, not a payload

`export` takes `format`: `step` (exact solid another CAD package opens), `stl`
(mesh for a slicer), `pdf` (the dimensioned sheet a shop reads), `dxf` (drawing
geometry on layers, for CAM), `svg` (the sheet as a web image).

It returns a **URL keyed by the file's content hash** — never inline bytes. A
plain plate is 1.4 MB of STEP, which base64s to 1.9 MB riding on a response you
would then put in a prompt. Two consequences: exporting the same body twice
returns the same URL, so a retry is free; and the digest is of the *file*, so two
documents with identical geometry still export differently — the STEP header
carries the part name.

The mesh is the same shape of thing: `derived.mesh_url` is
`/api/documents/{id}/mesh/{mesh_hash}`, immutable and cached forever, carrying
Forge's indexed binary mesh (`FMSH`) rather than STL. A hash the document no
longer has returns 404 rather than different triangles — re-read the document and
use the new URL.

## The URDF refusals are features

`export_urdf` is what mate connectors are for: a link needs mass, a centre of
mass and an inertia tensor, and a joint needs two frames and an axis. What it
will not do is fake the hard half.

- A **closed loop** (a four-bar, a differential) is named as unsupported. URDF is
  a tree; dropping a joint to force one would describe a different machine.
- **Cylindrical and spherical joints emit `fixed`, and say so.** URDF has neither.
  A joint that silently loses a degree of freedom is a robot that behaves one way
  in simulation and another on the bench.
- A **`revolute` mate emits `continuous`**, because URDF's `revolute` requires a
  `<limit>` and inventing one puts a travel stop in the simulation that the real
  machine does not have.

Do not work around these by editing the output. They are the honest answer, and
each one names what it declined.

## A release package is built from a revision

`release_package` zips the STEP body, the drawing as PDF and DXF, the mesh, a
one-line BOM, the spec report and the revision letter. It needs a **released
revision** — call `release_revision` first — because a package pinned to "now" is
a screenshot of a moving target. The manifest states
`document_matches_revision` explicitly; a document that has moved on since the
release is the normal case, not an error.

A format that will not render is listed in `unavailable` rather than failing the
package: a body the drawing pipeline cannot trace still has a STEP file worth
sending.

## Drawings

`generate_drawing` takes a full `DrawingSpec` — which views, which sections and
where they cut, what to dimension. `drawing_preset` (`shop` / `print` /
`inspection`) fills that in for you and produces a **spec**, not a sheet, so you
can edit one field rather than rebuild it. Printed dimension figures come from
the document's own bounding box, so a drawing and `bounding_box` can never
disagree about the part's size.

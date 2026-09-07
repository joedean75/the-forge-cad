# Where purchased-part CAD can come from

Written up because the question keeps returning and the answer is not obvious
from the outside: the big CAD portals look like an API away and are not. This
records what was checked, on 2026-08-09, so the next person does not re-derive
it — and so that if a portal's terms change, there is a baseline to compare
against.

## The question

`services/parts_catalog.py` generates 210 purchased parts from the published
dimensional standards. That is a good floor and a hard ceiling: an ISO 4762
M4×16 is an ISO 4762 M4×16 whoever makes it, but no amount of standards work
produces *this* Igus bearing, *that* Festo cylinder, or the connector whose
footprint decides whether the enclosure lid closes.

Those models exist, on 3Dfindit and TraceParts and the manufacturers' own sites.
The question is whether this system may fetch them.

## 3Dfindit (CADENAS) — no

| Checked | Result |
|---|---|
| `https://www.3dfindit.com/robots.txt` | `User-agent: ClaudeBot` → `Disallow: /`. Also Amazonbot, Bytespider, CCBot, GPTBot, Google-Extended, meta-externalagent. `Content-Signal: ai-train=no, use=reference`. |
| `GET https://www.3dfindit.com/` from a non-browser client | `403` |
| A documented HTTP API returning geometry | None found. The published interfaces are CAD-host plugins (NX, SolidWorks, Inventor, AVEVA E3D), desktop apps (PARTdataManager, PARTdesigner) and a C++ SDK. The one public repo, `cadenasgmbh/3dfindit-freecad-integration`, is archived. |
| Download gating | Free account, fair-use cap of 50 download tickets a day. |
| Who owns the models | The manufacturers who publish them. CADENAS hosts. |

The robots.txt naming this crawler and the 403 settle it before the licensing
question is even reached. There is no seeding path here.

## TraceParts — a pass-through, never a seed

TraceParts *does* publish a real REST gateway
(<https://developers.traceparts.com>): `POST /v3/Product/cadRequest` then
`GET /v2/Product/cadFileUrl` returns a temporary URL to a generated CAD file, on
a Bearer token from an API key granted after a review that "can take several
days".

Its [API Terms of Use](https://developers.traceparts.com/docs/api-terms-of-use)
close the door on using it as a catalog source, four times in one paragraph:

> Unless Users have received prior written consent from TraceParts, they may not
> i) distribute the Data as part of an external service, or ii) copy or post the
> Data on a website, or iii) distribute the Data on any storage medium
> whatsoever, or iv) use the Data with the aim of competing with any TraceParts
> service.

and, of end-user files:

> may not use such comments and files to create catalogs or websites competing
> with any TraceParts service.

Two further obligations shape any integration:

- **Contact capture.** "Users must share the contact information of who is
  downloading TraceParts' CAD models … at least … email address, name,
  company/organisation name and country."
- **Attribution.** Implementations "shall include the text hyperlink, 'powered
  by TraceParts'".

So a legitimate TraceParts integration is a **per-user pass-through**: the user
is identified to TraceParts, fetches one file for one use, and it is not
retained as library content. That is a real feature and a different one from
what a parts catalog is; it is not built, and if it ever is, it must not write
rows into `supplier_parts`.

## The open libraries — a copyleft trap

The tempting shortcut is a free parts library's dimension tables:

| Project | Licence | Verdict |
|---|---|---|
| [BOLTS](https://github.com/boltsparts/BOLTS) | GPL-3.0 | Copying its data would put this codebase under GPL-3. |
| [FreeCAD Fasteners WB](https://github.com/shaise/FreeCAD_FastenersWB) | GPL-2.0 | Same. |
| NopSCADlib | GPL-3.0 | Same. |

They look like free data. Transcribing tables out of them is a licence event,
not a shortcut, and the underlying numbers are available from the standards
anyway — which is where `parts_catalog` already gets them.

## Vendor libraries on GitHub — the one path that is not closed

Checked on 2026-09-07, prompted by Adafruit's, which is the well-known one.
The portals are gated because a portal's product *is* the aggregation; a
manufacturer publishing its own catalog is doing something else, and a few of
them publish under licences that permit redistribution outright. This is the
only source examined so far where seeding is legally available rather than
merely tempting.

The distinction that decides each row is **redistribution of the models as a
collection**, which is exactly what a catalog is. A licence that only permits
*use in a design* — every "design exception", and TraceParts' terms above —
covers a user putting the part in an assembly and not this system shipping it.

| Publisher | Where | Licence | Redistributable as catalog content? |
|---|---|---|---|
| [Adafruit](https://github.com/adafruit/Adafruit_CAD_Parts) | `adafruit/Adafruit_CAD_Parts` | **MIT** (repo `LICENSE`) | **Yes.** STEP + Fusion 360 + STL, one folder per product, keyed by Adafruit product number. Attribution is the only condition. |
| [SparkFun](https://github.com/sparkfun/3D_Models) | `sparkfun/3D_Models` | **CC BY-SA 4.0** (hardware/models; MIT for code) | **Yes, share-alike.** STEP/IGES/STL plus SolidWorks and Blender sources, under `products/{id}/{rev}/{type}/`, real scale, mm. Adaptations of a model stay CC BY-SA; the *codebase* is untouched, unlike the GPL tables above — these are data files, not something transcribed into source. |
| [Framework Computer](https://github.com/FrameworkComputer/Framework-Laptop-16) | `FrameworkComputer/Framework-Laptop-{12,13,16}`, `ExpansionBay`, `ExpansionCards` | **CC BY 4.0** | **Yes, attribution only.** Whole-system and module CAD, published so third parties can build accessories against it. The most permissive mechanical CAD found from a consumer hardware maker. |
| [KiCad](https://github.com/KiCad/kicad-packages3D) | `KiCad/kicad-packages3D` | CC BY-SA 4.0 **with a design exception** | Yes, but only under CC BY-SA. The exception waives attribution/share-alike *for boards designed with it*; redistributing the library as a collection is explicitly outside it. Thousands of STEP package bodies — generic footprints (SOT-23, TO-220), not products. |
| [Digi-Key](https://github.com/Digi-Key/digikey-partner-kicad-library) | `Digi-Key/digikey-partner-kicad-library` | CC BY-SA 4.0 with the same exception | Symbols and footprints, not solids. Useful as a part-number spine, not as geometry. |
| [Seeed Studio](https://github.com/Seeed-Studio/OPL_Kicad_Library) | `Seeed-Studio/OPL_Kicad_Library` | KiCad-style library licence | Same shape as Digi-Key's: OPL symbols/footprints. Seeed's STEP models are scattered across SnapMagic and shareCAD rather than collected in a repo. |
| [Voron](https://github.com/VoronDesign) | `VoronDesign/*` | GPLv3 | Printer parts as STEP/F3D. GPL on CAD is the same trap as BOLTS: fine to consult, a licence event to absorb. |
| Prusa | GitHub + prusa3d.com | GPL on older printers; **Open Community License** on CORE One | No. OCL grants inspection, modification and spare parts for personal or internal business use — not redistribution as library content. |
| Raspberry Pi | [`raspberrypi/documentation`](https://github.com/raspberrypi/documentation/) (CC BY-SA 4.0); STEP on [pip.raspberrypi.com](https://pip.raspberrypi.com/) | Mixed | The repo is prose. The board STEP files sit on the product portal under its own terms, so this is a per-file question, not a library. |
| Arduino | — | — | No official CAD repo found. Adafruit's covers several Arduino-form boards. |
| Pololu | Per-product "Resources" tab | Per-file | Official STEP for gearmotors and similar, one product page at a time. No repo, no bulk. |
| OpenBuilds | [builds.openbuilds.com](https://builds.openbuilds.com/projectresources/step-parts-library.162/) | Unstated | A full V-slot/extrusion STEP set, but community-hosted with no licence declared — which means no permission, not permission by silence. |

Robotics is a separate seam and worth knowing about, because `export_urdf`
already speaks that language: `UniversalRobots/Universal_Robots_ROS2_Description`
(BSD-3/Apache-2.0 packages, with UR's own terms on the meshes),
`frankarobotics/franka_ros` (Apache-2.0) and the Robotiq description packages
ship arm and gripper geometry with real joint frames. The catch is the format —
they are STL/DAE visual meshes, so they arrive through `import_service`'s
voxel path as an opaque `ImportFeature`, not as an addressable `BrepBodyFeature`.
Vendor STEP, where it exists, is the one worth importing.

[SnapMagic Search (SnapEDA)](https://www.snapeda.com/get-api/) is the
electronics-side analogue of TraceParts, and lands in the same place: real API,
models under CC BY-SA 4.0 with Design Exception 1.0, and that exception covers
putting a part in *a board design* rather than republishing the models as a
library. Read its terms before any integration; treat it as a pass-through.

### What this changes

The floor moves for the first time. `parts_catalog` generates standards
geometry and `supplier_parts` files what a user brings; between them sat every
part that is neither a standard nor already on the user's disk — and Adafruit,
SparkFun and Framework are that gap, under licences that permit shipping the
geometry rather than linking to it.

Three conditions on ever doing it, none of them optional:

- **Attribution is a stored field, not a footer.** A seeded row must carry its
  publisher and licence, because CC BY-SA obliges anything derived from a
  SparkFun model to stay CC BY-SA and a row that has lost its provenance
  cannot say so. `supplier_parts` rows already carry `source`/`source_url`;
  a seeded row needs a `licence` beside them.
- **Mixed licences cannot be one undifferentiated list.** MIT, CC BY and
  CC BY-SA impose different obligations downstream, and the listing already
  states `fidelity` and `geometry` per row for exactly this reason — a field
  present on only some rows is one a caller eventually reads wrong.
- **These are boards, not mechanisms.** The catalogue value is enclosure and
  clearance work: hole patterns, connector positions, the height of the tallest
  component. That is a real gap in the generated catalog and it is not the
  fastener/bearing/extrusion gap, which the standards already cover.

## What is built instead

`services/supplier_parts.py`. The person who accepted a portal's terms downloads
the STEP under their own account and imports it; it becomes a purchased part
**of theirs**, scoped to their account and never shared or re-published.

- `POST /api/documents/meta/supplier-parts` (multipart) — import and file it.
- `DELETE /api/documents/meta/supplier-parts/{key}` — forget the row, keep the
  document.
- `list_standard_parts` / `GET /meta/standard-parts` return one list: generated
  parts plus this account's imports, every row carrying `fidelity`
  (`as-standard` / `envelope` / `supplier`) and `geometry` (`exact` / `mesh`).
- `insert_standard_part` takes either kind of key.

The portal links in the listing's `sources` are **searches**. A hyperlink is not
a copy, and nothing in this system reaches into a portal on a user's behalf.

## If this is revisited

The thing to check is not "is there an API" — TraceParts has one and it does not
help. It is whether a supplier will grant *written consent to redistribute*, and
that is a commercial conversation with a named manufacturer, not an integration.
A single manufacturer agreeing to have their catalog generated into this system
would be worth more than any portal, because it comes with the right to keep the
geometry.

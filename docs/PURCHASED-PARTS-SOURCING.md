# Where purchased-part CAD can come from

Written up because the question keeps returning and the answer is not obvious
from the outside: the big CAD portals look like an API away and are not. This
records what was checked, on 2026-08-09, so the next person does not re-derive
it — and so that if a portal's terms change, there is a baseline to compare
against.

**Reanalysed 2026-09-07**, when the posture was settled: this is an *internal*
tool. That is not a footnote. Half the verdicts below were reached by asking
"may we redistribute this?", which is the wrong question for a tool nobody
outside the organisation runs — and the other half were reached by asking
"may we fetch this at all?", which the posture does not change. The two halves
are now marked as such, because the thing that will eventually make this
document wrong is the posture changing, not a licence changing.

## The posture this rests on

- **An internal tool for RC, drone and robotics work**
  ([`roadmaps/RC-DRONE-ROBOTICS.md`](roadmaps/RC-DRONE-ROBOTICS.md)), not a CAD
  product competing for seats.
- **The repository is private** and the code is not published. Neither is any
  part library built inside it.
- **Nothing is offered to anyone outside the organisation** — not as a hosted
  service to customers, not as a download, not as a data set.

Every conclusion below is conditional on all three. [If this ever leaves the
building](#if-this-ever-leaves-the-building) is the checklist for the day one of
them stops being true, and it is written first-person-plural on purpose: the
work of unwinding this falls on whoever is here then.

This is engineering judgment about which questions to ask, not legal advice.
The rule it applies is a boundary anyone can check — *did a copy leave?* — and
where the answer is close, the entry says so rather than rounding it down.

## The two questions, and why they answer differently

Almost every confusion in this area comes from treating two unrelated
restrictions as one. They fail at different moments:

| | Triggered by | Internal-only use | Examples here |
|---|---|---|---|
| **Copyright licence** (GPL, CC BY, CC BY-SA, MIT, OCL, CERN-OHL) | *Distributing* — conveying a copy to someone outside the licensee. Also *public* performance/sharing. | **Does not trigger.** An organisation using something across its own machines is one licensee, not a distribution chain. Obligations sit dormant, not discharged. | BOLTS, FreeCAD Fasteners, SparkFun, KiCad, Pixhawk |
| **Contract / terms of use** (a portal's ToU, an API agreement, an account you clicked through) | *Using it at all*, on the terms you accepted. | **Triggers exactly the same.** A contract does not care whether you redistribute; it binds because you agreed to it. | TraceParts, 3Dfindit, McMaster-Carr, GrabCAD |

So the reanalysis moves in one direction only. **Copyleft stopped being a
barrier; the portals did not.** Anything that was blocked by a licence is now
available; everything that was blocked by *access* — robots.txt, a client
certificate, an API agreement — is blocked exactly as hard as it was.

Two riders on the copyright row, because both bite in practice:

- **AGPL is the exception.** Its trigger is network use, not distribution, so a
  tool serving pages to a browser conveys under it. Nothing considered here is
  AGPL; check before adding something that is.
- **Dormant is not discharged.** GPL data mixed into this codebase makes the
  codebase undistributable until it is removed. That is a decision to accept a
  future cost, and it is only sane if the cost is recorded where whoever pays it
  will find it — which is what the provenance fields below are for.

## The question

`services/parts_catalog.py` generates 210 purchased parts from the published
dimensional standards. That is a good floor and a hard ceiling: an ISO 4762
M4×16 is an ISO 4762 M4×16 whoever makes it, but no amount of standards work
produces *this* Igus bearing, *that* Festo cylinder, or the connector whose
footprint decides whether the enclosure lid closes. R2 sharpens it — a
brushless motor, an ESC, a flight controller and a LiPo are all *this one*, not
a class.

Those models exist, on 3Dfindit and TraceParts and the manufacturers' own sites.
The question is whether this system may fetch them.

## 3Dfindit (CADENAS) — no, and internal use does not change it

| Checked | Result |
|---|---|
| `https://www.3dfindit.com/robots.txt` | `User-agent: ClaudeBot` → `Disallow: /`. Also Amazonbot, Bytespider, CCBot, GPTBot, Google-Extended, meta-externalagent. `Content-Signal: ai-train=no, use=reference`. |
| `GET https://www.3dfindit.com/` from a non-browser client | `403` |
| A documented HTTP API returning geometry | None found. The published interfaces are CAD-host plugins (NX, SolidWorks, Inventor, AVEVA E3D), desktop apps (PARTdataManager, PARTdesigner) and a C++ SDK. The one public repo, `cadenasgmbh/3dfindit-freecad-integration`, is archived. |
| Download gating | Free account, fair-use cap of 50 download tickets a day. |
| Who owns the models | The manufacturers who publish them. CADENAS hosts. |

The robots.txt naming this crawler and the 403 settle it before the licensing
question is even reached — which is precisely why the internal posture does not
rescue it. **A crawl is a use, not a distribution.** An engineer downloading a
part through the browser under their own account is the intended path and always
was; automating it from this system is not.

## TraceParts — narrower than it looked, still not a seed

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

**What the internal posture changes, and what it does not.** Clause (i) turns on
*external*, and an internal tool is not an external service; clause (iv) turns on
*competing*, and we are not in that market. Those two relax. Clause (iii) does
not — "distribute the Data on any storage medium whatsoever" is broad enough to
read on a shared internal library, and clause (ii) reads on an internal web app
without much strain. The contact-capture obligation is unaffected and is
per-downloading-user, which an account-shared cache defeats by construction.

So the conclusion survives with a different reason under it: a legitimate
TraceParts integration is a **per-user pass-through** — the user is identified to
TraceParts, fetches one file for one use, and it is not retained as shared
library content. It is not built. If it ever is, it must not write rows into
`supplier_parts` without written consent, because that is the line clause (iii)
sits on and consent is cheap to ask for compared with unwinding a seeded catalog.

## McMaster-Carr — unchanged

`robots.txt` disallows `/catalog/*` and the API is gated behind a per-customer
client certificate (see the `parts_catalog` module docstring). Both are access
controls, so both are in the column the posture does not move. An engineer with
an account downloads a part; this system does not.

## The open libraries — the trap was distribution, and we are not distributing

This section previously read "a copyleft trap" and told you to stay away. Under
the internal posture that was **wrong in the strict sense**: GPL obligations
attach to conveying a copy, and nothing here is conveyed.

| Project | Licence | Verdict, internal | What it would cost to publish later |
|---|---|---|---|
| [BOLTS](https://github.com/boltsparts/BOLTS) | GPL-3.0 | Usable. Consulting or transcribing its dimension tables into a private, undistributed codebase conveys nothing. | The codebase becomes undistributable until the derived tables are removed or the whole is released under GPL-3. |
| [FreeCAD Fasteners WB](https://github.com/shaise/FreeCAD_FastenersWB) | GPL-2.0 | Same. | Same, under GPL-2. |
| [NopSCADlib](https://github.com/nophead/NopSCADlib) | GPL-3.0 | Same. Vitamins (motors, bearings, extrusion, fans) are closer to R2's list than the fastener libraries are. | Same. |

Two things stop this from being a green light:

- **It is worth less than it looks.** `parts_catalog` already generates from the
  standards, and a dimension in a standard is a fact — nobody's content, no
  licence attached, and the reason that generator was the right call whatever
  the posture. What these libraries add is *coverage* (DIN and ANSI ranges the
  standards work has not reached) and, more valuable, an **independent
  cross-check**: two derivations of the same table that disagree have found a
  bug in one of them. Reading them for verification is the cheapest use and
  carries the least to unwind.
- **Every table taken this way is a lien on the codebase.** Dormant, not
  discharged. Record it (below) or the person who later wants to open-source or
  spin out this tool has to re-derive which numbers came from where — from a
  diff, years late, which is exactly the archaeology this document exists to
  prevent.

The sorting rule from [`roadmaps/HANDOFF-R2.md`](roadmaps/HANDOFF-R2.md) is
unaffected and still the one to apply first: *table what is genuinely
standardised, parameterise what varies by manufacturer, never invent a number to
fill a table.* Prefer the standard; use the copyleft library to check it.

## Vendor libraries on GitHub — now mostly a question of what you record

Checked 2026-09-07, prompted by Adafruit's, which is the well-known one. A
manufacturer publishing its own product CAD is doing something a portal
structurally cannot: giving away the aggregation. Under the internal posture the
column that used to decide these rows — *may this be redistributed as a
collection?* — decides nothing, because we are not redistributing. What is left
is the licence's weight **if the posture changes**, which is a real cost with a
known price rather than a barrier.

| Publisher | Where | Licence | Internal use | If it ever ships |
|---|---|---|---|---|
| [Adafruit](https://github.com/adafruit/Adafruit_CAD_Parts) | `adafruit/Adafruit_CAD_Parts` | **MIT** (verified in the repo's `LICENSE`) | Yes | Free — keep the copyright notice. The cheapest row here. |
| [SparkFun](https://github.com/sparkfun/3D_Models) | `sparkfun/3D_Models` | **CC BY-SA 4.0** models, MIT code | Yes | Attribution, and adaptations of a model stay CC BY-SA. The *codebase* is unaffected either way: these are data files, not something compiled in. |
| [Framework Computer](https://github.com/FrameworkComputer/Framework-Laptop-16) | `Framework-Laptop-{12,13,16}`, `ExpansionBay`, `ExpansionCards` | **CC BY 4.0** | Yes | Attribution only. Whole-system STEP, published so third parties can build against it. |
| [KiCad](https://github.com/KiCad/kicad-packages3D) | `KiCad/kicad-packages3D` | CC BY-SA 4.0 + design exception | Yes | Redistribution as a collection is outside the exception, so CC BY-SA applies. Generic package bodies (SOT-23, TO-220), not products — useful for board-level clearance, not for identifying a part. |
| [Digi-Key](https://github.com/Digi-Key/digikey-partner-kicad-library) | `Digi-Key/digikey-partner-kicad-library` | CC BY-SA 4.0 + the same exception | Yes | As above. Symbols and footprints, **not solids** — a part-number spine, not geometry. |
| [Seeed Studio](https://github.com/Seeed-Studio/OPL_Kicad_Library) | `Seeed-Studio/OPL_Kicad_Library` | KiCad-style library licence | Yes | As above. Seeed's STEP models are scattered across SnapMagic and shareCAD rather than collected. |
| [Voron](https://github.com/VoronDesign) | `VoronDesign/*` | GPLv3 | Yes | A lien, like BOLTS. Printer parts as STEP/F3D. |
| [Prusa](https://blog.prusa3d.com/core-one-cad-files-release-under-the-new-open-community-license-ocl_127290/) | GitHub + prusa3d.com | GPL (older printers); **Open Community License** (CORE One) | **Yes — and the OCL says so in as many words**, granting inspection, modification and spare parts for *personal or internal business use*. Under the old framing this was the one row that read "No". | No. The OCL grant stops where redistribution starts. |
| Raspberry Pi | [`raspberrypi/documentation`](https://github.com/raspberrypi/documentation/) (CC BY-SA 4.0); STEP on [pip.raspberrypi.com](https://pip.raspberrypi.com/) | Mixed | Per-file. The repo is prose; the board STEP sits on the product portal under its own terms. | Per-file. |
| [Pololu](https://www.pololu.com/) | Per-product "Resources" tab | Per-file | Yes, one product page at a time. No repo, no bulk. | Per-file. |
| [OpenBuilds](https://builds.openbuilds.com/projectresources/step-parts-library.162/) | builds.openbuilds.com, GrabCAD | **Unstated** | Grey, and the honest reading is that silence is not a grant. Using a downloaded V-slot STEP to design a machine is the published intent; folding the set into a shared library is not covered by anything written down. Ask, or model the extrusion from its published section — it is a profile, and `parts_catalog` already builds T-slot. | No. |

[SnapMagic Search (SnapEDA)](https://www.snapeda.com/get-api/) sits with the
portals rather than here: CC BY-SA 4.0 with Design Exception 1.0 on the models,
but an account and API terms in front of them. The licence permits internal use;
read the terms before automating a fetch, for the same reason TraceParts does not
flip.

## The domain libraries R2 will actually want

The vendor repos above are mostly *boards*. R2 needs brushless motors, ESCs,
flight controllers, servos, props, LiPos and standoffs, and the open sources for
those are a different set — worth recording now so R2 does not re-derive it:

| Source | Licence | What it gives |
|---|---|---|
| [Pixhawk / PX4 hardware](https://github.com/pixhawk/Hardware) | CC BY-SA 3.0, OSHW 1.1 | FMU reference designs and board outlines. The **30.5×30.5 pattern is a de-facto convention**, i.e. a fact — parameterise it per the R2 rule rather than copying anything. |
| [Poppy `Robotis-library`](https://github.com/poppy-project/Robotis-library) | CC BY-SA 4.0 | Dynamixel AX/MX/RX servo bodies as SolidWorks parts. The official ROBOTIS drawings are on their download page under their own terms. |
| [OpenArm](https://github.com/enactic/openarm) | CERN-OHL-S-2.0 | A full 7-DOF arm as STEP/STL/F3D. OHL-S is strongly reciprocal on *conveyed hardware and designs* — same dormant-lien shape as GPL, and worth knowing before it is used as a reference geometry. |
| ROS description packages — [UR](https://github.com/UniversalRobots/Universal_Robots_ROS2_Description) (BSD-3/Apache-2.0, UR's own terms on the meshes), [franka_ros](https://github.com/frankarobotics/franka_ros) (Apache-2.0), Robotiq | Permissive | Arm and gripper geometry **with real joint frames**, which is the half `export_urdf` cares about. The catch is format: STL/DAE meshes arrive through `import_service`'s voxel path as an opaque `ImportFeature`, so no face is addressable. Vendor STEP, where it exists, is what to import. |
| [step.parts](https://github.com/earthtojake/step.parts) | MIT for the project, **models keep their own licences** (`THIRD_PARTY_NOTICES.md`) | ~12k aggregated open STEP parts with a public API at `api.step.parts/v1`. Treat it as an index, not a licence: a row's terms are whatever its upstream says, so anything taken from it must carry that upstream through into the provenance fields below. |

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

Owner-scoping is worth keeping even internally, and not for licence reasons:
`add_component` refuses a part its owner cannot open, and a row names a
`document_id`. Making the import path organisation-wide is a real feature and a
separate decision from this document.

## What to record on a seeded row

Seeding the catalog from the sources above is now available, and the thing that
makes it safe is boring: **every seeded row records where it came from and under
what licence**, whether or not today's posture requires attribution. Three
conditions, none optional:

- **Provenance is a stored field, not a footer.** `supplier_parts` rows already
  carry `source` and `source_url`; a seeded row needs `licence` beside them, and
  for anything from an aggregator, the *upstream* licence rather than the
  aggregator's. The obligation is dormant, so nothing enforces this — which is
  exactly why it has to be written down at the moment the row is created, when
  the answer is known.
- **Mixed licences cannot be one undifferentiated list.** MIT, CC BY, CC BY-SA,
  OCL and GPL impose different obligations the day the posture changes, and the
  listing already states `fidelity` and `geometry` per row for the same reason —
  a field present on only some rows is one a caller eventually reads wrong.
- **Vendor board CAD is enclosure work, not mechanism work.** Adafruit's and
  SparkFun's value is hole patterns, connector positions and the height of the
  tallest component. That is a real gap and it is not the
  fastener/bearing/extrusion gap, which the standards already cover, nor the
  motor/ESC/prop gap, which is R2.

> **It has left the building (2026-09-27).** The repository is public and MIT
> licensed, so every "does not trigger" in the table below that depended on not
> distributing has flipped. The checklist in this section was run rather than
> filed; the outcome is recorded at the end of it, and `NOTICE` at the repository
> root is the inventory it produced. Read the posture section above as history.

## If this ever leaves the building

The posture changing is what makes this document wrong, so here is the checklist
in advance. Any of these counts: publishing the repository, open-sourcing part of
it, offering it to customers as a hosted service, shipping a copy to a partner or
a contractor working on their own account, or spinning the tool out.

1. **Inventory by licence** from the provenance fields — the reason they exist.
2. **Remove or release anything copyleft.** GPL-derived tables and CERN-OHL-S
   derived geometry are the liens: strip them (the standards-generated
   equivalents are already there, which is the argument for keeping the
   generator authoritative) or release the whole under that licence.
3. **Attribute the CC rows,** and keep CC BY-SA derivatives under CC BY-SA.
4. **Re-read every portal's terms as if new.** Contract terms bound us all along,
   and the clauses that relaxed under "internal" — *external service*,
   *competing* — snap back first.
5. **Ask counsel before, not after.** Everything above is about which questions
   to ask; the answers are worth money at that point.

### What running it actually found (2026-09-27)

1. **Inventory by licence** — `NOTICE`. Four kinds of third-party material, and
   none of it vendored: Adafruit (MIT, licence text shipped, models fetched at
   run time), Zero-To-CAD (Apache-2.0, converter only, dataset never committed),
   SketchGraphs (Onshape ToU §1.g.ii, which grants distribution outright), and
   dimensions from the standards, which are facts.
2. **Remove or release anything copyleft** — **nothing to do**, and this is the
   finding that made publishing possible at all. The GPL libraries were used only
   as an independent cross-check on the generated tables. Had a single dimension
   table been transcribed, this step would have been "strip it or relicense the
   whole repository under GPL-3", and the standards-generated equivalents being
   authoritative is exactly what bought the cheap answer.
3. **Attribute the CC rows** — none. No CC BY-SA geometry was ever taken.
4. **Re-read every portal's terms as if new** — no change needed, because nothing
   here calls a portal. `SOURCES` deep-links a *search*, and a hyperlink is not a
   copy. The clauses that relaxed under "internal" (*external service*,
   *competing*) no longer relax, which costs nothing today and constrains what may
   be added tomorrow.
5. **Ask counsel before, not after** — not done. This section is a record of which
   questions were asked and what the repository contains; it is not legal advice
   and does not stand in for it.

The standing rule is unchanged and is now load-bearing rather than merely cheap:
prefer the standards, because a fact carries no licence at all. What *is* new is
that a sourced row must record a licence permitting redistribution, not just
record a licence.

## If this is revisited

The thing to check is not "is there an API" — TraceParts has one and it does not
help. It is whether a supplier will grant *written consent to redistribute*, and
that is a commercial conversation with a named manufacturer, not an integration.
A single manufacturer agreeing to have their catalog generated into this system
would be worth more than any portal, because it comes with the right to keep the
geometry — and, under the internal posture, it is the one thing that would still
be worth having if the tool ever ships.

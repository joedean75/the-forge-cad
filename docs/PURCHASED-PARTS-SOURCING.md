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

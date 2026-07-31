# The Forge — Product Roadmap

_Product strategy and market read, July 2026._

Three waves of work, in the order they were done. **Everything in waves one and
two has shipped**; the third is in progress.

| Wave | What | State |
|---|---|---|
| One — market wedge | Copilot, universal import, instant quotes, agent platform, real-time collaboration | shipped |
| Two — production workflow | the ten standard-CAD features (materials, parameters, assemblies, drawings, GD&T, revisions …) | shipped |
| Three — exact kernel | OpenCascade alongside the SDF, edge selection, exact import, design specs, topology DFM, real FEA | mostly shipped — see [docs/SPRINT-ROADMAP.md](docs/SPRINT-ROADMAP.md) |

For what the system *does today* rather than what was planned, read
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md); `CLAUDE.md` is authoritative on
the kernel's behaviour and the reasoning behind it.

## Thesis

The generative-design and AI-CAD markets are compounding fast, but every serious
buyer wants the two things AI still fumbles: **parametric editability** and
**manufacturability**. The Forge already produces editable parametric feature
trees where generative + parametric share one model, and exposes an **AI-native
operation API** most rivals are only now retrofitting. The roadmap doubles down
on that seam.

## Market read

| Signal | Figure | Note |
|---|---|---|
| Generative-design software (2026) | **$3.16B**, 18.5% CAGR | design side |
| Generative AI in design (2026) | **$1.52B**, 37.5% CAGR | fastest-growing slice |
| CNC machining market | **$1.8B → $11.8B** (2026→2035) | the "make" demand quoting rides |
| MCP monthly installs | **97M** in 16 months | agent-tooling is now standard |

**Four field signals**

1. **Agents move from generating to orchestrating.** MCP at 97M installs;
   Claude for Creative Work acts inside Fusion/Blender; SimScale ships autonomous
   sim-agents. The frontier is a copilot that _takes multi-step action_.
2. **Text-to-CAD's unsolved core is editable + makeable.** AdamCAD ($4.1M seed,
   YC W25, 1M+ models) and Zoo are hot, yet research is blunt that engineering
   demands the editability + manufacturability only parametric CAD provides; the
   parametric↔B-Rep split is "a fundamental bottleneck."
3. **Quoting is moving into the CAD canvas.** Xometry ships instant-quote + DFM
   add-ins for Fusion/SOLIDWORKS/Onshape; Fictiv pairs quotes with AI DFM on
   upload; Siemens is embedding Xometry in Designcenter.
4. **Distribution is a plugin/API, not a destination.** AdamCAD's key move was an
   Onshape plugin; Zoo bets developer-first on API + kernel.

## The five wave-one features

All five have shipped. They are kept below as written — including the original
`_NOW_` / `_NEXT_` / `_LATER_` priority markers — because the reasoning is the
record of *why* each was built in that order.

### 01 · Forge Copilot — the agentic engineering loop  · _NOW_
A conversational agent that plans and **executes** across model → optimize →
validate → DFM, verifies its own output (runs a sim / DFM check) and iterates,
with human approve & undo on every step.
- **Why now:** agentic AI is 2026's inflection; rivals do single-shot generation —
  closed-loop verification is the wedge.
- **Builds on:** `cad_operations.dispatch` (serializable op path), `llm_client`,
  the MCP server, FEA + DFM services, the unified document.
- **Success:** ≥40% of new parts touched by the copilot; median ≥3 chained ops/session.

### 02 · Universal import — existing parts, editable  · _NOW_
Upload STEP / STL / OBJ → becomes a live document body via the mesh→SDF bridge:
measurable, optimizable, DFM-checkable, machinable, assemblable.
_Wave three went further than this: STEP and IGES now import **exact**, with
addressable faces and edges. Only meshes take the SDF bridge._
- **Why now:** interoperability is the #1 pro-adoption wall; unlocks "optimize /
  quote the part I already have."
- **Builds on:** the MeshSDF / GridSDF bridge already used for generative results;
  the documents pipeline; STEP export (round-trips).
- **Success:** import→first-op < 60s; ≥25% of documents originate from an upload.

### 03 · Instant DFM + live quotes — "Get it made"  · _NEXT_
Sharpen per-process AI DFM and add a pluggable supplier-quote / order provider:
model → price + lead time → order without leaving The Forge.
- **Why now:** instant-quote marketplaces + in-CAD add-ins are the pattern; direct
  revenue via markup/referral on a $1.8→11.8B make-side.
- **Builds on:** `services/dfm.py` (DFM + quote), the "Make" mode, STEP export, the
  opt-in provider pattern proven in `billing.py`.
- **Success:** first paid quote provider attached; quote→order tracked as revenue.

### 04 · Agent platform — public API, MCP-first, embeddable  · _NEXT_
Productize the operation API + MCP server as a documented, key-authed platform —
"the agent-native CAD backend" — plus an embeddable "generate in your CAD" widget.
- **Why now:** distribution is a plugin/API; MCP is the rail buyers already speak.
- **Builds on:** the existing MCP server, per-account API keys, the versioned
  operation catalog — most of the surface already ships.
- **Success:** first external agent/app builds on the API.

### 05 · Real-time collaboration + PDM  · _LATER_
Multiplayer editing, live presence/cursors, operation broadcast over WebSocket,
branch/merge on the op stream.
- **Why now:** real-time multiplayer + branching is the moat cloud incumbents
  charge $1.5–2.5k/seat/yr for; teams expect it.
- **Builds on:** role-based sharing + version history, the serializable op stream.
  Gated on externalizing in-process state.
- **Success:** two users co-edit one document live; enterprise seat expansion.

## Sequencing

- **Now** (wedge + adoption): 01 Forge Copilot, 02 Universal import.
- **Next** (revenue + reach): 03 Instant quotes, 04 Agent platform.
- **Later** (infra moat): 05 Real-time collaboration, ahead of the hybrid B-rep
  kernel — which has since shipped (see below).

---

# Wave two — production workflow: ten standard-CAD features

_Added July 2026. Roadmap features 01–05 above have all shipped; this is the next
horizon: closing the gap from "generates and makes parts" to a **complete
professional design-to-manufacturing workflow**, grounded in how mechanical CAD
is actually used._

> **Status: all 10 SHIPPED** (July 2026). Every item below is implemented with
> a backend service, an HTTP endpoint, an agent operation, and tests. See the
> commits `34db7bc` (materials/parameters/mass-properties/configurations),
> `e081237` (mates/interference/BOM), and `e7c2ee7` (drawings/GD&T/revisions).

## The standard workflow, and where The Forge stands

Industry sources describe one consistent pipeline: **concept → preliminary design
→ detail design & optimization → validation (FEA) → documentation (2D drawings,
GD&T, BOM) → manufacturing handoff (CAM/CNC/print) → data management (PDM/PLM)**.
Mapping The Forge onto it shows the strengths are real and the gaps are
concentrated in two places — **documentation** and **design-intent / data rigor**:

| Workflow stage | The Forge today | Gap |
|---|---|---|
| Concept / preliminary | Text-to-CAD (parts **and** assemblies), Copilot | strong |
| Detail modeling | Parametric feature tree, sketches-on-face | no driving parameters / configs |
| Optimization | Real SIMP generative, editable result | strong |
| Validation | FEA-style sim, DFM | no tolerance/stack-up analysis |
| **Documentation** | measurement dimensions only | **no 2D drawings, GD&T, or BOM** |
| Manufacturing handoff | quotes, CNC CAM, FDM slicer + print | strong |
| **Data management** | version snapshots, live collab | **no revisions / released states** |

The Forge's differentiator holds throughout: every feature below is designed to
be **agent-accessible through the operation API + MCP**, so the copilot and
external agents gain each capability the moment humans do.

## The ten, by leverage

Ordered by how much of the professional workflow each unlocks, weighted toward
features that build on what already ships.

### 01 · 2D engineering drawings (drafting)
The authoritative manufacturing document — dimensioned orthographic + section +
detail views, title block, notes, revision block; export to **PDF / DXF / DWG**.
Every serious CAD tool has this; The Forge has none, and the 2D drawing (not the
model) is still what the shop floor and QA inspect.
- **Builds on:** the SDF kernel (silhouette + planar-section curves already used
  by the sketch backdrop), the document's derived cache, dimensions.
- **AI hook:** `generate_drawing` op — an agent produces a shop-ready print.

### 02 · GD&T & tolerances
Datums, feature control frames, and tolerance callouts (limit/fit/±) on the
drawing and model; the language every manufacturer quotes and inspects against.
Pairs with 01 — together they are the manufacturing data package.
- **Builds on:** the drawing view stack (01), the datum/face system already in
  the kernel (`datum.py`, FACE_MAP), the DFM service.
- **AI hook:** DFM/quote consume tolerances; copilot can apply a fit ("H7/g6").

### 03 · Global parameters & equations
Named driving variables and relations (`width = 100`, `hole_d = width / 10`)
that propagate through the tree. This is the backbone of *design intent* — and
what makes text-to-CAD edits, configurations, and part families robust instead
of one-off geometry.
- **Builds on:** the feature tree + `set_feature_params`; promote today's
  point-to-point dimensions into driving parameters.
- **AI hook:** the copilot edits *intent* ("make it 20% wider"), not raw coords.

### 04 · Assembly mates / joints
Replace raw placement transforms with real constraints — concentric, coincident,
fasten, revolute, slider — that define fit and motion. The standard way
assemblies are built (Onshape mates, Fusion joints), and the foundation for
motion studies and interference checks.
- **Builds on:** the new assembly-composition model (`AssemblyComponent` +
  `ComponentTransform`) and text-to-assembly; solve mates → transforms.
- **AI hook:** text-to-assembly emits mates, not just positions.

### 05 · Bill of Materials (BOM)
Auto-generated parts list from an assembly — quantity, material, mass, part
number — with CSV/PDF export and a live tie to instant quotes ("quote the whole
assembly").
- **Builds on:** `AssemblyDoc` components, the materials work (06), the quote
  service (feature 03 above).
- **AI hook:** `export_bom`; an agent costs a whole assembly in one call.

### 06 · Materials library & assignment
A real material catalog (density, modulus, yield, cost) assignable per part,
driving mass, simulation, DFM, and quotes — replacing today's hard-coded
aluminium density.
- **Builds on:** `_DEFAULT_DENSITY` and the quote material map already exist;
  generalize into a catalog + a `material_id` on the document.
- **AI hook:** `set_material`; quotes and sim reflect the real spec.

### 07 · Configurations / part families
One model, many sizes — a parameter table producing variants (M3/M4/M5 of the
same bracket) without duplicating the tree. Standard for catalog and hardware
work.
- **Builds on:** global parameters (03) + the evaluate-with-overrides path.
- **AI hook:** "give me this in three sizes" → a configured family.

### 08 · Interference & clearance detection
Assembly validation: flag colliding solids and sub-minimum gaps between placed
parts. The check every assembly ships through before release.
- **Builds on:** per-part SDFs are already evaluated; sample one part's field at
  another's occupied cells for exact penetration/clearance.
- **AI hook:** the copilot self-checks an assembly it just built.

### 09 · Revision & release management (PDM)
Named revisions (A/B/C), released / in-review / obsolete states, and a change log
above raw version snapshots — so a document can be *released* and referenced with
confidence. The data-management stage the workflow ends on.
- **Builds on:** the existing version history + collaboration/roles; add a
  release state machine and immutable released revisions.
- **AI hook:** agents read state ("is rev B released?") before acting.

### 10 · Mass properties & inspection
Center of gravity, moments of inertia, surface area, plus section analysis and
true point/edge/face measurement — the quantitative read engineers check
constantly.
- **Builds on:** the evaluated mesh + existing mass/volume; add COG/inertia
  integrals and a section-plane tool.
- **AI hook:** `mass_properties` query for verification loops.

## Sequencing

- **Documentation package first** (biggest gap, most-requested): 01 drawings →
  02 GD&T → 05 BOM. This alone moves The Forge from "makes parts" to "ships a
  manufacturing data package."
- **Design-intent rigor** (multiplies everything else): 03 parameters →
  07 configurations → 06 materials.
- **Assembly maturity:** 04 mates → 08 interference → 10 mass properties.
- **Release the work:** 09 revision/release management, alongside the collab
  infra already shipped.

Each is scoped to build on shipped infrastructure and to expose an operation so
the agent platform inherits it automatically — keeping the AI-native edge while
closing the professional-workflow gap.

## Sources

- Mechanical CAD Services for Product Design (workflow stages) — Endeion
- Mechanical Product Design: 6 Steps Concept to Product — 3DDFM
- 4 Ways AI Design Will Reshape the CAD Workflow in 2026 — Design & Development Today
- Mastering Tolerances / GD&T in 2D Drawings — MLC-CAD; GD&T — Autodesk, Formlabs
- The Ultimate CAD Drawing Guide (formats, GD&T, shop-floor readiness) — Tuofa
- SOLIDWORKS vs Onshape vs Fusion 360 (mates, BOM, drawings, revisions) — TriStar, GaugeHow, DillaDev
- How to create an engineering drawing — Engineering LibreTexts (Fusion 360)
- Generative Design Software Market Report 2026 — The Business Research Company
- Generative AI in Design Market — GlobeNewswire, Jul 2026
- Best Online CNC Machining Services 2026 — Jiga (CNC market trajectory)
- MCP adoption & Claude for Creative Work — MecAgent
- Agentic AI in Engineering — SimScale
- AdamCAD review (funding, 1M+ models, Onshape plugin) — 2026; Why AI Must Generate Parametric CAD — Zoo
- LLM-driven CAD + B-Rep grounding (parametric↔B-Rep bottleneck) — arXiv 2026
- Xometry CAD Add-ins; Siemens + Xometry (Designcenter) — Engineering.com
- Onshape plans & pricing

_Market figures are analyst estimates and vary by scope; treat as directional._

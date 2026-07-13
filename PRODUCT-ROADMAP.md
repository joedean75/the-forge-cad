# The Forge — Product Roadmap (Market Read & Next 5 Features)

_Product strategy, July 2026. Companion to the technical `ROADMAP.md` (kernel horizons)._
_Interactive version: published as a Claude artifact._

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

## The next five features

Ordered by leverage; each builds on something The Forge already has.

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
- **Later** (infra moat): 05 Real-time collaboration, ahead of the Horizon-2
  hybrid B-Rep kernel (`ROADMAP.md`).

## Sources

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

# Forge → Professional CAD Suite — Roadmap

A staged plan from the current implicit-kernel parametric editor to a
professional-grade parametric + generative CAD suite, with an **AI-native operation API**
maintained as a first-class concern throughout.

## Where we are

A **document-centric parametric editor** on an **implicit (SDF) kernel**:

- Primitives + booleans + modifiers (fillet-as-blend, shell, hole, pattern, transform).
- A real **generative topology optimizer** (SIMP), meshed via marching cubes.
- **Text-to-CAD**: LLM emits full feature trees + rule fallback + self-repair + conversational **AI edit**.
- Coarse FEA, DFM/quote, 2.5D CAM, Moonraker (Klipper) machine control.
- Dimensions/measure with feature snapping; part library.
- The **feature tree is JSON** — already the AI's interface.

## Gaps vs. established CAD suites

1. **Kernel is implicit, not B-rep.** No exact NURBS faces, no stable topological
   entities (faces/edges/vertices) to name, select, and reference. Blocks
   edge-select fillets, up-to-face features, precise GD&T, true STEP round-trip.
   **Biggest lift.**
2. **No sketch + constraint solver** — the heart of parametric CAD.
3. **No *exact* topological naming** — the SDF field has no persistent
   face/edge entities, so a reference can't survive an operation that
   re-topologizes geometry (CAD's famously hard problem). *Partially bridged:* a
   pragmatic **datum layer** now resolves symbolic face references (`sketch on
   the top face of feature X`) against the referenced feature's *live* bounding
   box at regeneration, so a sketch-on-face follows an upstream resize. True
   naming (surviving arbitrary re-topologizing) is gated on the Horizon-2 B-rep
   kernel.
4. **Static recipe, not a regeneration engine** — no reorder/suppress/rollback/error recovery.
5. **Part-centric, no real assemblies** — no instances, 3D mates, interference, BOM.
6. Coarse FEA; 2.5D-only CAM; no associative 2D drawings/PMI; mesh-based STEP,
   not true B-rep interop. *Commercial/SaaS foundation landed:* accounts +
   plans/entitlements + usage metering + **Stripe checkout/webhook billing**;
   **documents owned and strictly isolated per account** with **role-based
   sharing** (owner/editor/viewer, `document_service.access_role`) enforced on
   every route; **version history / restore / branch**; and a **durable
   SQLAlchemy datastore** (SQLite dev → Postgres prod via `DATABASE_URL`). Still
   missing: real-time co-editing (live cursors / OT-CRDT) and associative 2D
   drawings.

## Horizon 1 — solidify the editor + go AI-native (near-term)

Stay on the current kernel; make it a real editor and lock in the AI interface.

- **Regeneration engine:** feature dependency graph, reorder / suppress / rollback,
  per-feature error state. Undo/redo. Document **versioning** (snapshots → branches).
- **Feature references (lightweight):** ✅ symbolic `on_face` datum references
  ({feature_id, face}) resolved from live geometry at regen (`services/datum.py`
  + `feature_ops.resolve_datums`) — sketch-on-face survives upstream resize.
  Next: pattern-of-feature, reference points/axes.
- **Sketch MVP** on base planes/faces: ✅ lines/arcs/circles + rect, extrude
  (union/cut), sketch-on-face, editable/reopenable. ✅ **2D constraint solver**
  (`services/sketch_solver.py`: fixed/coincident/H/V/distance/parallel/perp/
  equal-length/radius/arc-radius/point-on-line; arcs carry implicit
  equal-radius). Next: revolve/sweep, splines, dimensioned-first workflow.
- **Assembly MVP:** multiple part instances + coincident/offset mates.
- **AI-native surface (the differentiator):**
  - **Operation API** — discrete, typed CAD operations (`add_primitive`, `set_feature_params`,
    `fillet`, `measure`, `query_features`…) that both the UI and an agent call. *(in progress)*
  - **MCP server** wrapping the operation API so any agent (Claude, etc.) drives Forge natively.
  - **Versioned, LLM-legible document schema**; reference entities by name/tag.
- Harden export (STL/3MF/STEP), materials, appearances.

## Horizon 2 — exact geometry + real sketching (mid-term)

- **Hybrid kernel:** integrate **OpenCascade (pythonOCC / OCP)** as the exact **B-rep**
  kernel *alongside* the SDF kernel; convert between them. Keep the generative/implicit
  superpower, gain exact faces/edges + topological IDs.
- **Solve topological naming** (stable IDs surviving regeneration) → enables
  **edge/face-selected** fillet, chamfer, shell, draft.
- **Full sketch-based modeling:** constraint solver + sweep/loft/up-to-face/draft.
- **True STEP/IGES import+export** (B-rep round-trip).
- **Assemblies:** 3D mate solver, interference, BOM.
- **Drawings MVP:** projected/section views + associative dimensions.

## Horizon 3 — professional depth + platform (long-term)

- **Real simulation:** FEA/thermal/modal (CalculiX/Code_Aster), CFD (OpenFOAM);
  feed the generative optimizer for a closed sim-driven-design loop.
- **Full CAM:** 3–5 axis, sheet-metal flatten/bend, toolpath sim, post-processor library.
- **Drawings / PMI / GD&T** complete.
- **Cloud collaboration**: real-time multiplayer, CAD branching/merging,
  PDM, permissions — a deliberate infra pivot, decoupled from kernel work.
- Extensibility (FeatureScript-like DSL), marketplace, rendering/appearances.

## Cross-cutting AI-native principles (hold through every horizon)

- **API-first:** every UI action is an API call the AI can make — one operation surface.
- **Deterministic regeneration:** same tree → same geometry, so the AI can reason and self-check.
- **Validate-and-repair on every AI mutation** (seed exists — keep extending).
- **Semantic/naming layer** so AI references "the mounting face," "the M4 holes."
- **Schema versioning** as the model grows, so old docs + AI prompts keep working.

## Pivotal decisions

- **Kernel:** custom-SDF (unique generative edge, never exact) vs. adopt-OCCT (exact,
  heavy C++ dep) vs. **hybrid** → *recommend hybrid.* The fork everything else depends on.
- **Constraint solver:** integrate (`planegcs`/SolveSpace) vs. build → *integrate.*
- **Topological naming:** budget real time — hardest problem, gates edge/face features.
- **Cloud/collab:** separate product track; don't let it block the engine.

## Current step

**Operation API + MCP server** over the current document model — low effort, high
leverage, makes the whole system agent-drivable today — in parallel with the
regeneration engine and sketch MVP.

- `services/feature_ops.py` — Python feature-tree manipulation (find / set-params / add / remove).
- `services/cad_operations.py` — typed operation catalog + dispatcher.
- `routers/operations.py` — `GET /api/operations` (discoverable tool schema) +
  `POST /api/documents/{id}/op` (dispatch). MCP server wraps these next.

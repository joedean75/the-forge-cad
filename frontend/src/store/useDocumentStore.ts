import { create } from 'zustand'
import type {
  AnalysisReport,
  DesignDocument,
  EdgeRef,
  EditorMode,
  FaceRef,
  FeatureTreeDict,
  OptimizeJob,
  OptimizationSetup,
  LoadCase,
  Objective,
  ReferenceReport,
  SpecReport,
  SpecRequirement,
  DfmProcess,
  DfmReport,
  CamResult,
  Vector3,
  CopilotResult,
  DocumentParameter,
  DerivedCache,
} from '../../../shared/types'
import { api } from '../api/client'
import { ensureIds } from '../util/featureTree'

const DEFAULT_RESOLUTION = 96

export interface SketchSpec {
  points: Record<string, unknown>[]
  lines?: Record<string, unknown>[]
  arcs?: Record<string, unknown>[]
  circles?: Record<string, unknown>[]
  constraints?: Record<string, unknown>[]
}

/**
 * Face/edge selection state.
 *
 * `kind` is what the body is currently being listed as, and it is exclusive:
 * every operation that consumes a selection takes faces *or* edges, never both,
 * so keeping one list avoids a UI that lets you build a selection no operation
 * can accept. `error` holds the PRECONDITION a body with no exact form returns —
 * a state to explain, not a failure to swallow.
 */
export interface TopologyState {
  kind: 'face' | 'edge'
  faces: FaceRef[]
  edges: EdgeRef[]
  selected: string[]
  loading: boolean
  error: string | null
}

interface DocumentState {
  doc: DesignDocument | null
  mode: EditorMode
  selectedFeatureId: string | null
  busy: boolean
  error: string | null
  optimizeJob: OptimizeJob | null
  sim: AnalysisReport | null
  dfm: DfmReport | null
  /** Which process `dfm` was asked for. The report's own `process` is a display
   *  label ('LPBF', 'MILLING'), so it cannot answer "is this report about the
   *  process now selected?" — and a stale report read as a current one is the
   *  whole problem. */
  dfmProcess: DfmProcess | null
  cam: CamResult | null
  past: HistoryEntry[]
  future: HistoryEntry[]
  undo: () => Promise<void>
  redo: () => Promise<void>
  sketchOpen: boolean
  sketchEdit: { featureId: string; sketch: SketchSpec; plane: string; depth: number; offset: number;
      /** A standalone SketchFeature has no depth of its own — the consumer owns it. */
      standalone?: boolean } | null
  /** True while the user is picking a sketch plane (a face in the viewport or a base plane). */
  sketchPick: boolean
  /** How a new sketch should be seeded once the overlay opens (from the pick step). */
  sketchPreset: { onFace?: { featureId: string; face: string }; plane?: string } | null
  openSketch: (edit?: { featureId: string; sketch: SketchSpec; plane: string; depth: number; offset: number;
      /** A standalone SketchFeature has no depth of its own — the consumer owns it. */
      standalone?: boolean }) => void
  closeSketch: () => void
  /** Enter face/plane-picking mode (viewport + feature tree) before the sketch opens. */
  startSketchPick: () => void
  cancelSketchPick: () => void
  /** Open a new sketch attached to a feature's face (from a viewport or tree click). */
  pickSketchOnFace: (featureId: string, face: string) => void
  /** Open a new sketch on a base datum plane (xy/xz/yz). */
  pickSketchPlane: (plane: string) => void

  /** Faces/edges the body currently offers, and which of them are picked. */
  topo: TopologyState
  /** List the body's faces or edges (clears the selection when the kind changes). */
  loadTopology: (kind: 'face' | 'edge') => Promise<void>
  toggleTopoSelection: (id: string) => void
  /** Select every listed entity, or none. */
  setTopoSelection: (ids: string[]) => void
  /** Apply a treatment to the current selection and re-list the new body. */
  applyTopology: (op: 'fillet' | 'chamfer' | 'shell', sizeMm: number) => Promise<void>
  /** Which stored selections still resolve — see `check_references`. */
  refs: ReferenceReport | null
  checkReferences: () => Promise<void>

  /** The design spec and the last score against it. */
  specReport: SpecReport | null
  saveSpec: (requirements: SpecRequirement[], goal?: string) => Promise<void>
  verifySpec: () => Promise<void>

  setMode: (mode: EditorMode) => void
  select: (id: string | null) => void
  clearError: () => void
  runOptimize: (opts: {
    volume_fraction?: number
    objective?: Objective
    resolution?: number
    load_cases?: LoadCase[]
  }) => Promise<void>
  /** Linear-static FEA. `elements` raises the mesh budget: slower, finer stress. */
  runSimulate: (elements?: number) => Promise<void>
  runDfm: (buildAxis?: string, process?: DfmProcess) => Promise<void>
  runCam: (tool: { diameter_mm: number; stepdown_mm: number; stepover_mm: number; feed_mm_min: number }) => Promise<void>
  exportStep: () => Promise<void>
  downloadGcode: () => void
  addDimension: (a: Vector3, b: Vector3, label?: string | null) => Promise<void>
  removeDimension: (dimId: string) => Promise<void>

  /** Explicit save: geometry autosaves on every edit; this drops a named restore point. */
  saveCheckpoint: (label?: string) => Promise<boolean>
  /** Silent re-fetch after a collaborator's change — keeps selection/undo intact. */
  refreshDoc: () => Promise<void>
  createFromPrompt: (prompt: string) => Promise<void>
  /** Generate a part from an image; the prompt is an optional hint alongside it. */
  createFromImage: (file: File, prompt?: string) => Promise<void>
  newBlank: () => Promise<void>
  openDocument: (id: string) => Promise<void>
  rename: (name: string) => Promise<void>
  /** Replace the feature tree and re-evaluate on the server. */
  commitTree: (tree: FeatureTreeDict) => Promise<void>
  /** The single geometry-mutation path (same operations agents call). Returns the op result. */
  runOp: (op: string, args?: Record<string, unknown>) => Promise<Record<string, unknown> | undefined>
  aiEdit: (instruction: string) => Promise<void>
  /** The machine the job is being prepared for — drives nesting sheet, posting
   *  dialect, the build simulation and dispatch. Shared by Make/Validate/Machine. */
  machineId: string
  setMachineId: (id: string) => void
  /** Assign an engineering material (drives mass/sim/DFM/quote). */
  setMaterial: (materialId: string) => Promise<void>
  /** Replace the driving parameters/equations and re-evaluate. */
  setParameters: (parameters: DocumentParameter[]) => Promise<void>
  /** Forge Copilot: run an agentic multi-step plan toward a goal. Returns the transcript. */
  runCopilot: (goal: string, onProgress?: (state: CopilotResult) => void) => Promise<CopilotResult | undefined>
}

/** Normalize a freshly-received document so every feature node has a stable id. */
function withIds(doc: DesignDocument): DesignDocument {
  return { ...doc, feature_tree: ensureIds(doc.feature_tree) }
}

const BLANK_TOPOLOGY: TopologyState = {
  kind: 'edge', faces: [], edges: [], selected: [], loading: false, error: null,
}

/**
 * Everything measured *from* the geometry, cleared the moment the geometry moves.
 *
 * A stale reading is worse than a missing one — a safety factor from before the
 * wall was thinned still looks like an answer. The face and edge listings belong
 * here too: their ids are derived from what the entities currently measure, so
 * after an edit they name a body that no longer exists.
 */
const STALE = {
  sim: null, dfm: null, dfmProcess: null, cam: null, specReport: null,
  refs: null, topo: BLANK_TOPOLOGY,
} as const

/**
 * One step of history: the tree *and* the geometry that tree evaluated to.
 *
 * It used to be the tree alone, which is why undo was as slow as the edit it
 * reversed — the tree came back instantly and the body did not, so every undo
 * waited on a server round trip and a re-evaluation. Since the mesh became an
 * immutable hash-keyed URL the browser already holds, restoring the `derived`
 * cache alongside the tree puts the old body back with no network at all.
 */
export type HistoryEntry = { tree: FeatureTreeDict; derived: DerivedCache }

const HISTORY_DEPTH = 50

function pushHistory(past: HistoryEntry[], doc: DesignDocument): HistoryEntry[] {
  return [...past, { tree: doc.feature_tree, derived: doc.derived }].slice(-HISTORY_DEPTH)
}

/**
 * The undo/redo write that is still on its way to the server.
 *
 * Undo applies locally and syncs in the background, so the server can briefly
 * hold the tree the user just undid. A real edit issued in that window would
 * race it — and whichever request landed second would win — so every edit waits
 * for this first. It is normally already settled by the time anyone looks.
 */
let pendingSync: Promise<unknown> | null = null

export function historySyncSettled(): Promise<unknown> {
  return pendingSync ?? Promise.resolve()
}

export const useDocumentStore = create<DocumentState>((set, get) => ({
  doc: null,
  mode: 'model',
  selectedFeatureId: null,
  busy: false,
  error: null,
  optimizeJob: null,
  sim: null,
  dfm: null,
  dfmProcess: null,
  cam: null,
  past: [],
  future: [],
  sketchOpen: false,
  sketchEdit: null,
  sketchPick: false,
  sketchPreset: null,
  openSketch: (edit) => set({ sketchOpen: true, sketchEdit: edit ?? null, sketchPick: false, sketchPreset: null }),
  closeSketch: () => set({ sketchOpen: false, sketchEdit: null, sketchPick: false, sketchPreset: null }),
  startSketchPick: () => set({ sketchPick: true, sketchOpen: false, sketchEdit: null }),
  cancelSketchPick: () => set({ sketchPick: false }),
  pickSketchOnFace: (featureId, face) =>
    set({ sketchPick: false, sketchOpen: true, sketchEdit: null, sketchPreset: { onFace: { featureId, face } } }),
  pickSketchPlane: (plane) =>
    set({ sketchPick: false, sketchOpen: true, sketchEdit: null, sketchPreset: { plane } }),

  topo: BLANK_TOPOLOGY,
  refs: null,
  specReport: null,

  loadTopology: async (kind) => {
    const { doc, topo } = get()
    if (!doc) return
    // A selection of faces means nothing to fillet_edges, so switching kind
    // drops it rather than carrying ids across into an operation that will
    // reject them one at a time.
    set({ topo: { ...topo, kind, loading: true, error: null, selected: kind === topo.kind ? topo.selected : [] } })
    try {
      const env = kind === 'face' ? await api.listFaces(doc.id) : await api.listEdges(doc.id)
      if (!env.ok) {
        set({ topo: { ...get().topo, loading: false, error: env.error?.message ?? 'Unavailable' } })
        return
      }
      const faces = kind === 'face' ? (env.result as { faces: FaceRef[] }).faces : get().topo.faces
      const edges = kind === 'edge' ? (env.result as { edges: EdgeRef[] }).edges : get().topo.edges
      // Anything that vanished from the new listing can no longer be operated on.
      const live = new Set((kind === 'face' ? faces : edges).map((e) => e.id))
      set({
        topo: {
          ...get().topo, kind, faces, edges, loading: false, error: null,
          selected: get().topo.selected.filter((id) => live.has(id)),
        },
      })
    } catch (e) {
      set({ topo: { ...get().topo, loading: false, error: e instanceof Error ? e.message : 'Unavailable' } })
    }
  },

  toggleTopoSelection: (id) => {
    const { topo } = get()
    set({
      topo: {
        ...topo,
        selected: topo.selected.includes(id)
          ? topo.selected.filter((s) => s !== id)
          : [...topo.selected, id],
      },
    })
  },

  setTopoSelection: (ids) => set({ topo: { ...get().topo, selected: ids } }),

  applyTopology: async (op, sizeMm) => {
    await historySyncSettled()
    const { doc, topo, past } = get()
    if (!doc || topo.selected.length === 0) return
    set({ past: pushHistory(past, doc), future: [], busy: true, error: null })
    try {
      const call = op === 'fillet' ? api.filletEdges : op === 'chamfer' ? api.chamferEdges : api.shellFaces
      const env = await call(doc.id, topo.selected, sizeMm)
      if (!env.ok) {
        // These operations roll their edit back, so the document is unchanged
        // and the message names the reason — a radius the geometry cannot carry.
        set({ error: env.error?.message ?? `${op} failed` })
        return
      }
      const updated = withIds(await api.getDocument(doc.id))
      set({ doc: updated, error: updated.derived.error ?? null, ...STALE })
      await get().loadTopology(topo.kind)
      await get().checkReferences()
    } catch (e) {
      set({ error: e instanceof Error ? e.message : `${op} failed` })
    } finally {
      set({ busy: false })
    }
  },

  checkReferences: async () => {
    const { doc } = get()
    if (!doc) return
    try {
      const env = await api.checkReferences(doc.id)
      // A PRECONDITION here just means the body has no exact form; there is
      // nothing to report and nothing wrong.
      set({ refs: env.ok ? env.result : null })
    } catch {
      set({ refs: null })
    }
  },

  saveSpec: async (requirements, goal) => {
    const { doc } = get()
    if (!doc) return
    set({ busy: true, error: null })
    try {
      const env = await api.setSpec(doc.id, requirements, goal)
      if (!env.ok) { set({ error: env.error?.message ?? 'Could not save the spec' }); return }
      set({ doc: { ...doc, spec: { goal, requirements } }, specReport: null })
      await get().verifySpec()
    } catch (e) {
      set({ error: e instanceof Error ? e.message : 'Could not save the spec' })
    } finally {
      set({ busy: false })
    }
  },

  verifySpec: async () => {
    const { doc } = get()
    if (!doc) return
    try {
      const env = await api.verifyAgainstSpec(doc.id)
      set({ specReport: env.ok ? env.result : null, error: env.ok ? null : env.error?.message ?? null })
    } catch (e) {
      set({ error: e instanceof Error ? e.message : 'Could not verify the spec' })
    }
  },

  setMode: (mode) => set({ mode }),
  select: (selectedFeatureId) => set({ selectedFeatureId }),
  clearError: () => set({ error: null }),

  runOptimize: async (opts) => {
    const { doc } = get()
    if (!doc) return
    const setup: OptimizationSetup = {
      design_domain: 'from_body',
      keep_in: [],
      keep_out: [],
      load_cases: opts.load_cases ?? [],
      constraints: [],
      objective: opts.objective ?? { target: 'minimize_mass', weight: 1 },
      volume_fraction: opts.volume_fraction ?? 0.4,
      resolution: opts.resolution ?? 96,
      material_id: 'al-7075',
    }
    set({ busy: true, error: null, optimizeJob: null })
    try {
      let job = await api.optimizeDocument(doc.id, { setup, resolution: setup.resolution })
      set({ optimizeJob: job })
      while (job.status !== 'done' && job.status !== 'failed') {
        await new Promise((r) => setTimeout(r, 500))
        job = await api.getOptimizeJob(doc.id, job.id)
        set({ optimizeJob: job })
      }
      if (job.status === 'failed') {
        set({ error: job.message })
      } else {
        const updated = withIds(await api.getDocument(doc.id))
        set({
          doc: updated,
          selectedFeatureId: updated.feature_tree.root?.id ?? null,
          mode: 'model',
          sim: null,
          dfm: null,
          cam: null,
        })
      }
    } catch (e) {
      set({ error: e instanceof Error ? e.message : 'Optimization failed' })
    } finally {
      set({ busy: false })
    }
  },

  runOp: async (op, args) => {
    await historySyncSettled()
    const { doc, past } = get()
    if (!doc) return undefined
    set({ past: pushHistory(past, doc), future: [], busy: true, error: null })
    try {
      const res = await api.runOperation(doc.id, op, args)
      if (res.document) {
        const updated = withIds(res.document)
        set({ doc: updated, error: updated.derived.error ?? null, ...STALE })
      }
      return res.result
    } catch (e) {
      set({ error: e instanceof Error ? e.message : `Operation ${op} failed` })
      return undefined
    } finally {
      set({ busy: false })
    }
  },

  machineId: 'ender3',
  setMachineId: (id) => set({ machineId: id }),

  setMaterial: async (materialId) => {
    const { doc } = get()
    if (!doc) return
    set({ busy: true, error: null })
    try {
      const updated = withIds(await api.setMaterial(doc.id, materialId))
      set({ doc: updated, error: updated.derived.error ?? null })
    } catch (e) {
      set({ error: e instanceof Error ? e.message : 'Could not set material' })
    } finally {
      set({ busy: false })
    }
  },

  setParameters: async (parameters) => {
    await historySyncSettled()
    const { doc, past } = get()
    if (!doc) return
    set({ past: pushHistory(past, doc), future: [], busy: true, error: null })
    try {
      const updated = withIds(await api.setParameters(doc.id, parameters))
      set({ doc: updated, error: updated.derived.error ?? null, ...STALE })
    } catch (e) {
      set({ error: e instanceof Error ? e.message : 'Could not set parameters' })
    } finally {
      set({ busy: false })
    }
  },

  aiEdit: async (instruction) => {
    await historySyncSettled()
    const { doc, past } = get()
    if (!doc || !instruction.trim()) return
    set({ past: pushHistory(past, doc), future: [], busy: true, error: null })
    try {
      const updated = withIds(await api.aiEditDocument(doc.id, instruction))
      set({
        doc: updated,
        selectedFeatureId: updated.feature_tree.root?.id ?? null,
        sim: null,
        dfm: null,
        cam: null,
      })
    } catch (e) {
      set({ error: e instanceof Error ? e.message : 'AI edit failed' })
    } finally {
      set({ busy: false })
    }
  },

  runCopilot: async (goal, onProgress) => {
    await historySyncSettled()
    const { doc, past } = get()
    if (!doc || !goal.trim()) return undefined
    set({ past: pushHistory(past, doc), future: [], busy: true, error: null })
    try {
      const res = await api.runCopilot(doc.id, goal, 8, onProgress)
      if (res.document) {
        const updated = withIds(res.document)
        set({
          doc: updated,
          selectedFeatureId: updated.feature_tree.root?.id ?? null,
          error: updated.derived.error ?? null,
          ...STALE,
        })
      }
      return res
    } catch (e) {
      set({ error: e instanceof Error ? e.message : 'Copilot failed' })
      return undefined
    } finally {
      set({ busy: false })
    }
  },

  runSimulate: async (elements) => {
    const { doc } = get()
    if (!doc) return
    set({ busy: true, error: null })
    try {
      // Through the operation, not `POST /simulate`: that endpoint's response
      // model drops `solver` and `estimate_only`, so a closed-form guess would
      // reach the panel looking exactly like a real solve.
      const env = await api.analyse(doc.id, elements)
      if (!env.ok) { set({ error: env.error?.message ?? 'Simulation failed' }); return }
      set({ sim: env.result })
    } catch (e) {
      set({ error: e instanceof Error ? e.message : 'Simulation failed' })
    } finally {
      set({ busy: false })
    }
  },

  runDfm: async (buildAxis = 'auto', process: DfmProcess = 'additive') => {
    const { doc } = get()
    if (!doc) return
    set({ busy: true, error: null })
    try {
      const dfm = await api.dfmDocument(doc.id, buildAxis, 96, process)
      set({ dfm, dfmProcess: process })
    } catch (e) {
      set({ error: e instanceof Error ? e.message : 'DFM analysis failed' })
    } finally {
      set({ busy: false })
    }
  },

  runCam: async (tool) => {
    const { doc } = get()
    if (!doc) return
    set({ busy: true, error: null })
    try {
      const cam = await api.camDocument(doc.id, tool)
      set({ cam })
    } catch (e) {
      set({ error: e instanceof Error ? e.message : 'CAM generation failed' })
    } finally {
      set({ busy: false })
    }
  },

  exportStep: async () => {
    const { doc } = get()
    if (!doc) return
    try {
      const step = await api.exportDocumentStep(doc.id, 128)
      const blob = new Blob([step], { type: 'application/step' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `${doc.name.replace(/[^\w.-]+/g, '_') || 'part'}.step`
      a.click()
      URL.revokeObjectURL(url)
    } catch (e) {
      set({ error: e instanceof Error ? e.message : 'Export failed' })
    }
  },

  downloadGcode: () => {
    const { doc, cam } = get()
    if (!doc || !cam?.gcode_url) return
    const a = document.createElement('a')
    a.href = cam.gcode_url
    a.download = `${doc.name.replace(/[^\w.-]+/g, '_') || 'part'}.nc`
    a.click()
  },

  addDimension: async (a, b, label) => {
    const { doc } = get()
    if (!doc) return
    try {
      const dim = await api.addDimension(doc.id, { a, b, label, source: 'user' })
      set({ doc: { ...doc, dimensions: [...doc.dimensions, dim] } })
    } catch (e) {
      set({ error: e instanceof Error ? e.message : 'Failed to add dimension' })
    }
  },

  removeDimension: async (dimId) => {
    const { doc } = get()
    if (!doc) return
    // optimistic removal
    set({ doc: { ...doc, dimensions: doc.dimensions.filter((d) => d.id !== dimId) } })
    try {
      await api.removeDimension(doc.id, dimId)
    } catch {
      /* ignore; next fetch reconciles */
    }
  },

  saveCheckpoint: async (label) => {
    const { doc } = get()
    if (!doc) return false
    // The tree already persists on every mutation; a checkpoint is a named version
    // the user can restore. Skip while a mutation is still in flight.
    if (get().busy) return false
    try {
      const when = new Date().toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
      await api.createVersion(doc.id, label ?? `Saved ${when}`)
      return true
    } catch (e) {
      set({ error: e instanceof Error ? e.message : 'Save failed' })
      return false
    }
  },

  refreshDoc: async () => {
    const { doc, busy } = get()
    if (!doc || busy) return
    try {
      const updated = withIds(await api.getDocument(doc.id))
      set({ doc: updated, error: updated.derived.error ?? null })
    } catch {
      /* transient — next event or action reconciles */
    }
  },

  createFromPrompt: async (prompt) => {
    if (!prompt.trim()) return
    set({ busy: true, error: null })
    try {
      const doc = withIds(await api.createDocument({ prompt, resolution: DEFAULT_RESOLUTION }))
      set({ doc, selectedFeatureId: doc.feature_tree.root?.id ?? null, mode: 'model', ...STALE, past: [], future: [] })
      if (doc.derived.error) set({ error: doc.derived.error })
    } catch (e) {
      set({ error: e instanceof Error ? e.message : 'Failed to generate part' })
    } finally {
      set({ busy: false })
    }
  },

  createFromImage: async (file, prompt) => {
    set({ busy: true, error: null })
    try {
      const doc = withIds(await api.createDocumentFromImage(file, prompt))
      set({ doc, selectedFeatureId: doc.feature_tree.root?.id ?? null, mode: 'model', ...STALE, past: [], future: [] })
      if (doc.derived.error) set({ error: doc.derived.error })
    } catch (e) {
      set({ error: e instanceof Error ? e.message : 'Failed to build a part from that image' })
    } finally {
      set({ busy: false })
    }
  },

  newBlank: async () => {
    set({ busy: true, error: null })
    try {
      const doc = withIds(await api.createDocument({ resolution: DEFAULT_RESOLUTION }))
      set({ doc, selectedFeatureId: doc.feature_tree.root?.id ?? null, mode: 'model', ...STALE, past: [], future: [] })
    } catch (e) {
      set({ error: e instanceof Error ? e.message : 'Failed to create document' })
    } finally {
      set({ busy: false })
    }
  },

  openDocument: async (id) => {
    set({ busy: true, error: null })
    try {
      const doc = withIds(await api.getDocument(id))
      set({ doc, selectedFeatureId: doc.feature_tree.root?.id ?? null, ...STALE, past: [], future: [] })
    } catch (e) {
      set({ error: e instanceof Error ? e.message : 'Failed to open document' })
    } finally {
      set({ busy: false })
    }
  },

  rename: async (name) => {
    const { doc } = get()
    if (!doc) return
    set({ doc: { ...doc, name } })
    try {
      await api.updateDocument(doc.id, { name })
    } catch {
      /* name is cosmetic; ignore transient failure */
    }
  },

  commitTree: async (tree) => {
    await historySyncSettled()
    const { doc, past } = get()
    if (!doc) return
    // snapshot current tree for undo, then optimistic update
    set({ past: pushHistory(past, doc), future: [], busy: true, error: null, doc: { ...doc, feature_tree: tree } })
    try {
      const updated = withIds(
        await api.updateDocument(doc.id, { feature_tree: tree, resolution: DEFAULT_RESOLUTION }),
      )
      set({ doc: updated, error: updated.derived.error ?? null, ...STALE })
    } catch (e) {
      set({ error: e instanceof Error ? e.message : 'Failed to evaluate edit' })
    } finally {
      set({ busy: false })
    }
  },

  undo: async () => {
    const { doc, past, future } = get()
    if (!doc || past.length === 0) return
    step(set, get, doc, past[past.length - 1], {
      past: past.slice(0, -1),
      future: [...future, { tree: doc.feature_tree, derived: doc.derived }],
    }, 'Undo failed')
  },

  redo: async () => {
    const { doc, past, future } = get()
    if (!doc || future.length === 0) return
    step(set, get, doc, future[future.length - 1], {
      future: future.slice(0, -1),
      past: [...past, { tree: doc.feature_tree, derived: doc.derived }],
    }, 'Redo failed')
  },
}))

type Setter = (partial: Partial<DocumentState>) => void

/**
 * Move to a remembered state: locally now, on the server shortly.
 *
 * The local half is the whole point — the tree is in hand and the mesh behind
 * `derived.mesh_url` is immutable and already in the browser's cache, so there
 * is nothing to wait for. Two things it must still do:
 *
 * - **Everything measured from the geometry goes stale.** `derived` is the
 *   body's own cache and travels with the step; a simulation, a DFM report or a
 *   face listing is a *reading* of a body that is no longer on screen, and an
 *   undo that left one next to the restored geometry would be showing an answer
 *   to a question nobody asked.
 * - **The server has to catch up, and the catch-up must not clobber a newer
 *   edit.** The adopt-the-response step checks the tree is still the one it
 *   sent; `historySyncSettled` is what the edit path waits on so the two cannot
 *   land out of order.
 */
function step(set: Setter, get: () => DocumentState, doc: DesignDocument,
              target: HistoryEntry, stacks: Partial<DocumentState>, failure: string) {
  set({
    ...stacks,
    doc: { ...doc, feature_tree: target.tree, derived: target.derived },
    error: null,
    ...STALE,
  })
  pendingSync = api
    .updateDocument(doc.id, { feature_tree: target.tree, resolution: DEFAULT_RESOLUTION })
    .then((fresh) => {
      const current = get().doc
      if (current && current.feature_tree === target.tree) {
        set({ doc: withIds(fresh), error: fresh.derived.error ?? null })
      }
    })
    .catch((e) => set({ error: e instanceof Error ? e.message : failure }))
  const mine = pendingSync
  // Only the *latest* step clears the slot: a second undo lands while the first
  // is still in flight, and nulling unconditionally would let an edit through
  // while a write it must follow was still open.
  void mine.finally(() => { if (pendingSync === mine) pendingSync = null })
}

import { create } from 'zustand'
import type {
  DesignDocument,
  EditorMode,
  FeatureTreeDict,
  OptimizeJob,
  OptimizationSetup,
  LoadCase,
  Objective,
  SimulationResult,
  DfmReport,
  CamResult,
  Vector3,
  CopilotResult,
  DocumentParameter,
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

interface DocumentState {
  doc: DesignDocument | null
  mode: EditorMode
  selectedFeatureId: string | null
  busy: boolean
  error: string | null
  optimizeJob: OptimizeJob | null
  sim: SimulationResult | null
  dfm: DfmReport | null
  cam: CamResult | null
  past: FeatureTreeDict[]
  future: FeatureTreeDict[]
  undo: () => Promise<void>
  redo: () => Promise<void>
  sketchOpen: boolean
  sketchEdit: { featureId: string; sketch: SketchSpec; plane: string; depth: number; offset: number } | null
  /** True while the user is picking a sketch plane (a face in the viewport or a base plane). */
  sketchPick: boolean
  /** How a new sketch should be seeded once the overlay opens (from the pick step). */
  sketchPreset: { onFace?: { featureId: string; face: string }; plane?: string } | null
  openSketch: (edit?: { featureId: string; sketch: SketchSpec; plane: string; depth: number; offset: number }) => void
  closeSketch: () => void
  /** Enter face/plane-picking mode (viewport + feature tree) before the sketch opens. */
  startSketchPick: () => void
  cancelSketchPick: () => void
  /** Open a new sketch attached to a feature's face (from a viewport or tree click). */
  pickSketchOnFace: (featureId: string, face: string) => void
  /** Open a new sketch on a base datum plane (xy/xz/yz). */
  pickSketchPlane: (plane: string) => void

  setMode: (mode: EditorMode) => void
  select: (id: string | null) => void
  clearError: () => void
  runOptimize: (opts: {
    volume_fraction?: number
    objective?: Objective
    resolution?: number
    load_cases?: LoadCase[]
  }) => Promise<void>
  runSimulate: () => Promise<void>
  runDfm: (buildAxis?: string) => Promise<void>
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

export const useDocumentStore = create<DocumentState>((set, get) => ({
  doc: null,
  mode: 'model',
  selectedFeatureId: null,
  busy: false,
  error: null,
  optimizeJob: null,
  sim: null,
  dfm: null,
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
    const { doc, past } = get()
    if (!doc) return undefined
    set({ past: [...past, doc.feature_tree].slice(-50), future: [], busy: true, error: null })
    try {
      const res = await api.runOperation(doc.id, op, args)
      if (res.document) {
        const updated = withIds(res.document)
        set({ doc: updated, error: updated.derived.error ?? null, sim: null, dfm: null, cam: null })
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
    const { doc, past } = get()
    if (!doc) return
    set({ past: [...past, doc.feature_tree].slice(-50), future: [], busy: true, error: null })
    try {
      const updated = withIds(await api.setParameters(doc.id, parameters))
      set({ doc: updated, error: updated.derived.error ?? null, sim: null, dfm: null, cam: null })
    } catch (e) {
      set({ error: e instanceof Error ? e.message : 'Could not set parameters' })
    } finally {
      set({ busy: false })
    }
  },

  aiEdit: async (instruction) => {
    const { doc, past } = get()
    if (!doc || !instruction.trim()) return
    set({ past: [...past, doc.feature_tree].slice(-50), future: [], busy: true, error: null })
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
    const { doc, past } = get()
    if (!doc || !goal.trim()) return undefined
    set({ past: [...past, doc.feature_tree].slice(-50), future: [], busy: true, error: null })
    try {
      const res = await api.runCopilot(doc.id, goal, 8, onProgress)
      if (res.document) {
        const updated = withIds(res.document)
        set({
          doc: updated,
          selectedFeatureId: updated.feature_tree.root?.id ?? null,
          error: updated.derived.error ?? null,
          sim: null, dfm: null, cam: null,
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

  runSimulate: async () => {
    const { doc } = get()
    if (!doc) return
    set({ busy: true, error: null })
    try {
      const sim = await api.simulateDocument(doc.id, 96)
      set({ sim })
    } catch (e) {
      set({ error: e instanceof Error ? e.message : 'Simulation failed' })
    } finally {
      set({ busy: false })
    }
  },

  runDfm: async (buildAxis = 'auto') => {
    const { doc } = get()
    if (!doc) return
    set({ busy: true, error: null })
    try {
      const dfm = await api.dfmDocument(doc.id, buildAxis, 96)
      set({ dfm })
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
      set({ doc, selectedFeatureId: doc.feature_tree.root?.id ?? null, mode: 'model', sim: null, dfm: null, cam: null, past: [], future: [] })
      if (doc.derived.error) set({ error: doc.derived.error })
    } catch (e) {
      set({ error: e instanceof Error ? e.message : 'Failed to generate part' })
    } finally {
      set({ busy: false })
    }
  },

  newBlank: async () => {
    set({ busy: true, error: null })
    try {
      const doc = withIds(await api.createDocument({ resolution: DEFAULT_RESOLUTION }))
      set({ doc, selectedFeatureId: doc.feature_tree.root?.id ?? null, mode: 'model', sim: null, dfm: null, cam: null, past: [], future: [] })
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
      set({ doc, selectedFeatureId: doc.feature_tree.root?.id ?? null, sim: null, dfm: null, cam: null, past: [], future: [] })
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
    const { doc, past } = get()
    if (!doc) return
    // snapshot current tree for undo, then optimistic update
    set({ past: [...past, doc.feature_tree].slice(-50), future: [], busy: true, error: null, doc: { ...doc, feature_tree: tree } })
    try {
      const updated = withIds(
        await api.updateDocument(doc.id, { feature_tree: tree, resolution: DEFAULT_RESOLUTION }),
      )
      set({ doc: updated, error: updated.derived.error ?? null, sim: null, dfm: null, cam: null })
    } catch (e) {
      set({ error: e instanceof Error ? e.message : 'Failed to evaluate edit' })
    } finally {
      set({ busy: false })
    }
  },

  undo: async () => {
    const { doc, past, future } = get()
    if (!doc || past.length === 0) return
    const prev = past[past.length - 1]
    set({ past: past.slice(0, -1), future: [...future, doc.feature_tree], busy: true, error: null, doc: { ...doc, feature_tree: prev } })
    try {
      const updated = withIds(await api.updateDocument(doc.id, { feature_tree: prev, resolution: DEFAULT_RESOLUTION }))
      set({ doc: updated, error: updated.derived.error ?? null, sim: null, dfm: null, cam: null })
    } catch (e) {
      set({ error: e instanceof Error ? e.message : 'Undo failed' })
    } finally {
      set({ busy: false })
    }
  },

  redo: async () => {
    const { doc, past, future } = get()
    if (!doc || future.length === 0) return
    const next = future[future.length - 1]
    set({ future: future.slice(0, -1), past: [...past, doc.feature_tree], busy: true, error: null, doc: { ...doc, feature_tree: next } })
    try {
      const updated = withIds(await api.updateDocument(doc.id, { feature_tree: next, resolution: DEFAULT_RESOLUTION }))
      set({ doc: updated, error: updated.derived.error ?? null, sim: null, dfm: null, cam: null })
    } catch (e) {
      set({ error: e instanceof Error ? e.message : 'Redo failed' })
    } finally {
      set({ busy: false })
    }
  },
}))

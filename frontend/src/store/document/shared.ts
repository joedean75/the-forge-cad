/**
 * What the three document slices have in common: the state shape, the helpers,
 * and the rule that decides whether a response that has just arrived is still
 * about the thing on screen.
 *
 * The store was one 785-line file holding the document, the selection, the
 * sketch overlay, every analysis panel's result and every job's polling loop.
 * Splitting it is not tidying: the three groups have different lifetimes. The
 * document survives everything; a selection survives an edit but not a document
 * switch; a reading of the geometry survives neither. Written as one object
 * those rules were enforced by remembering to spread `...STALE` at twenty-odd
 * call sites, and forgetting once leaves a safety factor from before the wall
 * was thinned sitting next to the new body.
 */

import type {
  AnalysisReport, CamResult, DerivedCache, DesignDocument, DfmProcess, DfmReport,
  EdgeRef, EditorMode, FaceRef, FeatureTreeDict, OptimizeJob, ReferenceReport,
  SpecReport, SpecRequirement, Vector3, CopilotResult, DocumentParameter,
  LoadCase, Objective, OptimizationSetup,
} from '../../../../shared/types'
import { ApiError } from '../../api/client'
import { ensureIds } from '../../util/featureTree'

export const DEFAULT_RESOLUTION = 96
export const HISTORY_DEPTH = 50

export interface SketchSpec {
  points: Record<string, unknown>[]
  lines?: Record<string, unknown>[]
  arcs?: Record<string, unknown>[]
  circles?: Record<string, unknown>[]
  constraints?: Record<string, unknown>[]
}

export type SketchEdit = {
  featureId: string
  sketch: SketchSpec
  plane: string
  depth: number
  offset: number
  /** A standalone SketchFeature has no depth of its own — the consumer owns it. */
  standalone?: boolean
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

/**
 * The last operation failure, with what to do about it.
 *
 * Separate from `error` — which is the status bar's "is something wrong" light
 * and is also set by `derived.error`, a *warning* about a tree that does not
 * currently evaluate. Conflating the two would paint a supported workflow
 * (binding a feature to a parameter before defining it) as a failure, which is
 * the distinction the operation envelope was built to make.
 */
export interface OperationFailure {
  op: string
  message: string
  /** MISSING_ARG | BAD_ARG | PRECONDITION | … */
  code?: string
  /** What to do about it. Generated per code in `services/op_errors`. */
  remedy?: string
  arg?: string | null
}

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

export const BLANK_TOPOLOGY: TopologyState = {
  kind: 'edge', faces: [], edges: [], selected: [], loading: false, error: null,
}

/**
 * Everything measured *from* the geometry, cleared the moment the geometry moves.
 *
 * A stale reading is worse than a missing one — a safety factor from before the
 * wall was thinned still looks like an answer. The face and edge listings belong
 * here too: their ids are derived from what the entities currently measure, so
 * after an edit they name a body that no longer exists.
 *
 * The server keeps the same judgement now (`services/derived_cache`), against a
 * fingerprint of the inputs rather than against "an edit happened here" — which
 * is what makes it survive a reload and hold for a second viewer. This stays
 * because it is instant and local: the panel should empty as the edit lands, not
 * one round trip later.
 */
export const STALE = {
  sim: null, dfm: null, dfmProcess: null, cam: null, specReport: null,
  refs: null, topo: BLANK_TOPOLOGY,
} as const

// ---------------------------------------------------------------------------
// Slices
// ---------------------------------------------------------------------------

/** The open part, its history, and every path that writes to it. */
export interface DocumentSlice {
  doc: DesignDocument | null
  busy: boolean
  error: string | null
  /** The last operation failure, with its code and remedy. Null once dismissed. */
  failure: OperationFailure | null
  past: HistoryEntry[]
  future: HistoryEntry[]
  /**
   * Bumped whenever the document is replaced — a switch, an edit, an undo.
   *
   * A response in flight names the generation it was issued against, so one
   * that arrives after the user has moved on is discarded instead of painting
   * an answer about a body nobody is looking at. See `issue`/`current`.
   */
  generation: number
  clearError: () => void
  dismissFailure: () => void
  undo: () => Promise<void>
  redo: () => Promise<void>
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
  /** Assign an engineering material (drives mass/sim/DFM/quote). */
  setMaterial: (materialId: string) => Promise<void>
  /** Replace the driving parameters/equations and re-evaluate. */
  setParameters: (parameters: DocumentParameter[]) => Promise<void>
  /** Explicit save: geometry autosaves on every edit; this drops a named restore point. */
  saveCheckpoint: (label?: string) => Promise<boolean>
  /** Silent re-fetch after a collaborator's change — keeps selection/undo intact. */
  refreshDoc: () => Promise<void>
  addDimension: (a: Vector3, b: Vector3, label?: string | null) => Promise<void>
  removeDimension: (dimId: string) => Promise<void>
  /** Forge Copilot: run an agentic multi-step plan toward a goal. Returns the transcript. */
  runCopilot: (goal: string, onProgress?: (state: CopilotResult) => void) => Promise<CopilotResult | undefined>
}

/** What is picked, what is being sketched, and what the spec says about it. */
export interface SelectionSlice {
  mode: EditorMode
  selectedFeatureId: string | null
  sketchOpen: boolean
  sketchEdit: SketchEdit | null
  /** True while the user is picking a sketch plane (a face in the viewport or a base plane). */
  sketchPick: boolean
  /** How a new sketch should be seeded once the overlay opens (from the pick step). */
  sketchPreset: { onFace?: { featureId: string; face: string }; plane?: string } | null
  /** Faces/edges the body currently offers, and which of them are picked. */
  topo: TopologyState
  /** Which stored selections still resolve — see `check_references`. */
  refs: ReferenceReport | null
  /** The design spec and the last score against it. */
  specReport: SpecReport | null
  setMode: (mode: EditorMode) => void
  select: (id: string | null) => void
  openSketch: (edit?: SketchEdit) => void
  closeSketch: () => void
  /** Enter face/plane-picking mode (viewport + feature tree) before the sketch opens. */
  startSketchPick: () => void
  cancelSketchPick: () => void
  /** Open a new sketch attached to a feature's face (from a viewport or tree click). */
  pickSketchOnFace: (featureId: string, face: string) => void
  /** Open a new sketch on a base datum plane (xy/xz/yz). */
  pickSketchPlane: (plane: string) => void
  /** List the body's faces or edges (clears the selection when the kind changes). */
  loadTopology: (kind: 'face' | 'edge') => Promise<void>
  toggleTopoSelection: (id: string) => void
  /** Select every listed entity, or none. */
  setTopoSelection: (ids: string[]) => void
  /** Apply a treatment to the current selection and re-list the new body. */
  applyTopology: (op: 'fillet' | 'chamfer' | 'shell', sizeMm: number) => Promise<void>
  checkReferences: () => Promise<void>
  saveSpec: (requirements: SpecRequirement[], goal?: string) => Promise<void>
  verifySpec: () => Promise<void>
}

/** Readings of the geometry, and the jobs that produce them. */
export interface ComputeSlice {
  optimizeJob: OptimizeJob | null
  sim: AnalysisReport | null
  dfm: DfmReport | null
  /** Which process `dfm` was asked for. The report's own `process` is a display
   *  label ('LPBF', 'MILLING'), so it cannot answer "is this report about the
   *  process now selected?" — and a stale report read as a current one is the
   *  whole problem. */
  dfmProcess: DfmProcess | null
  cam: CamResult | null
  /** The machine the job is being prepared for — drives nesting sheet, posting
   *  dialect, the build simulation and dispatch. Shared by Make/Validate/Machine. */
  machineId: string
  setMachineId: (id: string) => void
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
}

export type DocumentState = DocumentSlice & SelectionSlice & ComputeSlice

export type Get = () => DocumentState
export type Set = (partial: Partial<DocumentState>) => void

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

export function asFailure(e: unknown, op: string): OperationFailure {
  if (e instanceof ApiError) {
    return { op, message: e.message, code: e.code, remedy: e.remedy, arg: e.arg }
  }
  return { op, message: e instanceof Error ? e.message : `Operation ${op} failed` }
}

/** Normalize a freshly-received document so every feature node has a stable id. */
export function withIds(doc: DesignDocument): DesignDocument {
  return { ...doc, feature_tree: ensureIds(doc.feature_tree) }
}

export function pushHistory(past: HistoryEntry[], doc: DesignDocument): HistoryEntry[] {
  return [...past, { tree: doc.feature_tree, derived: doc.derived }].slice(-HISTORY_DEPTH)
}

export function message(e: unknown, fallback: string): string {
  return e instanceof Error ? e.message : fallback
}

// ---------------------------------------------------------------------------
// Request identity
// ---------------------------------------------------------------------------

/** Which document, and which version of it, a request was issued against. */
export interface Ticket {
  docId: string
  generation: number
}

export function issue(get: Get): Ticket | null {
  const { doc, generation } = get()
  return doc ? { docId: doc.id, generation } : null
}

/**
 * Whether a response is still about the thing the user is looking at.
 *
 * Two ways it can stop being: the user opened another part, or the body moved
 * under it. Both used to end the same way — the response was applied anyway —
 * and the visible result was an analysis panel describing a document that was
 * no longer open, or a face listing for a body two edits ago. Neither looks
 * like a bug from the outside, which is exactly why they survived.
 */
export function current(get: Get, ticket: Ticket | null): boolean {
  if (!ticket) return false
  const { doc, generation } = get()
  return !!doc && doc.id === ticket.docId && generation === ticket.generation
}

/**
 * Whether a response about the *document itself* may still be applied.
 *
 * Looser than `current` on purpose: a write's own response necessarily arrives
 * after the generation it was issued against, since the write is what moved it.
 * What must not happen is applying it to a different document.
 */
export function sameDocument(get: Get, ticket: Ticket | null): boolean {
  if (!ticket) return false
  const { doc } = get()
  return !!doc && doc.id === ticket.docId
}

/** Adopt a new document, retiring every reading of the old one. */
export function adopt(get: Get, fresh: DesignDocument): Partial<DocumentState> {
  return {
    doc: withIds(fresh),
    generation: get().generation + 1,
    error: fresh.derived.error ?? null,
  }
}

export type { AnalysisReport, CamResult, DesignDocument, DfmProcess, DfmReport, OptimizeJob, OptimizationSetup }

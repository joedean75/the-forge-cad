import type {
  AnalysisReport,
  EdgeRef,
  JobRecord,
  ModeStatus,
  FaceRef,
  OptimizeJob,
  PartSpec,
  ReferenceReport,
  SketchOpResult,
  SketchSolution,
  SketchSpec,
  SimulationResult,
  SpecReport,
  SpecRequirement,
  DfmProcess,
  DfmReport,
  CamResult,
  LlmConfig,
  DesignDocument,
  DocumentSummary,
  OptimizationSetup,
  FeatureTreeDict,
  MachinePrinter,
  MachineStatus,
  Dimension,
  Vector3,
  VersionSummary,
  DocumentVersion,
  Collaborator,
  AssemblyDoc,
  AssemblyMate,
  BomReport,
  InterferenceReport,
  AssemblyPromptJob,
  AssemblySummary,
  ComponentTransform,
  CopilotResult,
  QuoteBundle,
  ManufacturingOrder,
  CatalogMaterial,
  MaterialCategory,
  DocumentParameter,
  MassProperties,
  DrawingResult,
  DrawingSpec,
  ValidationReport,
  DocumentRevisionMeta,
  SliceSettings,
  SliceResponse,
  MachineSpec,
  NestResult,
  OperationSpec,
  ExportArtifact,
  ReleasePackageJob,
  StandardPart,
  StandardPartPage,
  PartTemplate,
} from '../../../shared/types'
import { pollJob } from '../store/document/polling'

const API_BASE = '/api'

/** What `export` can write. Mirrors `services/artifacts.FORMATS`. */
export type ExportFormat = 'step' | 'stl' | 'pdf' | 'dxf' | 'svg'

// Session token (JWT) attached to every request when present. Persisted so a
// reload keeps the user signed in.
let authToken: string | null = typeof localStorage !== 'undefined' ? localStorage.getItem('forge_token') : null

export function setAuthToken(token: string | null) {
  authToken = token
  if (typeof localStorage !== 'undefined') {
    if (token) localStorage.setItem('forge_token', token)
    else localStorage.removeItem('forge_token')
  }
}

export function getAuthToken() {
  return authToken
}

export function authHeaders(base: Record<string, string> = {}): Record<string, string> {
  return authToken ? { ...base, Authorization: `Bearer ${authToken}` } : base
}

/**
 * A failure with the fields the backend actually sent.
 *
 * `services/op_errors` gives every failure a `code` and a `remedy` — what to do
 * about it, generated per code so no handler has to remember. All of that used
 * to be thrown away *and mangled*: `detail` is an object for exactly the raise
 * sites with the most to say, and interpolating one into a template literal
 * produced `API error: 422 [object Object]`, which is what a user saw when they
 * subtracted from an empty document.
 */
export class ApiError extends Error {
  status: number
  code?: string
  /** What to do about it. The useful half, and the half that was being dropped. */
  remedy?: string
  /** Which argument, when the failure names one. */
  arg?: string | null

  constructor(status: number, detail: unknown, fallback: string) {
    const structured = detail && typeof detail === 'object' ? detail as Record<string, unknown> : null
    const message = String(structured?.message ?? (typeof detail === 'string' ? detail : fallback))
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = structured?.code as string | undefined
    this.remedy = structured?.remedy as string | undefined
    this.arg = (structured?.arg as string | null | undefined) ?? null
  }
}

async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${url}`, {
    ...init,
    headers: authHeaders({ 'Content-Type': 'application/json', ...(init?.headers as Record<string, string>) }),
  })
  if (!res.ok) {
    let detail: unknown = null
    try {
      detail = (await res.json())?.detail
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, detail, res.statusText || `request failed (${res.status})`)
  }
  return res.json() as Promise<T>
}

/**
 * Fetch a file behind the auth header and save it.
 *
 * Every export in this client had its own copy of blob → anchor → click →
 * revoke; the interesting part of each is only the URL and the filename. The
 * revoke matters and is the bit that gets forgotten: an object URL holds the
 * whole blob alive for the life of the document, and a STEP body is not small.
 */
async function downloadAuthed(url: string, filename: string, init?: RequestInit): Promise<void> {
  const res = await fetch(`${API_BASE}${url}`, { ...init, headers: authHeaders(init?.headers as Record<string, string>) })
  if (!res.ok) throw new Error(`Download failed (${res.status})`)
  const blob = await res.blob()
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = filename.slice(0, 80).replace(/[/\\?%*:|"<>]/g, '_')
  a.click()
  URL.revokeObjectURL(a.href)
}

export interface OAuthProvider {
  id: string
  name: string
}

export interface AccountView {
  id: string
  email: string
  plan: { id: string; name: string; features: string[] }
  usage: { ops_used: number; monthly_op_quota: number; period_start: number }
  api_keys: { id: string; name: string; prefix: string; created_at: string }[]
  created_at: string
}

export interface PlanSummary {
  id: string
  name: string
  price_usd_month: number
  max_documents: number
  resolution_cap: number
  monthly_op_quota: number
  seats: number
  features: string[]
}

/** What every operation returns: did it work, why not, and what changed. */
export interface OperationEnvelope<T = Record<string, unknown>> {
  ok: boolean
  op: string
  result: T
  /** Typed failure: MISSING_ARG | BAD_ARG | NO_SUCH_FEATURE | PRECONDITION | … */
  error?: { code: string; message: string; arg?: string } | null
  /** What the operation changed, including the ids added and removed. */
  delta?: Record<string, unknown> | null
  /** Compact document observation: features with their current params, size, mass. */
  state?: Record<string, unknown> | null
  /** Non-fatal conditions — EVAL_FAILED, and selections that stopped resolving. */
  warnings?: Array<{ code: string; message: string }>
  document?: DesignDocument
}

export const api = {
  health: () => fetchJson<{ status: string }>('/health'),
  /** Every operation the backend can run, with typed arguments — the same
   *  catalog agents read, so the console never needs a hand-kept list. */
  listOperations: () => fetchJson<{ operations: OperationSpec[] }>('/operations'),
  getLlmConfig: () => fetchJson<LlmConfig>('/llm/config'),
  setLlmModel: (model: string) =>
    fetchJson<LlmConfig>('/llm/model', {
      method: 'POST',
      body: JSON.stringify({ model }),
    }),

  // ── Design documents (unified part model) ──
  createDocument: (body: { name?: string; prompt?: string; units?: 'mm' | 'm'; resolution?: number }) =>
    fetchJson<DesignDocument>('/documents', { method: 'POST', body: JSON.stringify(body) }),
  listDocuments: () => fetchJson<DocumentSummary[]>('/documents'),
  /** Universal import: STL/OBJ/PLY/GLB/OFF or faceted STEP → editable document. */
  importDocument: async (file: File, opts?: { units?: string; resolution?: number }) => {
    const form = new FormData()
    form.append('file', file)
    if (opts?.units) form.append('units', opts.units)
    if (opts?.resolution) form.append('resolution', String(opts.resolution))
    const res = await fetch('/api/documents/import', { method: 'POST', body: form, headers: authHeaders() })
    if (!res.ok) {
      let detail = res.statusText
      try { detail = (await res.json())?.detail ?? detail } catch { /* non-JSON */ }
      throw new Error(detail)
    }
    return res.json() as Promise<DesignDocument>
  },
  /** Generate a part from an image (vision LLM when configured, else traced). */
  createDocumentFromImage: async (
    file: File,
    prompt?: string,
    opts?: { widthMm?: number; depthMm?: number },
  ) => {
    const form = new FormData()
    form.append('file', file)
    if (prompt?.trim()) form.append('prompt', prompt.trim())
    if (opts?.widthMm) form.append('width_mm', String(opts.widthMm))
    if (opts?.depthMm) form.append('depth_mm', String(opts.depthMm))
    const res = await fetch('/api/documents/from-image', { method: 'POST', body: form, headers: authHeaders() })
    if (!res.ok) {
      let detail = res.statusText
      try { detail = (await res.json())?.detail ?? detail } catch { /* non-JSON */ }
      throw new Error(detail)
    }
    return res.json() as Promise<DesignDocument>
  },
  getDocument: (id: string) => fetchJson<DesignDocument>(`/documents/${id}`),
  updateDocument: (
    id: string,
    body: { name?: string; feature_tree?: FeatureTreeDict; optimization?: OptimizationSetup; resolution?: number },
  ) => fetchJson<DesignDocument>(`/documents/${id}`, { method: 'PUT', body: JSON.stringify(body) }),
  evaluateDocument: (id: string, resolution = 128) =>
    fetchJson<DesignDocument>(`/documents/${id}/evaluate?resolution=${resolution}`, { method: 'POST' }),
  aiEditDocument: (id: string, instruction: string, resolution = 96) =>
    fetchJson<DesignDocument>(`/documents/${id}/ai-edit`, {
      method: 'POST',
      body: JSON.stringify({ instruction, resolution }),
    }),
  /** Instant manufacturing quotes across viable processes ("Get it made"). */
  quoteDocument: (id: string, quantity = 1) =>
    fetchJson<QuoteBundle>(`/documents/${id}/quotes`, {
      method: 'POST',
      body: JSON.stringify({ quantity }),
    }),
  placeOrder: (id: string, process: string, quantity = 1, notes = '') =>
    fetchJson<ManufacturingOrder>(`/documents/${id}/orders`, {
      method: 'POST',
      body: JSON.stringify({ process, quantity, notes }),
    }),
  listOrders: (id: string) =>
    fetchJson<{ orders: ManufacturingOrder[] }>(`/documents/${id}/orders`),

  /**
   * Write the part out as a file. Goes through the catalog, so the UI makes the
   * same call an agent does — and the result is a URL keyed by the file's
   * content hash, never the bytes.
   */
  exportDocument: (id: string, format: ExportFormat, spec?: Record<string, unknown>) =>
    api.runOperation<ExportArtifact>(id, 'export', spec ? { format, spec } : { format }),
  /** Export and save, in one call — what a toolbar button wants. */
  downloadExport: async (id: string, format: ExportFormat, spec?: Record<string, unknown>) => {
    const { result } = await api.exportDocument(id, format, spec)
    await downloadAuthed(result.url.replace(API_BASE, ''), result.filename)
    return result
  },

  /**
   * Build the handover package for a released revision. A job, not a request:
   * a package renders the drawing, exports the solid and meshes the body for
   * the spec report, and doing that inline times out at the worst moment.
   */
  buildReleasePackage: async (
    id: string,
    revision?: string,
    onProgress?: (job: ReleasePackageJob) => void,
    /** `standard`, or `printed` to add the printability report and a
     *  slicer-oriented drawing. A preset only ever adds files. */
    preset?: string,
  ): Promise<ReleasePackageJob> => {
    const started = await fetchJson<ReleasePackageJob>(`/documents/${id}/package`, {
      method: 'POST',
      body: JSON.stringify({ revision: revision ?? null, preset: preset ?? 'standard' }),
    })
    onProgress?.(started)
    if (started.status !== 'running') return started
    // One polling loop, with backoff and a deadline (`store/document/polling`).
    // The old `while (status === 'running') sleep(700)` had no exit but the
    // terminal status, so a job that vanished left the tab polling for as long
    // as it stayed open — which, now that a job is a durable row rather than an
    // entry in a dict that forgets, is a state worth distinguishing from
    // "still working".
    return pollJob(
      () => fetchJson<ReleasePackageJob>(`/documents/${id}/package/${started.id}`),
      (job) => job.status !== 'running',
      onProgress,
    )
  },

  /**
   * Every job against a document, newest first — how a reload reconnects.
   *
   * The browser that submitted a four-minute solve loses its job id to a
   * refresh; the document does not. Before jobs were rows there was nothing to
   * ask, because the id lived in a process-local dict that a restart emptied.
   */
  listJobs: (id: string) => fetchJson<{ jobs: JobRecord[] }>(`/documents/${id}/jobs`),

  /**
   * Per-mode readiness and freshness, in one call.
   *
   * Through the operation catalog, like every other typed wrapper here, so the
   * UI asks exactly what an agent asks. `settings` carries the per-mode call
   * arguments that affect freshness — a DFM report for additive is not current
   * for milling, and the server cannot know which one the panel has selected.
   */
  workflowStatus: (id: string, settings?: Record<string, unknown>) =>
    api.runOperation<{ modes: ModeStatus[]; package: ModeStatus }>(
      id, 'workflow_status', { settings: settings ?? {} }),
  getJob: (id: string, jobId: string) => fetchJson<JobRecord>(`/documents/${id}/jobs/${jobId}`),
  cancelJob: (id: string, jobId: string) =>
    fetchJson<JobRecord>(`/documents/${id}/jobs/${jobId}/cancel`, { method: 'POST' }),
  downloadArtifact: (url: string, filename: string) =>
    downloadAuthed(url.replace(API_BASE, ''), filename),

  /**
   * Browse the standard purchased parts.
   *
   * A resource, not an operation. `list_standard_parts` exists in the catalog
   * too — an agent always holds a document or assembly id — but a person
   * opening a *new* assembly holds neither, and that is exactly when they reach
   * for a screw. Both doors read the same catalog.
   */
  listStandardParts: (opts?: { category?: string; search?: string; source?: string; limit?: number }) => {
    const query = new URLSearchParams()
    if (opts?.category) query.set('category', opts.category)
    if (opts?.search) query.set('search', opts.search)
    if (opts?.source) query.set('source', opts.source)
    query.set('limit', String(opts?.limit ?? 60))
    return fetchJson<StandardPartPage>(`/documents/meta/standard-parts?${query}`)
  },
  /**
   * File a supplier's CAD file as a purchased part of this account's.
   *
   * Multipart, like every other import, because an operation's arguments are
   * JSON and a STEP body is not. *Placing* the result is an operation, so
   * nothing a person imports is out of an agent's reach.
   *
   * The file comes from the user. No portal is called from here — 3Dfindit
   * disallows it and TraceParts' terms forbid copying its data into another
   * catalog, so the links in `sources` are searches to go and fetch one by
   * hand under your own account.
   */
  importSupplierPart: async (file: File, meta: {
    designation?: string; source?: string; manufacturer?: string
    part_number?: string; category?: string; source_url?: string
    notes?: string; units?: string
  }) => {
    const form = new FormData()
    form.append('file', file)
    for (const [key, value] of Object.entries(meta)) {
      if (value) form.append(key, value)
    }
    const res = await fetch('/api/documents/meta/supplier-parts',
                            { method: 'POST', body: form, headers: authHeaders() })
    if (!res.ok) {
      let detail: unknown = null
      try { detail = (await res.json())?.detail } catch { /* non-JSON */ }
      throw new ApiError(res.status, detail, 'Could not import that file')
    }
    return res.json() as Promise<StandardPart>
  },
  /** Take an imported part out of the catalog. Its document is left alone. */
  forgetSupplierPart: (key: string) =>
    fetchJson<{ ok: boolean; key: string }>(
      `/documents/meta/supplier-parts/${encodeURIComponent(key)}`, { method: 'DELETE' }),
  /** The starter parts, and building one. Both are ordinary operations — a
   *  template is applied *into* a document, so there is always an id to
   *  address, which is why neither needed a REST route of its own. */
  listTemplates: (id: string) =>
    api.runOperation<{ templates: PartTemplate[] }>(id, 'list_templates'),
  applyTemplate: (id: string, template: string) =>
    api.runOperation<{ name: string; feature_ids: string[]; parameters: string[] }>(
      id, 'apply_template', { template }),

  /**
   * Forge Copilot: an agentic loop that plans + executes a sequence of operations.
   * Runs as a background job on the server (a run can span many LLM turns);
   * this polls until it resolves, reporting live steps via `onProgress`.
   */
  runCopilot: async (
    id: string,
    goal: string,
    maxSteps = 8,
    onProgress?: (state: CopilotResult) => void,
  ): Promise<CopilotResult> => {
    let state = await fetchJson<CopilotResult>(`/documents/${id}/copilot`, {
      method: 'POST',
      body: JSON.stringify({ goal, max_steps: maxSteps }),
    })
    const deadline = Date.now() + 20 * 60_000
    while (state.status === 'running') {
      if (Date.now() > deadline) throw new Error('Copilot timed out')
      await new Promise((r) => setTimeout(r, 1200))
      state = await fetchJson<CopilotResult>(`/documents/${id}/copilot/${state.job_id}`)
      onProgress?.(state)
    }
    return state
  },
  /** Single-use ticket for the document WebSocket (keeps the JWT out of the URL). */
  wsTicket: (id: string) =>
    fetchJson<{ ticket: string; expires_in: number }>(`/documents/${id}/ws-ticket`, { method: 'POST' }),
  /** The single mutation path shared by the UI and AI agents. */
  runOperation: <T = Record<string, unknown>>(id: string, op: string, args?: Record<string, unknown>) =>
    fetchJson<OperationEnvelope<T>>(`/documents/${id}/op`, {
      method: 'POST',
      body: JSON.stringify({ op, args: args ?? {} }),
    }),

  // ── Addressable topology (the exact kernel) ──
  // These go through the operation catalog rather than bespoke endpoints,
  // because that catalog *is* the API: the same call an agent makes.

  /** Selectable faces, largest first. PRECONDITION when the body has no exact form. */
  /** Pass `feature` for one feature's own faces — the id space a sketch plane uses. */
  listFaces: (id: string, feature?: string) =>
    api.runOperation<{ faces: FaceRef[]; count: number; feature?: string }>(
      id, 'list_faces', feature ? { feature } : undefined),
  listEdges: (id: string, filter?: { kind?: 'all' | 'line' | 'circle'; min_length_mm?: number }) =>
    api.runOperation<{ edges: EdgeRef[]; count: number }>(id, 'list_edges', filter),
  /** `edges` accepts the string "all" in place of an id list. */
  filletEdges: (id: string, edges: string[] | 'all', radiusMm: number) =>
    api.runOperation<{ message: string; feature_id: string; edges: string[] }>(
      id, 'fillet_edges', { edges, radius: radiusMm / 1000 }),
  chamferEdges: (id: string, edges: string[] | 'all', distanceMm: number) =>
    api.runOperation<{ message: string; feature_id: string; edges: string[] }>(
      id, 'chamfer_edges', { edges, distance: distanceMm / 1000 }),
  shellFaces: (id: string, faces: string[] | 'all', thicknessMm: number) =>
    api.runOperation<{ message: string; feature_id: string; faces: string[] }>(
      id, 'shell_faces', { faces, thickness: thicknessMm / 1000 }),
  /** Did any stored selection stop resolving? See ReferenceReport. */
  checkReferences: (id: string) => api.runOperation<ReferenceReport>(id, 'check_references'),

  // ── Design spec (what the part is *supposed* to be) ──
  getSpec: (id: string) => api.runOperation<PartSpec>(id, 'get_spec'),
  setSpec: (id: string, requirements: SpecRequirement[], goal?: string) =>
    api.runOperation<{ message: string; requirements: number }>(
      id, 'set_spec', goal ? { requirements, goal } : { requirements }),
  verifyAgainstSpec: (id: string) => api.runOperation<SpecReport>(id, 'verify_against_spec'),

  /**
   * The full FEA report, via the operation rather than `POST /simulate`.
   *
   * That endpoint's `SimulationResult` drops `solver` and `estimate_only`, so a
   * closed-form guess arrives looking exactly like a solve. The operation
   * returns the whole report and the panel can say which one ran.
   */
  analyse: (id: string, elements?: number) =>
    api.runOperation<AnalysisReport>(id, 'simulate', elements ? { elements } : {}),
  optimizeDocument: (id: string, body: { setup?: OptimizationSetup; resolution?: number }) =>
    fetchJson<OptimizeJob>(`/documents/${id}/optimize`, { method: 'POST', body: JSON.stringify(body) }),
  getOptimizeJob: (id: string, jobId: string) =>
    fetchJson<OptimizeJob>(`/documents/${id}/optimize/${jobId}`),
  simulateDocument: (id: string, resolution = 128) =>
    fetchJson<SimulationResult>(`/documents/${id}/simulate?resolution=${resolution}`, { method: 'POST' }),
  dfmDocument: (id: string, buildAxis = 'auto', resolution = 128, process: DfmProcess = 'additive') =>
    fetchJson<DfmReport>(
      `/documents/${id}/dfm?build_axis=${buildAxis}&resolution=${resolution}&process=${process}`,
      { method: 'POST' },
    ),
  exportDocumentStep: (id: string, resolution = 128) =>
    fetch(`/api/documents/${id}/step?resolution=${resolution}`, { headers: authHeaders() }).then((res) => {
      if (!res.ok) throw new Error(`API error: ${res.status}`)
      return res.text()
    }),
  camDocument: (
    id: string,
    tool: { diameter_mm?: number; stepdown_mm?: number; stepover_mm?: number; feed_mm_min?: number },
  ) => fetchJson<CamResult>(`/documents/${id}/cam`, { method: 'POST', body: JSON.stringify(tool) }),
  addDimension: (
    id: string,
    dim: { a: Vector3; b: Vector3; label?: string | null; source?: 'user' | 'llm' },
  ) => fetchJson<Dimension>(`/documents/${id}/dimensions`, { method: 'POST', body: JSON.stringify(dim) }),
  removeDimension: (id: string, dimId: string) =>
    fetchJson<{ status: string; id: string }>(`/documents/${id}/dimensions/${dimId}`, { method: 'DELETE' }),

  solveSketch: (spec: {
    points: unknown[]
    lines?: unknown[]
    arcs?: unknown[]
    circles?: unknown[]
    constraints?: unknown[]
  }) => fetchJson<SketchSolution>('/sketch/solve', { method: 'POST', body: JSON.stringify(spec) }),

  /** A sketch as its own feature: shared by every extrude/revolve/loft that reads it. */
  createSketch: (
    id: string,
    sketch: SketchSpec,
    where: { plane?: string; offset?: number; on_face?: unknown; name?: string } = {},
  ) => api.runOperation<SketchOpResult>(id, 'create_sketch', { sketch, ...where }),
  /** Re-solve a sketch feature or an add_sketch extrude; reports which features moved. */
  updateSketch: (id: string, featureId: string, sketch: SketchSpec, rest: Record<string, unknown> = {}) =>
    api.runOperation<SketchOpResult>(id, 'update_sketch', { feature_id: featureId, sketch, ...rest }),
  /** Re-solve without changing anything — status, dof, conflicting constraints. */
  checkSketch: (id: string, what: { feature_id?: string; sketch?: SketchSpec }) =>
    api.runOperation<SketchSolution & SketchOpResult>(id, 'check_sketch', what),

  // ── Materials, parameters, mass properties ──
  listMaterials: () =>
    fetchJson<{ materials: CatalogMaterial[]; categories: MaterialCategory[] }>(
      '/documents/meta/materials',
    ),
  setMaterial: (id: string, materialId: string) =>
    fetchJson<DesignDocument>(`/documents/${id}/material`, {
      method: 'POST', body: JSON.stringify({ material_id: materialId }),
    }),
  setParameters: (id: string, parameters: DocumentParameter[]) =>
    fetchJson<DesignDocument>(`/documents/${id}/parameters`, {
      method: 'PUT', body: JSON.stringify({ parameters }),
    }),
  massProperties: (id: string) => fetchJson<MassProperties>(`/documents/${id}/mass-properties`),

  // ── Drawings, tolerances, revisions ──
  drawing: (id: string, section = true) =>
    fetchJson<DrawingResult>(`/documents/${id}/drawing?section=${section}`),
  /** Render to an explicit spec: chosen views, sections, dimensions, sheet, scale. */
  drawingFromSpec: (id: string, spec: DrawingSpec) =>
    fetchJson<DrawingResult>(`/documents/${id}/drawing`, {
      method: 'POST',
      body: JSON.stringify(spec),
    }),
  /** Validate a drawing as a manufacturing document. */
  checkDrawing: (id: string, spec: DrawingSpec = {}) =>
    fetchJson<ValidationReport>(`/documents/${id}/drawing/check`, {
      method: 'POST',
      body: JSON.stringify(spec),
    }),
  computeFit: (nominalMm: number, fit: string) =>
    fetchJson<Record<string, unknown>>(`/documents/meta/fit?nominal_mm=${nominalMm}&fit=${encodeURIComponent(fit)}`),
  listRevisions: (id: string) => fetchJson<DocumentRevisionMeta[]>(`/documents/${id}/revisions`),
  releaseRevision: (id: string, notes = '') =>
    fetchJson<DocumentRevisionMeta>(`/documents/${id}/revisions`, { method: 'POST', body: JSON.stringify({ notes }) }),
  setLifecycleState: (id: string, state: string) =>
    fetchJson<DesignDocument>(`/documents/${id}/state`, { method: 'POST', body: JSON.stringify({ state }) }),
  listMachines: () => fetchJson<{ machines: MachineSpec[] }>('/machine/machines'),
  nestSheet: (id: string, machineId: string, quantity = 1, kerfMm = 0.2) =>
    fetchJson<NestResult>(`/documents/${id}/nest?machine_id=${machineId}&quantity=${quantity}&kerf_mm=${kerfMm}`),
  postGcode: (id: string, machineId: string, source: 'print' | 'cut' = 'print') =>
    fetchJson<{ machine: { id: string; name: string; dialect: string }; gcode: string; lines: number; stripped: number; bytes: number }>(
      `/documents/${id}/post`, { method: 'POST', body: JSON.stringify({ machine_id: machineId, source }) }),
  profileCut: (id: string, kerfMm = 0.2, plane = 'xy') =>
    fetchJson<{ path_count: number; cut_length_mm: number; estimated_minutes: number; size_mm: { u: number; v: number } }>(
      `/documents/${id}/profile-cut?kerf_mm=${kerfMm}&plane=${plane}`),
  downloadProfileCut: async (id: string, name = 'part', kerfMm = 0.2) => {
    const res = await fetch(`${API_BASE}/documents/${id}/profile-cut.gcode?kerf_mm=${kerfMm}`, { headers: authHeaders() })
    if (!res.ok) throw new Error(`Cut unavailable (${res.status})`)
    const blob = await res.blob()
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `${name.slice(0, 40).replace(/\s+/g, '_') || 'part'}_cut.gcode`
    a.click()
    URL.revokeObjectURL(a.href)
  },
  /** Sheet-metal blank as DXF — outline on the CUT layer, bend lines on BEND. */
  downloadFlatPattern: async (id: string, name = 'part') => {
    const res = await fetch(`${API_BASE}/documents/${id}/flat-pattern.dxf`, { headers: authHeaders() })
    if (!res.ok) throw new Error(`Flat pattern unavailable (${res.status})`)
    const blob = await res.blob()
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `${name.slice(0, 40).replace(/\s+/g, '_') || 'part'}_flat.dxf`
    a.click()
    URL.revokeObjectURL(a.href)
  },
  /** The drawing as R12 DXF — layered geometry a CAM package can measure. */
  downloadDrawingDxf: async (id: string, name = 'part', spec: DrawingSpec = {}) => {
    const res = await fetch(`${API_BASE}/documents/${id}/drawing.dxf`, {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(spec),
    })
    if (!res.ok) throw new Error(`Drawing unavailable (${res.status})`)
    const blob = await res.blob()
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `${name.slice(0, 40).replace(/\s+/g, '_') || 'part'}_drawing.dxf`
    a.click()
    URL.revokeObjectURL(a.href)
  },
  downloadDrawing: async (id: string, name = 'part') => {
    const res = await fetch(`${API_BASE}/documents/${id}/drawing.svg`, { headers: authHeaders() })
    if (!res.ok) throw new Error(`Drawing unavailable (${res.status})`)
    const blob = await res.blob()
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `${name.slice(0, 40).replace(/\s+/g, '_') || 'part'}_drawing.svg`
    a.click()
    URL.revokeObjectURL(a.href)
  },

  // ── FDM slicing ──
  sliceDocument: (id: string, settings: SliceSettings = {}) =>
    fetchJson<SliceResponse>(`/documents/${id}/slice`, { method: 'POST', body: JSON.stringify(settings) }),
  /** Download the last sliced G-code as a file (authenticated fetch → blob). */
  downloadGcode: async (id: string, name = 'part') => {
    const res = await fetch(`${API_BASE}/documents/${id}/gcode`, { headers: authHeaders() })
    if (!res.ok) throw new Error(`No G-code (${res.status}) — slice the part first`)
    const blob = await res.blob()
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `${name.slice(0, 40).replace(/\s+/g, '_') || 'part'}.gcode`
    a.click()
    URL.revokeObjectURL(a.href)
  },

  // ── Machine control (Moonraker / Mainsail / Klipper) ──
  listPrinters: () => fetchJson<MachinePrinter[]>('/machine/printers'),
  machineStatus: (url: string) =>
    fetchJson<MachineStatus>('/machine/status', { method: 'POST', body: JSON.stringify({ url }) }),
  machineJog: (url: string, axis: string, distance: number, feed = 1500) =>
    fetchJson<{ ok: boolean }>('/machine/jog', { method: 'POST', body: JSON.stringify({ url, axis, distance, feed }) }),
  machineHome: (url: string, axes = '') =>
    fetchJson<{ ok: boolean }>('/machine/home', { method: 'POST', body: JSON.stringify({ url, axes }) }),
  machinePrint: (url: string, action: string, filename?: string) =>
    fetchJson<{ ok: boolean }>('/machine/print', { method: 'POST', body: JSON.stringify({ url, action, filename }) }),
  machineSendDocument: (url: string, docId: string, start = false) =>
    fetchJson<{ ok: boolean }>('/machine/send-document', {
      method: 'POST',
      body: JSON.stringify({ url, doc_id: docId, start }),
    }),
  deleteDocument: (id: string) =>
    fetchJson<{ status: string; id: string }>(`/documents/${id}`, { method: 'DELETE' }),

  // ── Assemblies (compose parts) ──
  listAssemblies: () => fetchJson<AssemblySummary[]>('/assemblies'),
  /**
   * Text-to-assembly: one prompt → generated parts placed as an assembly.
   * Runs as a server-side job (a plan + one text-to-CAD run per part);
   * polls until done and returns the new assembly id.
   */
  assemblyFromPrompt: async (prompt: string, onProgress?: (msg: string) => void): Promise<string> => {
    let job = await fetchJson<AssemblyPromptJob>('/assemblies/from-prompt', {
      method: 'POST',
      body: JSON.stringify({ prompt }),
    })
    const deadline = Date.now() + 20 * 60_000
    while (job.status === 'running') {
      if (Date.now() > deadline) throw new Error('Assembly generation timed out')
      await new Promise((r) => setTimeout(r, 1500))
      job = await fetchJson<AssemblyPromptJob>(`/assemblies/from-prompt/${job.job_id}`)
      if (job.progress) onProgress?.(job.progress)
    }
    if (job.status !== 'done' || !job.assembly_id) {
      throw new Error(job.error || 'Assembly generation failed')
    }
    return job.assembly_id
  },
  createAssembly: (name?: string) =>
    fetchJson<AssemblyDoc>('/assemblies', { method: 'POST', body: JSON.stringify({ name }) }),
  getAssemblyDoc: (id: string) => fetchJson<AssemblyDoc>(`/assemblies/${id}`),
  renameAssembly: (id: string, name: string) =>
    fetchJson<AssemblyDoc>(`/assemblies/${id}`, { method: 'PUT', body: JSON.stringify({ name }) }),
  deleteAssembly: (id: string) =>
    fetchJson<{ ok: boolean }>(`/assemblies/${id}`, { method: 'DELETE' }),
  addComponent: (id: string, partId: string) =>
    fetchJson<AssemblyDoc>(`/assemblies/${id}/components`, {
      method: 'POST',
      body: JSON.stringify({ part_id: partId }),
    }),
  updateComponent: (id: string, componentId: string, transform: ComponentTransform) =>
    fetchJson<AssemblyDoc>(`/assemblies/${id}/components/${componentId}`, {
      method: 'PUT',
      body: JSON.stringify({ transform }),
    }),
  removeComponent: (id: string, componentId: string) =>
    fetchJson<AssemblyDoc>(`/assemblies/${id}/components/${componentId}`, { method: 'DELETE' }),

  /**
   * Mates, clash and the parts list.
   *
   * All three were implemented, routed and tested on the server and had no way
   * in: an assembly was a set of parts you nudged into place with numbers.
   * Adding a mate re-solves every placement, so the response is the whole
   * assembly rather than the mate — the same reason `addComponent` returns one.
   */
  addMate: (id: string, mate: Omit<AssemblyMate, 'id'>) =>
    fetchJson<AssemblyDoc>(`/assemblies/${id}/mates`, {
      method: 'POST',
      body: JSON.stringify(mate),
    }),
  removeMate: (id: string, mateId: string) =>
    fetchJson<AssemblyDoc>(`/assemblies/${id}/mates/${mateId}`, { method: 'DELETE' }),
  assemblyInterference: (id: string, clearanceMm = 1.0) =>
    fetchJson<InterferenceReport>(
      `/assemblies/${id}/interference?clearance_mm=${clearanceMm}`),
  assemblyBom: (id: string) => fetchJson<BomReport>(`/assemblies/${id}/bom`),
  /** The assembly half of the operation catalog — the same call an agent makes. */
  runAssemblyOperation: <T = Record<string, unknown>>(
    id: string, op: string, args?: Record<string, unknown>,
  ) => fetchJson<OperationEnvelope<T>>(`/assemblies/${id}/op?envelope=1`, {
    method: 'POST', body: JSON.stringify({ op, args: args ?? {} }),
  }),
  downloadAssemblyBomCsv: (id: string, name: string) =>
    downloadAuthed(`/assemblies/${id}/bom.csv`, `${name || 'assembly'}-bom.csv`),

  // ── Version history & collaboration ──
  listVersions: (id: string) => fetchJson<VersionSummary[]>(`/documents/${id}/versions`),
  createVersion: (id: string, label: string) =>
    fetchJson<DocumentVersion>(`/documents/${id}/versions`, {
      method: 'POST',
      body: JSON.stringify({ label }),
    }),
  restoreVersion: (id: string, versionId: string, resolution = 96) =>
    fetchJson<DesignDocument>(`/documents/${id}/versions/${versionId}/restore?resolution=${resolution}`, {
      method: 'POST',
    }),
  branchVersion: (id: string, versionId: string, name?: string, resolution = 96) =>
    fetchJson<DesignDocument>(`/documents/${id}/versions/${versionId}/branch`, {
      method: 'POST',
      body: JSON.stringify({ name, resolution }),
    }),
  listCollaborators: (id: string) => fetchJson<Collaborator[]>(`/documents/${id}/collaborators`),
  addCollaborator: (id: string, email: string, role: 'viewer' | 'editor') =>
    fetchJson<DesignDocument>(`/documents/${id}/collaborators`, {
      method: 'POST',
      body: JSON.stringify({ email, role }),
    }),
  removeCollaborator: (id: string, accountId: string) =>
    fetchJson<DesignDocument>(`/documents/${id}/collaborators/${accountId}`, { method: 'DELETE' }),

  // ── Auth & subscription ──
  signup: (email: string, password: string) =>
    fetchJson<{ token: string; account: AccountView }>('/auth/signup', {
      method: 'POST',
      body: JSON.stringify({ email, password }),
    }),
  login: (email: string, password: string) =>
    fetchJson<{ token: string; account: AccountView }>('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email, password }),
    }),
  me: () => fetchJson<AccountView>('/auth/me'),
  /** OAuth providers with credentials configured on the server (may be empty). */
  oauthProviders: () => fetchJson<{ providers: OAuthProvider[] }>('/auth/oauth/providers'),
  /** Redeem the single-use code from the OAuth callback redirect for a session. */
  oauthExchange: (code: string) =>
    fetchJson<{ token: string; account: AccountView }>('/auth/oauth/exchange', {
      method: 'POST',
      body: JSON.stringify({ code }),
    }),
  listPlans: () =>
    fetchJson<{ plans: PlanSummary[]; billing: { provider: string; live: boolean; note: string | null } }>('/auth/plans'),
  subscribe: (planId: string) =>
    fetchJson<{ status: string; plan?: AccountView['plan']; url?: string }>('/auth/subscribe', {
      method: 'POST',
      body: JSON.stringify({ plan_id: planId }),
    }),
  billingPortal: () => fetchJson<{ url: string }>('/auth/billing-portal', { method: 'POST' }),
  createApiKey: (name: string) =>
    fetchJson<{ api_key: string; warning: string; account: AccountView }>('/auth/api-keys', {
      method: 'POST',
      body: JSON.stringify({ name }),
    }),
  revokeApiKey: (keyId: string) =>
    fetchJson<{ revoked: string; account: AccountView }>(`/auth/api-keys/${keyId}`, { method: 'DELETE' }),
}

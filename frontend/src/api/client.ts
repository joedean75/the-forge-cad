import type {
  Material,
  DesignSpec,
  OptimizeJob,
  OptimizeResult,
  SimulationResult,
  ManufacturingResult,
  AssemblyResult,
  DfmReport,
  CamResult,
  Candidate,
  DesignGenerationResponse,
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
  AssemblySummary,
  ComponentTransform,
  CopilotResult,
  QuoteBundle,
  ManufacturingOrder,
  SliceSettings,
  SliceResponse,
} from '../../../shared/types'

const API_BASE = '/api'

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

async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${url}`, {
    ...init,
    headers: authHeaders({ 'Content-Type': 'application/json', ...(init?.headers as Record<string, string>) }),
  })
  if (!res.ok) {
    let detail = res.statusText
    try {
      detail = (await res.json())?.detail ?? detail
    } catch {
      /* non-JSON error body */
    }
    const err = new Error(`API error: ${res.status} ${detail}`) as Error & { status?: number }
    err.status = res.status
    throw err
  }
  return res.json() as Promise<T>
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

export const api = {
  health: () => fetchJson<{ status: string }>('/health'),
  materials: () => fetchJson<Material[]>('/materials'),
  optimize: (spec: DesignSpec) =>
    fetchJson<OptimizeJob>('/optimize', {
      method: 'POST',
      body: JSON.stringify(spec),
    }),
  getJob: (jobId: string) => fetchJson<OptimizeJob>(`/optimize/${jobId}`),
  getResult: (jobId: string) => fetchJson<OptimizeResult>(`/optimize/${jobId}/result`),
  simulate: (candidateId: string) =>
    fetchJson<SimulationResult>(`/simulate?candidate_id=${candidateId}`, { method: 'POST' }),
  manufacture: (candidateId: string, buildAxis: string = 'auto') =>
    fetchJson<ManufacturingResult>(`/manufacturing?candidate_id=${candidateId}&build_axis=${buildAxis}`, { method: 'POST' }),
  textToCad: (prompt: string, spec: DesignSpec) =>
    fetchJson<DesignSpec>('/text-to-cad', { method: 'POST', body: JSON.stringify({ prompt, spec }) }),
  assembly: () => fetchJson<AssemblyResult>('/assembly'),
  exportStep: (candidateId: string) =>
    fetch(`/api/export/${candidateId}/step`).then((res) => {
      if (!res.ok) throw new Error(`API error: ${res.status}`)
      return res.text()
    }),
  generatePart: (prompt: string, resolution: number = 128) =>
    fetchJson<Candidate>('/text-to-cad/generate', {
      method: 'POST',
      body: JSON.stringify({ prompt, resolution }),
    }),
  reevaluate: (candidateId: string, featureTree: object, resolution: number = 128) =>
    fetchJson<Candidate>(`/cad/${candidateId}/reevaluate`, {
      method: 'POST',
      body: JSON.stringify({ feature_tree: featureTree, resolution }),
    }),
  generateAssembly: (prompt: string, resolution: number = 128) =>
    fetchJson<AssemblyResult>('/assembly/generate', {
      method: 'POST',
      body: JSON.stringify({ prompt, resolution }),
    }),
  exportAssemblyStep: (assemblyId: string) =>
    fetch(`/api/export/assembly/${assemblyId}/step`).then((res) => {
      if (!res.ok) throw new Error(`API error: ${res.status}`)
      return res.text()
    }),
  listSavedCandidates: () => fetchJson<Candidate[]>('/cad/candidates'),
  listSavedAssemblies: () => fetchJson<AssemblyResult[]>('/cad/assemblies'),
  getAssembly: (id: string) => fetchJson<AssemblyResult>(`/cad/assemblies/${id}`),
  generateDesign: (prompt: string, resolution: number = 128) =>
    fetchJson<DesignGenerationResponse>('/design/generate', {
      method: 'POST',
      body: JSON.stringify({ prompt, resolution }),
    }),
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
  runOperation: (id: string, op: string, args?: Record<string, unknown>) =>
    fetchJson<{ ok: boolean; op: string; result: Record<string, unknown>; document?: DesignDocument }>(
      `/documents/${id}/op`,
      { method: 'POST', body: JSON.stringify({ op, args: args ?? {} }) },
    ),
  optimizeDocument: (id: string, body: { setup?: OptimizationSetup; resolution?: number }) =>
    fetchJson<OptimizeJob>(`/documents/${id}/optimize`, { method: 'POST', body: JSON.stringify(body) }),
  getOptimizeJob: (id: string, jobId: string) =>
    fetchJson<OptimizeJob>(`/documents/${id}/optimize/${jobId}`),
  simulateDocument: (id: string, resolution = 128) =>
    fetchJson<SimulationResult>(`/documents/${id}/simulate?resolution=${resolution}`, { method: 'POST' }),
  dfmDocument: (id: string, buildAxis = 'auto', resolution = 128) =>
    fetchJson<DfmReport>(`/documents/${id}/dfm?build_axis=${buildAxis}&resolution=${resolution}`, { method: 'POST' }),
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
    circles?: unknown[]
    constraints?: unknown[]
  }) =>
    fetchJson<{
      points: Record<string, [number, number]>
      radii: Record<string, number>
      residual: number
      converged: boolean
      polygon: [number, number][] | null
    }>('/sketch/solve', { method: 'POST', body: JSON.stringify(spec) }),

  // ── FDM slicing ──
  sliceDocument: (id: string, settings: SliceSettings = {}) =>
    fetchJson<SliceResponse>(`/documents/${id}/slice`, { method: 'POST', body: JSON.stringify(settings) }),
  /** Browser-navigable download URL for the last sliced G-code. */
  gcodeUrl: (id: string) => `/api/documents/${id}/gcode`,

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

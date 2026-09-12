import { create } from 'zustand'
import type {
  AssemblyDoc, AssemblyMate, BomReport, ComponentTransform, CopilotResult, InterferenceReport,
} from '../../../shared/types'
import { api } from '../api/client'

interface AssemblyState {
  assembly: AssemblyDoc | null
  busy: boolean
  error: string | null
  selectedComponentId: string | null
  /** A component's part was opened for editing, so this copy may be out of date. */
  stale: boolean
  refreshing: boolean
  markStale: () => void
  refresh: () => Promise<void>
  open: (id: string) => Promise<void>
  createAndOpen: (name?: string) => Promise<string | null>
  close: () => void
  rename: (name: string) => Promise<void>
  insertPart: (partId: string) => Promise<void>
  /** Place a standard purchased part by catalog key — no part document needed. */
  insertStandardPart: (key: string, quantity?: number,
                      params?: Record<string, number>) => Promise<void>
  removeComponent: (componentId: string) => Promise<void>
  setTransform: (componentId: string, transform: ComponentTransform) => Promise<void>
  select: (componentId: string | null) => void

  /**
   * Forge Copilot, pointed at this assembly.
   *
   * The same agentic loop the part editor runs, against the other half of the
   * operation catalog: it places components, mates them and measures the
   * result. It cannot change a part's own geometry — that is a run against the
   * part document.
   */
  runCopilot: (goal: string, onProgress?: (state: CopilotResult) => void,
               model?: string | null) => Promise<CopilotResult | undefined>

  /** Add a mate and re-solve every placement; the server returns the whole assembly. */
  addMate: (mate: Omit<AssemblyMate, 'id'>) => Promise<void>
  removeMate: (mateId: string) => Promise<void>
  /**
   * Which components a mate positions.
   *
   * A component named as a mate's `moved` is placed by the solver, so its
   * translation is an *output*. Dragging it writes a number the next solve
   * overwrites — the user drags, sees it snap back, and concludes the tool is
   * broken. The screen reads this to disable the fields rather than letting
   * that happen.
   */
  drivenComponentIds: () => Set<string>

  /** Pairwise clash, and the parts list. Both are readings of the assembly as
   *  it stands, so both are cleared the moment it changes. */
  clash: InterferenceReport | null
  bom: BomReport | null
  checkClash: (clearanceMm?: number) => Promise<void>
  loadBom: () => Promise<void>
  downloadBomCsv: () => Promise<void>
}

/**
 * The readings taken *from* an assembly, cleared the moment it changes.
 *
 * The same rule the document store follows: a clash report from before a
 * component moved still looks like an answer, and a parts list from before an
 * insert is missing a part. `useDocumentStore`'s `STALE` exists for exactly
 * this and for exactly this reason.
 */
const STALE = { clash: null, bom: null } as const

// Invalidates in-flight refreshes when the subject or its source changes.
let refreshRequest = 0

export const useAssemblyStore = create<AssemblyState>((set, get) => ({
  assembly: null,
  busy: false,
  error: null,
  selectedComponentId: null,
  stale: false,
  refreshing: false,
  clash: null,
  bom: null,

  open: async (id) => {
    refreshRequest++
    set({ busy: true, error: null, refreshing: false })
    try {
      set({ assembly: await api.getAssemblyDoc(id), selectedComponentId: null, stale: false, ...STALE })
    } catch (e) {
      set({ error: e instanceof Error ? e.message : 'Failed to open assembly' })
    } finally {
      set({ busy: false })
    }
  },

  markStale: () => {
    refreshRequest++
    set({ stale: true, refreshing: false, ...STALE })
  },

  refresh: async () => {
    const { assembly, stale, refreshing } = get()
    if (!assembly || !stale || refreshing) return
    const request = ++refreshRequest
    set({ refreshing: true, error: null })
    try {
      const updated = await api.getAssemblyDoc(assembly.id)
      if (request !== refreshRequest || get().assembly !== assembly) return
      const selected = get().selectedComponentId
      set({ assembly: updated, stale: false, ...STALE,
        selectedComponentId: updated.components.some((c) => c.id === selected) ? selected : null })
    } catch (e) {
      if (request === refreshRequest && get().assembly === assembly) {
        set({ stale: true, ...STALE,
          error: `Assembly could not refresh: ${e instanceof Error ? e.message : 'request failed'}. Retry to load the current parts.` })
      }
    } finally {
      if (request === refreshRequest) set({ refreshing: false })
    }
  },

  createAndOpen: async (name) => {
    refreshRequest++
    set({ busy: true, error: null, refreshing: false })
    try {
      const a = await api.createAssembly(name)
      set({ assembly: a, selectedComponentId: null, stale: false, ...STALE })
      return a.id
    } catch (e) {
      set({ error: e instanceof Error ? e.message : 'Failed to create assembly' })
      return null
    } finally {
      set({ busy: false })
    }
  },

  close: () => {
    refreshRequest++
    set({ assembly: null, selectedComponentId: null, error: null,
      stale: false, refreshing: false, ...STALE })
  },

  rename: async (name) => {
    const { assembly } = get()
    if (!assembly) return
    set({ assembly: { ...assembly, name } })
    try {
      await api.renameAssembly(assembly.id, name)
    } catch {
      /* cosmetic; next fetch reconciles */
    }
  },

  insertPart: async (partId) => {
    const { assembly } = get()
    if (!assembly) return
    set({ busy: true, error: null })
    try {
      const updated = await api.addComponent(assembly.id, partId)
      const last = updated.components[updated.components.length - 1]
      set({ assembly: updated, selectedComponentId: last?.id ?? null, ...STALE })
    } catch (e) {
      set({ error: e instanceof Error ? e.message : 'Failed to insert part' })
    } finally {
      set({ busy: false })
    }
  },

  insertStandardPart: async (key, quantity = 1, params) => {
    const { assembly } = get()
    if (!assembly || get().busy) return
    set({ busy: true, error: null })
    try {
      // `params` is omitted rather than sent empty when the row takes none: the
      // handler refuses a parameter the row does not declare, and an empty
      // object on a row with no parameters is a difference the reference would
      // have to encode.
      const env = await api.runAssemblyOperation(assembly.id, 'insert_standard_part',
                                                 { key, quantity,
                                                   ...(params ? { params } : {}) })
      if (!env.ok) throw new Error(env.error?.message ?? 'Insert failed')
      // Refetch unconditionally rather than through `refresh()`, which is gated
      // on `stale` — that flag means "a component's *part* was edited elsewhere",
      // and this is the assembly changing under our own hand.
      const updated = await api.getAssemblyDoc(assembly.id)
      const last = updated.components[updated.components.length - 1]
      if (get().assembly?.id !== assembly.id) return
      set({ assembly: updated, selectedComponentId: last?.id ?? null,
            stale: false, ...STALE })
    } catch (e) {
      set({ error: e instanceof Error ? e.message : 'Failed to insert the part' })
    } finally {
      set({ busy: false })
    }
  },

  removeComponent: async (componentId) => {
    const { assembly, selectedComponentId } = get()
    if (!assembly) return
    set({ busy: true, error: null })
    try {
      const updated = await api.removeComponent(assembly.id, componentId)
      set({
        assembly: updated,
        selectedComponentId: selectedComponentId === componentId ? null : selectedComponentId,
        ...STALE,
      })
    } catch (e) {
      set({ error: e instanceof Error ? e.message : 'Failed to remove component' })
    } finally {
      set({ busy: false })
    }
  },

  setTransform: async (componentId, transform) => {
    const { assembly } = get()
    if (!assembly) return
    // Optimistic: reflect the placement immediately, reconcile from the server.
    set({
      assembly: {
        ...assembly,
        components: assembly.components.map((c) => (c.id === componentId ? { ...c, transform } : c)),
      },
    })
    try {
      const updated = await api.updateComponent(assembly.id, componentId, transform)
      set({ assembly: updated, ...STALE })
    } catch (e) {
      set({ error: e instanceof Error ? e.message : 'Failed to move component' })
    }
  },

  select: (componentId) => set({ selectedComponentId: componentId }),

  runCopilot: async (goal, onProgress, model) => {
    const { assembly } = get()
    if (!assembly || !goal.trim()) return undefined
    set({ busy: true, error: null })
    try {
      // No step budget: the server's assembly default covers a whole build,
      // where each part is a nested part-copilot run.
      const res = await api.runCopilot(assembly.id, goal, undefined, onProgress,
                                      'assemblies', model)
      // A run takes minutes and places and mates components as it goes, so what
      // comes back *is* the assembly — but only adopt it if this is still the
      // one on screen, the rule every other long call here follows.
      if (res.assembly && get().assembly?.id === assembly.id) {
        const kept = res.assembly.components.some((c) => c.id === get().selectedComponentId)
        set({
          assembly: res.assembly,
          // The copilot may have removed the selected component; a selection
          // naming a component that is gone reads as a broken inspector.
          selectedComponentId: kept ? get().selectedComponentId : null,
          ...STALE,
        })
      }
      return res
    } catch (e) {
      if (get().assembly?.id === assembly.id) {
        set({ error: e instanceof Error ? e.message : 'Copilot failed' })
      }
      return undefined
    } finally {
      set({ busy: false })
    }
  },

  addMate: async (mate) => {
    const { assembly } = get()
    if (!assembly) return
    set({ busy: true, error: null })
    try {
      set({ assembly: await api.addMate(assembly.id, mate), ...STALE })
    } catch (e) {
      set({ error: e instanceof Error ? e.message : 'Could not add that mate' })
    } finally {
      set({ busy: false })
    }
  },

  removeMate: async (mateId) => {
    const { assembly } = get()
    if (!assembly) return
    set({ busy: true, error: null })
    try {
      set({ assembly: await api.removeMate(assembly.id, mateId), ...STALE })
    } catch (e) {
      set({ error: e instanceof Error ? e.message : 'Could not remove that mate' })
    } finally {
      set({ busy: false })
    }
  },

  drivenComponentIds: () => new Set((get().assembly?.mates ?? []).map((m) => m.moved)),

  checkClash: async (clearanceMm = 1.0) => {
    const { assembly } = get()
    if (!assembly) return
    set({ busy: true, error: null })
    try {
      set({ clash: await api.assemblyInterference(assembly.id, clearanceMm) })
    } catch (e) {
      set({ error: e instanceof Error ? e.message : 'Interference check failed' })
    } finally {
      set({ busy: false })
    }
  },

  loadBom: async () => {
    const { assembly } = get()
    if (!assembly) return
    set({ busy: true, error: null })
    try {
      set({ bom: await api.assemblyBom(assembly.id) })
    } catch (e) {
      set({ error: e instanceof Error ? e.message : 'Could not build the parts list' })
    } finally {
      set({ busy: false })
    }
  },

  downloadBomCsv: async () => {
    const { assembly } = get()
    if (!assembly) return
    try {
      await api.downloadAssemblyBomCsv(assembly.id, assembly.name)
    } catch (e) {
      set({ error: e instanceof Error ? e.message : 'CSV download failed' })
    }
  },
}))

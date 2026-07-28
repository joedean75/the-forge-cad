import { create } from 'zustand'
import type { AssemblyDoc, ComponentTransform } from '../../../shared/types'
import { api } from '../api/client'

interface AssemblyState {
  assembly: AssemblyDoc | null
  busy: boolean
  error: string | null
  selectedComponentId: string | null
  /** A component's part was opened for editing, so this copy may be out of date. */
  stale: boolean
  markStale: () => void
  refresh: () => Promise<void>
  open: (id: string) => Promise<void>
  createAndOpen: (name?: string) => Promise<string | null>
  close: () => void
  rename: (name: string) => Promise<void>
  insertPart: (partId: string) => Promise<void>
  removeComponent: (componentId: string) => Promise<void>
  setTransform: (componentId: string, transform: ComponentTransform) => Promise<void>
  select: (componentId: string | null) => void
}

export const useAssemblyStore = create<AssemblyState>((set, get) => ({
  assembly: null,
  busy: false,
  error: null,
  selectedComponentId: null,
  stale: false,

  open: async (id) => {
    set({ busy: true, error: null })
    try {
      set({ assembly: await api.getAssemblyDoc(id), selectedComponentId: null, stale: false })
    } catch (e) {
      set({ error: e instanceof Error ? e.message : 'Failed to open assembly' })
    } finally {
      set({ busy: false })
    }
  },

  markStale: () => set({ stale: true }),

  refresh: async () => {
    const { assembly, stale } = get()
    if (!assembly || !stale) return
    // Components carry their part's geometry, so an edited part leaves this copy
    // showing the old shape. Refetch, but only when a part was actually opened
    // for editing — the payload is the sum of every component's mesh.
    try {
      const selected = get().selectedComponentId
      set({ assembly: await api.getAssemblyDoc(assembly.id), selectedComponentId: selected, stale: false })
    } catch {
      set({ stale: false })  // a failed refresh must not retry forever
    }
  },

  createAndOpen: async (name) => {
    set({ busy: true, error: null })
    try {
      const a = await api.createAssembly(name)
      set({ assembly: a, selectedComponentId: null })
      return a.id
    } catch (e) {
      set({ error: e instanceof Error ? e.message : 'Failed to create assembly' })
      return null
    } finally {
      set({ busy: false })
    }
  },

  close: () => set({ assembly: null, selectedComponentId: null, error: null }),

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
      set({ assembly: updated, selectedComponentId: last?.id ?? null })
    } catch (e) {
      set({ error: e instanceof Error ? e.message : 'Failed to insert part' })
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
      set({ assembly: updated })
    } catch (e) {
      set({ error: e instanceof Error ? e.message : 'Failed to move component' })
    }
  },

  select: (componentId) => set({ selectedComponentId: componentId }),
}))

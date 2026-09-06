/**
 * What is picked, what is being sketched, and what the spec says about it.
 *
 * Selection outlives an edit but not a document switch, which is why it is its
 * own slice: the face and edge listings *are* readings of the geometry (their
 * ids are derived from what the entities currently measure) and go stale with
 * everything else, while the feature selection and the open sketch do not.
 *
 * Every listing here carries a request ticket. A face list that arrives after
 * the body has moved names entities that no longer exist, and an operation
 * built from it fails one id at a time — which reads as a broken fillet rather
 * than as a stale selection.
 */

import type { EdgeRef, FaceRef } from '../../../../shared/types'
import { api } from '../../api/client'
import { historySyncSettled } from './documentSlice'
import {
  BLANK_TOPOLOGY, STALE, adopt, current, issue, message, pushHistory, sameDocument,
} from './shared'
import type { Get, SelectionSlice, Set } from './shared'

export function createSelectionSlice(set: Set, get: Get): SelectionSlice {
  return {
    mode: 'model',
    selectedFeatureId: null,
    sketchOpen: false,
    sketchEdit: null,
    sketchPick: false,
    sketchPreset: null,
    topo: BLANK_TOPOLOGY,
    refs: null,
    specReport: null,

    setMode: (mode) => set({ mode }),
    select: (selectedFeatureId) => set({ selectedFeatureId }),

    openSketch: (edit) =>
      set({ sketchOpen: true, sketchEdit: edit ?? null, sketchPick: false, sketchPreset: null }),
    closeSketch: () =>
      set({ sketchOpen: false, sketchEdit: null, sketchPick: false, sketchPreset: null }),
    startSketchPick: () => set({ sketchPick: true, sketchOpen: false, sketchEdit: null }),
    cancelSketchPick: () => set({ sketchPick: false }),
    pickSketchOnFace: (featureId, face) =>
      set({ sketchPick: false, sketchOpen: true, sketchEdit: null,
            sketchPreset: { onFace: { featureId, face } } }),
    pickSketchPlane: (plane) =>
      set({ sketchPick: false, sketchOpen: true, sketchEdit: null, sketchPreset: { plane } }),

    loadTopology: async (kind) => {
      const { doc, topo } = get()
      if (!doc) return
      const ticket = issue(get)
      // A selection of faces means nothing to fillet_edges, so switching kind
      // drops it rather than carrying ids across into an operation that will
      // reject them one at a time.
      set({ topo: { ...topo, kind, loading: true, error: null,
                    selected: kind === topo.kind ? topo.selected : [] } })
      try {
        const env = kind === 'face' ? await api.listFaces(doc.id) : await api.listEdges(doc.id)
        // A listing that arrives after the body moved names entities that no
        // longer exist. Discarding it leaves the panel loading until the caller
        // asks again, which is what an edit does.
        if (!current(get, ticket)) return
        if (!env.ok) {
          set({ topo: { ...get().topo, loading: false,
                        error: env.error?.message ?? 'Unavailable' } })
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
        if (!current(get, ticket)) return
        set({ topo: { ...get().topo, loading: false, error: message(e, 'Unavailable') } })
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
      const { doc, topo } = get()
      if (!doc || topo.selected.length === 0) return
      const ticket = issue(get)
      set({ past: pushHistory(get().past, doc), future: [], busy: true, error: null })
      try {
        const call = op === 'fillet' ? api.filletEdges
          : op === 'chamfer' ? api.chamferEdges : api.shellFaces
        const env = await call(doc.id, topo.selected, sizeMm)
        if (!sameDocument(get, ticket)) return
        if (!env.ok) {
          // These operations roll their edit back, so the document is unchanged
          // and the message names the reason — a radius the geometry cannot carry.
          set({ error: env.error?.message ?? `${op} failed` })
          return
        }
        set({ ...adopt(get, await api.getDocument(doc.id)), ...STALE })
        await get().loadTopology(topo.kind)
        await get().checkReferences()
      } catch (e) {
        if (sameDocument(get, ticket)) set({ error: message(e, `${op} failed`) })
      } finally {
        set({ busy: false })
      }
    },

    checkReferences: async () => {
      const { doc } = get()
      if (!doc) return
      const ticket = issue(get)
      try {
        const env = await api.checkReferences(doc.id)
        if (!current(get, ticket)) return
        // A PRECONDITION here just means the body has no exact form; there is
        // nothing to report and nothing wrong.
        set({ refs: env.ok ? env.result : null })
      } catch {
        if (current(get, ticket)) set({ refs: null })
      }
    },

    saveSpec: async (requirements, goal) => {
      const { doc } = get()
      if (!doc) return
      const ticket = issue(get)
      set({ busy: true, error: null })
      try {
        const env = await api.setSpec(doc.id, requirements, goal)
        if (!sameDocument(get, ticket)) return
        if (!env.ok) { set({ error: env.error?.message ?? 'Could not save the spec' }); return }
        const held = get().doc
        if (held) set({ doc: { ...held, spec: { goal, requirements } }, specReport: null })
        await get().verifySpec()
      } catch (e) {
        if (sameDocument(get, ticket)) set({ error: message(e, 'Could not save the spec') })
      } finally {
        set({ busy: false })
      }
    },

    verifySpec: async () => {
      const { doc } = get()
      if (!doc) return
      const ticket = issue(get)
      try {
        const env = await api.verifyAgainstSpec(doc.id)
        // A score is a reading of the body: one that lands after an edit is an
        // answer about geometry that is no longer on screen.
        if (!current(get, ticket)) return
        set({ specReport: env.ok ? env.result : null,
              error: env.ok ? null : env.error?.message ?? null })
      } catch (e) {
        if (current(get, ticket)) set({ error: message(e, 'Could not verify the spec') })
      }
    },
  }
}

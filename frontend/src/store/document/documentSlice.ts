/**
 * The open part, its history, and every path that writes to it.
 *
 * Two invariants live here and nowhere else. **A write's response is only
 * adopted if it is still about the document on screen** — a user who opens
 * another part while an edit is in flight used to get the first part's geometry
 * painted over the second. And **undo is local, with the server catching up
 * behind it**: the tree is in hand and the mesh behind `derived.mesh_url` is
 * immutable and already cached in the browser, so there is nothing to wait for.
 */

import type { DesignDocument, FeatureTreeDict } from '../../../../shared/types'
import { api } from '../../api/client'
import {
  DEFAULT_RESOLUTION, STALE, adopt, asFailure, current, issue, message,
  pushHistory, sameDocument, withIds,
} from './shared'
import type { DocumentSlice, DocumentState, Get, HistoryEntry, Set, Ticket } from './shared'

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

/** Reset between documents, so a stale sync cannot gate the next part's edits. */
export function clearPendingSync(): void {
  pendingSync = null
}

export function createDocumentSlice(set: Set, get: Get): DocumentSlice {
  /** Open a document from scratch: no history, no readings, nothing selected. */
  const install = (doc: DesignDocument) => {
    clearPendingSync()
    set({
      ...adopt(get, doc),
      selectedFeatureId: doc.feature_tree.root?.id ?? null,
      mode: 'model',
      past: [],
      future: [],
      ...STALE,
    })
    // Re-attach to anything already running against this part. A reload during
    // a four-minute solve loses the job id with the tab; the document knows
    // what is running against it, because a job is a row. Fire-and-forget: it
    // is a reconnection, not a precondition for showing the part.
    void get().reconnectJobs()
  }

  /** Run a write, adopting its response only if it is still about this part. */
  const write = async (
    label: string,
    call: (doc: DesignDocument) => Promise<DesignDocument>,
    opts: { history?: boolean; reselectRoot?: boolean } = {},
  ) => {
    await historySyncSettled()
    const doc = get().doc
    if (!doc) return
    const ticket = issue(get)
    if (opts.history !== false) set({ past: pushHistory(get().past, doc), future: [] })
    set({ busy: true, error: null })
    try {
      const fresh = await call(doc)
      // Not `current`: a write's own response necessarily arrives after the
      // generation it was issued against, because the write is what moved it.
      // What must not happen is painting it onto a different document.
      if (!sameDocument(get, ticket)) return
      const next = adopt(get, fresh)
      set(opts.reselectRoot
        ? { ...next, selectedFeatureId: fresh.feature_tree.root?.id ?? null, ...STALE }
        : { ...next, ...STALE })
    } catch (e) {
      if (sameDocument(get, ticket)) set({ error: message(e, label) })
    } finally {
      set({ busy: false })
    }
  }

  return {
    doc: null,
    busy: false,
    error: null,
    failure: null,
    past: [],
    future: [],
    generation: 0,

    clearError: () => set({ error: null }),
    dismissFailure: () => set({ failure: null }),

    createFromPrompt: async (prompt) => {
      if (!prompt.trim()) return
      set({ busy: true, error: null })
      try {
        install(await api.createDocument({ prompt, resolution: DEFAULT_RESOLUTION }))
        const failed = get().doc?.derived.error
        if (failed) set({ error: failed })
      } catch (e) {
        set({ error: message(e, 'Failed to generate part') })
      } finally {
        set({ busy: false })
      }
    },

    createFromImage: async (file, prompt) => {
      set({ busy: true, error: null })
      try {
        install(await api.createDocumentFromImage(file, prompt))
        const failed = get().doc?.derived.error
        if (failed) set({ error: failed })
      } catch (e) {
        set({ error: message(e, 'Failed to build a part from that image') })
      } finally {
        set({ busy: false })
      }
    },

    newBlank: async () => {
      set({ busy: true, error: null })
      try {
        install(await api.createDocument({ resolution: DEFAULT_RESOLUTION }))
      } catch (e) {
        set({ error: message(e, 'Failed to create document') })
      } finally {
        set({ busy: false })
      }
    },

    openDocument: async (id) => {
      set({ busy: true, error: null })
      try {
        install(await api.getDocument(id))
      } catch (e) {
        set({ error: message(e, 'Failed to open document') })
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

    commitTree: async (tree: FeatureTreeDict) => {
      const doc = get().doc
      if (!doc) return
      // Optimistic: show the edited tree while the evaluation is in flight.
      set({ doc: { ...doc, feature_tree: tree } })
      await write('Failed to evaluate edit',
        (d) => api.updateDocument(d.id, { feature_tree: tree, resolution: DEFAULT_RESOLUTION }))
    },

    runOp: async (op, args) => {
      await historySyncSettled()
      const doc = get().doc
      if (!doc) return undefined
      const ticket = issue(get)
      set({ past: pushHistory(get().past, doc), future: [], busy: true, error: null, failure: null })
      try {
        const res = await api.runOperation(doc.id, op, args)
        if (res.document && sameDocument(get, ticket)) {
          set({ ...adopt(get, res.document), ...STALE })
        }
        return res.result
      } catch (e) {
        if (sameDocument(get, ticket)) {
          set({ failure: asFailure(e, op), error: message(e, `Operation ${op} failed`) })
        }
        return undefined
      } finally {
        set({ busy: false })
      }
    },

    aiEdit: async (instruction) => {
      if (!instruction.trim()) return
      await write('AI edit failed', (d) => api.aiEditDocument(d.id, instruction),
                  { reselectRoot: true })
    },

    setMaterial: async (materialId) => {
      // No history entry: a material cannot move the body, so an undo step for
      // it would restore a tree identical to the one on screen and read as a
      // no-op to whoever pressed it.
      const doc = get().doc
      if (!doc) return
      const ticket = issue(get)
      set({ busy: true, error: null })
      try {
        const fresh = await api.setMaterial(doc.id, materialId)
        if (sameDocument(get, ticket)) set(adopt(get, fresh))
      } catch (e) {
        if (sameDocument(get, ticket)) set({ error: message(e, 'Could not set material') })
      } finally {
        set({ busy: false })
      }
    },

    setParameters: async (parameters) => {
      await write('Could not set parameters', (d) => api.setParameters(d.id, parameters))
    },

    saveCheckpoint: async (label) => {
      const { doc, busy } = get()
      if (!doc) return false
      // The tree already persists on every mutation; a checkpoint is a named
      // version the user can restore. Skip while a mutation is still in flight.
      if (busy) return false
      try {
        const when = new Date().toLocaleString([], {
          month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
        await api.createVersion(doc.id, label ?? `Saved ${when}`)
        return true
      } catch (e) {
        set({ error: message(e, 'Save failed') })
        return false
      }
    },

    refreshDoc: async () => {
      const { doc, busy } = get()
      if (!doc || busy) return
      const ticket = issue(get)
      try {
        const fresh = await api.getDocument(doc.id)
        if (sameDocument(get, ticket)) set(adopt(get, fresh))
      } catch {
        /* transient — next event or action reconciles */
      }
    },

    addDimension: async (a, b, label) => {
      const { doc } = get()
      if (!doc) return
      const ticket = issue(get)
      try {
        const dim = await api.addDimension(doc.id, { a, b, label, source: 'user' })
        const held = get().doc
        if (held && sameDocument(get, ticket)) {
          set({ doc: { ...held, dimensions: [...held.dimensions, dim] } })
        }
      } catch (e) {
        set({ error: message(e, 'Failed to add dimension') })
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

    runCopilot: async (goal, onProgress) => {
      await historySyncSettled()
      const doc = get().doc
      if (!doc || !goal.trim()) return undefined
      const ticket = issue(get)
      set({ past: pushHistory(get().past, doc), future: [], busy: true, error: null })
      try {
        const res = await api.runCopilot(doc.id, goal, 8, onProgress)
        if (res.document && sameDocument(get, ticket)) {
          set({
            ...adopt(get, res.document),
            selectedFeatureId: res.document.feature_tree.root?.id ?? null,
            ...STALE,
          })
        }
        return res
      } catch (e) {
        if (sameDocument(get, ticket)) set({ error: message(e, 'Copilot failed') })
        return undefined
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
  }
}

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
function step(set: Set, get: Get, doc: DesignDocument, target: HistoryEntry,
              stacks: Partial<DocumentState>, failure: string) {
  const ticket: Ticket = { docId: doc.id, generation: get().generation + 1 }
  set({
    ...stacks,
    doc: { ...doc, feature_tree: target.tree, derived: target.derived },
    generation: ticket.generation,
    error: null,
    ...STALE,
  })
  pendingSync = api
    .updateDocument(doc.id, { feature_tree: target.tree, resolution: DEFAULT_RESOLUTION })
    .then((fresh) => {
      // Both checks matter: `current` catches an edit landing on top of the
      // undo, and it is keyed on the generation this step just minted rather
      // than on object identity of the tree.
      if (current(get, ticket)) {
        set({ doc: withIds(fresh), error: fresh.derived.error ?? null })
      }
    })
    .catch((e) => { if (sameDocument(get, ticket)) set({ error: message(e, failure) }) })
  const mine = pendingSync
  // Only the *latest* step clears the slot: a second undo lands while the first
  // is still in flight, and nulling unconditionally would let an edit through
  // while a write it must follow was still open.
  void mine.finally(() => { if (pendingSync === mine) pendingSync = null })
}

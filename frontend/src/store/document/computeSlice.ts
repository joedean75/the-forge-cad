/**
 * Readings of the geometry, and the jobs that produce them.
 *
 * Everything here answers a question *about* a body, so everything here is
 * discarded the moment the body moves — that is what the request ticket on each
 * call enforces. A safety factor computed before the wall was thinned still
 * looks like an answer, and it is the one class of stale data that is actively
 * dangerous rather than merely untidy.
 *
 * Polling lives in `polling.ts`: one loop with backoff, shared by every job
 * type, instead of a `while (status !== 'done') await sleep(500)` per panel.
 */

import type { DesignDocument, DfmProcess, OptimizationSetup } from '../../../../shared/types'
import { api } from '../../api/client'
import { pollJob } from './polling'
import { STALE, adopt, current, issue, message, sameDocument } from './shared'
import type { ComputeSlice, Get, Set } from './shared'

export function createComputeSlice(set: Set, get: Get): ComputeSlice {
  return {
    optimizeJob: null,
    sim: null,
    dfm: null,
    dfmProcess: null,
    cam: null,
    machineId: 'ender3',

    setMachineId: (id) => set({ machineId: id }),

    runOptimize: async (opts) => {
      const doc = get().doc
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
      const ticket = issue(get)
      set({ busy: true, error: null, optimizeJob: null })
      try {
        const started = await api.optimizeDocument(doc.id, { setup, resolution: setup.resolution })
        set({ optimizeJob: started })
        const job = await pollJob(
          () => api.getOptimizeJob(doc.id, started.id),
          (j) => j.status === 'done' || j.status === 'failed',
          (j) => { if (sameDocument(get, ticket)) set({ optimizeJob: j }) },
        )
        // A solve rewrites the tree, so the document has to be re-read whatever
        // else happened — but only onto the part it was a solve of.
        if (!sameDocument(get, ticket)) return
        if (job.status === 'failed') {
          set({ error: job.message })
          return
        }
        const fresh: DesignDocument = await api.getDocument(doc.id)
        set({
          ...adopt(get, fresh),
          selectedFeatureId: fresh.feature_tree.root?.id ?? null,
          mode: 'model',
          ...STALE,
        })
      } catch (e) {
        if (sameDocument(get, ticket)) set({ error: message(e, 'Optimization failed') })
      } finally {
        set({ busy: false })
      }
    },

    runSimulate: async (elements) => {
      const doc = get().doc
      if (!doc) return
      const ticket = issue(get)
      set({ busy: true, error: null })
      try {
        // Through the operation, not `POST /simulate`: that endpoint's response
        // model drops `solver` and `estimate_only`, so a closed-form guess would
        // reach the panel looking exactly like a real solve.
        const env = await api.analyse(doc.id, elements)
        if (!current(get, ticket)) return
        if (!env.ok) { set({ error: env.error?.message ?? 'Simulation failed' }); return }
        set({ sim: env.result })
      } catch (e) {
        if (current(get, ticket)) set({ error: message(e, 'Simulation failed') })
      } finally {
        set({ busy: false })
      }
    },

    runDfm: async (buildAxis = 'auto', process: DfmProcess = 'additive') => {
      const doc = get().doc
      if (!doc) return
      const ticket = issue(get)
      set({ busy: true, error: null })
      try {
        const dfm = await api.dfmDocument(doc.id, buildAxis, 96, process)
        if (!current(get, ticket)) return
        // `dfmProcess` is what was *asked for*. `dfm.process` is a display
        // label ('LPBF', 'MILLING') and cannot answer "is this report about the
        // process now selected?".
        set({ dfm, dfmProcess: process })
      } catch (e) {
        if (current(get, ticket)) set({ error: message(e, 'DFM analysis failed') })
      } finally {
        set({ busy: false })
      }
    },

    runCam: async (tool) => {
      const doc = get().doc
      if (!doc) return
      const ticket = issue(get)
      set({ busy: true, error: null })
      try {
        const cam = await api.camDocument(doc.id, tool)
        if (!current(get, ticket)) return
        set({ cam })
      } catch (e) {
        if (current(get, ticket)) set({ error: message(e, 'CAM generation failed') })
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
        set({ error: message(e, 'Export failed') })
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
  }
}

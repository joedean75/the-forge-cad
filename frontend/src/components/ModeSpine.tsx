import { useEffect } from 'react'
import { Boxes, Waypoints, Activity, Wrench, Printer, PlayCircle } from 'lucide-react'
import { useDocumentStore } from '../store/useDocumentStore'
import type { EditorMode } from '../../../shared/types'

/**
 * The load-path spine: the five-stage workflow as numbered stations strung on a
 * vertical line. Stations up to and including the active one read as "loaded"
 * (ember); stations ahead read as "void" (cool steel) — so the rail encodes where
 * you are in the pipeline, mirroring the stress-ramp language of the whole app.
 * This is the editor's signature element.
 */

const STATIONS: { id: EditorMode; label: string; icon: typeof Boxes }[] = [
  { id: 'model', label: 'Model', icon: Boxes },
  { id: 'optimize', label: 'Optimize', icon: Waypoints },
  { id: 'analysis', label: 'Analysis', icon: Activity },
  { id: 'make', label: 'Make', icon: Wrench },
  { id: 'validate', label: 'Validate', icon: PlayCircle },
  { id: 'machine', label: 'Machine', icon: Printer },
]

/**
 * The dot on a station: what that mode can do, and whether it is still true.
 *
 * The two that matter most are the two the spine could not previously show at
 * all. **Blocked** dims the icon: the station is still reachable, but the mode
 * cannot do anything yet and the tooltip names the step that unblocks it —
 * previously indistinguishable from "nothing has been run here", which is one
 * click away rather than three. **Stale** is amber: there is a result, it is on
 * screen, and it is about a body that has since moved.
 *
 * `current` gets no marker on purpose. It is the state everything is in most of
 * the time, and a badge on every station is a badge on none of them — the same
 * reason the sketch canvas leaves `under_constrained` uncoloured.
 */
const STATE_MARK: Record<string, { colour: string; title: string } | null> = {
  blocked: null,
  ready: null,
  current: null,
  queued: { colour: 'var(--color-cad-accent)', title: 'queued' },
  running: { colour: 'var(--color-cad-accent-hot)', title: 'running' },
  stale: { colour: 'var(--color-stress-warn, #d19a3a)', title: 'out of date' },
  failed: { colour: 'var(--color-stress-fail, #c2554a)', title: 'failed' },
}

export function ModeSpine() {
  const { mode, setMode, doc, workflow, refreshWorkflow, generation, dfmProcess } =
    useDocumentStore()
  const activeIdx = STATIONS.findIndex((s) => s.id === mode)
  const enabled = !!doc

  // Keyed on the document and its generation rather than on every render: the
  // answer only changes when the body does, and asking on each keystroke would
  // put a request behind the typing.
  const docId = doc?.id
  useEffect(() => {
    if (docId) void refreshWorkflow()
  }, [docId, generation, dfmProcess, refreshWorkflow])

  // Number keys 1–5 switch modes (skip while typing or with a modifier held).
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if (e.ctrlKey || e.metaKey || e.altKey) return
      const t = e.target as HTMLElement | null
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return
      const i = '123456'.indexOf(e.key)
      if (i >= 0 && useDocumentStore.getState().doc) {
        e.preventDefault()
        useDocumentStore.getState().setMode(STATIONS[i].id)
      }
    }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [])

  return (
    <nav
      aria-label="Workflow"
      className={`w-[76px] border-r border-cad-border bg-cad-panel flex flex-col shrink-0 z-10 ${enabled ? '' : 'opacity-45 pointer-events-none'}`}
    >
      {/* cap — aligns with the panel headers either side */}
      <div className="h-11 border-b border-cad-border grid place-items-center">
        <span className="eyebrow text-cad-text/50">Flow</span>
      </div>

      {/* stations, vertically centered as a deliberate instrument */}
      <div className="flex-1 flex flex-col items-center justify-center gap-0 py-4">
        {STATIONS.map((s, i) => {
          const active = i === activeIdx
          const loaded = i <= activeIdx // energized up to where you are
          const Icon = s.icon
          const status = workflow?.[s.id]
          const blocked = status?.state === 'blocked'
          const mark = status ? STATE_MARK[status.state] : null
          return (
            <div key={s.id} className="flex flex-col items-center">
              {/* incoming connector segment (skip before the first station) */}
              {i > 0 && (
                <span
                  aria-hidden
                  className="w-[2px] h-4 rounded-full"
                  style={{
                    background: i <= activeIdx ? 'var(--color-cad-accent)' : 'var(--color-stress-void)',
                  }}
                />
              )}
              <button
                onClick={() => setMode(s.id)}
                aria-current={active ? 'step' : undefined}
                title={status
                  ? `${String(i + 1).padStart(2, '0')} · ${s.label} — ${status.detail}`
                  : `${String(i + 1).padStart(2, '0')} · ${s.label}`}
                className="group flex flex-col items-center gap-1 py-1 px-1.5 rounded-lg outline-none focus-visible:ring-2 focus-visible:ring-cad-accent/60"
              >
                {/* node on the spine */}
                <span
                  className={`relative grid place-items-center w-9 h-9 rounded-full border transition-colors ${
                    active
                      ? 'border-cad-accent text-cad-accent-hot bg-cad-accent/10'
                      : loaded
                        ? 'border-cad-accent/50 text-cad-accent'
                        : 'border-stress-void text-cad-text/70 group-hover:text-cad-text-heading group-hover:border-cad-text/40'
                  }`}
                  style={active ? { boxShadow: '0 0 0 4px var(--color-cad-accent-glow)' } : undefined}
                >
                  <Icon size={16} strokeWidth={active ? 2.2 : 1.8}
                        opacity={blocked && !active ? 0.45 : 1} />
                  {active && (
                    <span
                      aria-hidden
                      className="absolute -right-0.5 -top-0.5 w-1.5 h-1.5 rounded-full bg-cad-accent-hot animate-filament"
                    />
                  )}
                  {mark && !active && (
                    <span
                      aria-hidden
                      title={mark.title}
                      className="absolute -right-0.5 -top-0.5 w-1.5 h-1.5 rounded-full"
                      style={{ background: mark.colour }}
                    />
                  )}
                </span>
                {/* index + label */}
                <span className="flex flex-col items-center leading-none gap-0.5">
                  <span
                    className={`tabular text-[8px] tracking-wider ${active ? 'text-cad-accent-hot' : loaded ? 'text-cad-accent/80' : 'text-cad-text/40'}`}
                  >
                    {String(i + 1).padStart(2, '0')}
                  </span>
                  <span
                    className={`text-[9px] font-medium tracking-wide ${
                      active ? 'text-cad-text-heading' : 'text-cad-text/70 group-hover:text-cad-text'
                    }`}
                  >
                    {s.label}
                  </span>
                </span>
              </button>
            </div>
          )
        })}
      </div>
    </nav>
  )
}

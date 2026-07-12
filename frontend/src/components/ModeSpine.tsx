import { Boxes, Waypoints, Activity, Wrench, Printer } from 'lucide-react'
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
  { id: 'validate', label: 'Validate', icon: Activity },
  { id: 'make', label: 'Make', icon: Wrench },
  { id: 'machine', label: 'Machine', icon: Printer },
]

export function ModeSpine() {
  const { mode, setMode, doc } = useDocumentStore()
  const activeIdx = STATIONS.findIndex((s) => s.id === mode)
  const enabled = !!doc

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
                title={`${String(i + 1).padStart(2, '0')} · ${s.label}`}
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
                  <Icon size={16} strokeWidth={active ? 2.2 : 1.8} />
                  {active && (
                    <span
                      aria-hidden
                      className="absolute -right-0.5 -top-0.5 w-1.5 h-1.5 rounded-full bg-cad-accent-hot animate-filament"
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

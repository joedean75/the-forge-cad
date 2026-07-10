import type { ComponentType } from 'react'
import {
  ListTodo,
  Sparkles,
  Box,
  Activity,
  Factory,
  Puzzle,
  Download,
} from 'lucide-react'
import { useAppStore } from '../store/useAppStore'
import type { WorkflowStep } from '../../../shared/types'

const STEPS: { id: WorkflowStep; label: string; icon: ComponentType<{ size?: number }> }[] = [
  { id: 'requirements', label: 'Requirements', icon: ListTodo },
  { id: 'generative', label: 'Generative', icon: Sparkles },
  { id: 'topology', label: 'Topology', icon: Box },
  { id: 'simulation', label: 'Simulation', icon: Activity },
  { id: 'manufacturing', label: 'Manufacture', icon: Factory },
  { id: 'assembly', label: 'Assembly', icon: Puzzle },
  { id: 'export', label: 'Export', icon: Download },
]

/**
 * The load-path spine. The workflow is a directed sequence, so it's drawn as a
 * structural member under load: connectors heat from cool steel to ember as
 * "load" (progress) reaches each node; the current node is the hot filament.
 */
export function Sidebar() {
  const { step, setStep } = useAppStore()
  const activeIndex = STEPS.findIndex((s) => s.id === step)

  return (
    <nav
      className="w-20 border-r border-cad-border bg-cad-panel flex flex-col items-center py-5 shrink-0 z-10"
      aria-label="Workflow"
    >
      {STEPS.map((s, i) => {
        const Icon = s.icon
        const active = i === activeIndex
        const energized = i < activeIndex // load has already flowed through
        // The connector entering this node carries load if load reached the node above.
        const inboundLoaded = i <= activeIndex

        return (
          <div key={s.id} className="relative flex flex-col items-center">
            {/* connector segment above this node (skip for the first) */}
            {i > 0 && (
              <span
                aria-hidden
                className="w-px h-4"
                style={{
                  background: inboundLoaded
                    ? 'linear-gradient(var(--color-cad-accent), var(--color-cad-accent))'
                    : 'var(--color-cad-border)',
                }}
              />
            )}

            <button
              onClick={() => setStep(s.id)}
              aria-current={active ? 'step' : undefined}
              className="group relative flex flex-col items-center gap-1 py-1 outline-none"
              title={s.label}
            >
              {/* datum index — the node's position on the load path */}
              <span
                className={`tabular absolute -left-1.5 top-1 text-[8px] leading-none transition-colors ${
                  active || energized ? 'text-cad-accent' : 'text-cad-text/50'
                }`}
              >
                {String(i + 1).padStart(2, '0')}
              </span>

              {/* the node */}
              <span
                className={[
                  'flex items-center justify-center w-9 h-9 rounded-full border transition-all',
                  active
                    ? 'border-cad-accent text-cad-accent-hot bg-cad-accent/10 animate-filament'
                    : energized
                    ? 'border-cad-accent/60 text-cad-accent bg-cad-accent/[0.06]'
                    : 'border-cad-border text-cad-text group-hover:border-stress-void group-hover:text-cad-text-heading',
                ].join(' ')}
                style={active ? { boxShadow: '0 0 0 4px var(--color-cad-accent-glow)' } : undefined}
              >
                <Icon size={17} />
              </span>

              <span
                className={`text-[8.5px] leading-none tracking-wide transition-colors ${
                  active ? 'text-cad-text-heading font-medium' : 'text-cad-text group-hover:text-cad-text-heading'
                }`}
              >
                {s.label}
              </span>
            </button>
          </div>
        )
      })}
    </nav>
  )
}

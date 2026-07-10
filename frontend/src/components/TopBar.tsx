import { Play, RotateCcw, Redo, Bell, User } from 'lucide-react'
import { useAppStore } from '../store/useAppStore'
import { api } from '../api/client'

/** Mini-truss mark: two anchored nodes carrying one loaded (ember) node — the product in miniature. */
function TrussMark() {
  return (
    <svg width="26" height="26" viewBox="0 0 24 24" fill="none" aria-hidden>
      <path d="M4 19 L12 6 L20 19 M4 19 L20 19 M12 6 L12 19" stroke="var(--color-stress-void)" strokeWidth="1.25" strokeLinejoin="round" />
      <circle cx="4" cy="19" r="1.8" fill="var(--color-cad-text)" />
      <circle cx="20" cy="19" r="1.8" fill="var(--color-cad-text)" />
      <circle cx="12" cy="6" r="2.4" fill="var(--color-cad-accent)" />
    </svg>
  )
}

export function TopBar() {
  const { spec, setStep, setActiveJob } = useAppStore()

  const runOptimization = async () => {
    try {
      const job = await api.optimize(spec)
      setActiveJob(job)
      setStep('generative')
    } catch (e) {
      console.error(e)
    }
  }

  return (
    <header className="h-14 border-b border-cad-border bg-cad-panel flex items-center justify-between pl-3 pr-4 shrink-0 z-20">
      <div className="flex items-center gap-3">
        <TrussMark />
        <div className="leading-none">
          <h1 className="font-display text-cad-text-heading font-semibold text-[15px] tracking-tight leading-none">
            DAPS
          </h1>
          <p className="eyebrow text-cad-text mt-1 leading-none">Divergent Adaptive Production</p>
        </div>

        <div className="h-7 w-px bg-cad-border mx-3" />

        <div className="leading-none">
          <span className="eyebrow text-cad-text/70">Active part</span>
          <div className="tabular text-[13px] text-cad-text-heading mt-1 leading-none">{spec.name}</div>
        </div>
      </div>

      <div className="flex items-center gap-2">
        <button className="p-2 rounded hover:bg-cad-raised text-cad-text hover:text-cad-text-heading transition" title="Undo">
          <RotateCcw size={16} />
        </button>
        <button className="p-2 rounded hover:bg-cad-raised text-cad-text hover:text-cad-text-heading transition" title="Redo">
          <Redo size={16} />
        </button>

        <div className="h-6 w-px bg-cad-border mx-1" />

        <button
          onClick={runOptimization}
          className="group flex items-center gap-2 pl-3 pr-3.5 py-2 rounded-sm bg-cad-accent text-cad-bg text-xs font-semibold tracking-wide hover:bg-cad-accent-hot transition-colors"
          style={{ boxShadow: '0 0 0 1px var(--color-cad-accent), 0 6px 20px -8px var(--color-cad-accent-glow)' }}
        >
          <Play size={13} fill="currentColor" />
          Run Generative Study
        </button>

        <div className="h-6 w-px bg-cad-border mx-1" />

        <button className="p-2 rounded hover:bg-cad-raised text-cad-text hover:text-cad-text-heading transition" title="Notifications">
          <Bell size={16} />
        </button>
        <button className="p-2 rounded hover:bg-cad-raised text-cad-text hover:text-cad-text-heading transition" title="Account">
          <User size={16} />
        </button>
      </div>
    </header>
  )
}

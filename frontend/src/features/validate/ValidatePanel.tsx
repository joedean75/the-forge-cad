import { Loader2, Activity } from 'lucide-react'
import { useDocumentStore } from '../../store/useDocumentStore'

export function ValidatePanel() {
  const { doc, busy, sim, runSimulate } = useDocumentStore()
  if (!doc) return <p className="text-[11px] text-cad-text/60">Open a part to validate it.</p>

  // Safety factor drives the readout tint: <1.5 danger, <3 caution, else within spec.
  const sf = sim?.safety_factor ?? 0
  const sfTone = sf < 1.5 ? 'text-cad-danger' : sf < 3 ? 'text-cad-warning' : 'text-cad-success'

  return (
    <div className="space-y-5">
      <p className="text-xs text-cad-text leading-relaxed">
        A coarse linear-elastic estimate of how the current body carries its load — stress,
        deflection, and safety margin against the material's yield.
      </p>

      <button
        onClick={runSimulate}
        disabled={busy}
        className="w-full flex items-center justify-center gap-2 py-2.5 rounded-sm bg-cad-accent text-cad-bg text-xs font-semibold hover:bg-cad-accent-hot transition-colors disabled:opacity-50"
      >
        {busy ? <Loader2 size={14} className="animate-spin" /> : <Activity size={14} />}
        {busy ? 'Solving…' : 'Run FEA estimate'}
      </button>

      {sim && (
        <>
          <div className="grid grid-cols-2 gap-2">
            <Metric label="Max stress" value={sim.max_stress.toFixed(1)} unit="MPa" />
            <Metric label="Safety factor" value={sf.toFixed(2)} unit="×" tone={sfTone} />
            <Metric label="Max displ." value={sim.max_displacement.toFixed(3)} unit="mm" />
            <Metric label="Mass" value={sim.mass.toFixed(3)} unit="kg" />
          </div>
          <div className="pt-3 border-t border-cad-border">
            <h3 className="sec-head mb-2">Compliance</h3>
            <p className="tabular text-xs text-cad-text-heading">{sim.compliance.toExponential(3)} J</p>
          </div>
        </>
      )}
    </div>
  )
}

function Metric({ label, value, unit, tone }: { label: string; value: string; unit: string; tone?: string }) {
  return (
    <div className="p-2.5 rounded-sm border border-cad-border bg-cad-bg/50">
      <div className="eyebrow text-cad-text/60">{label}</div>
      <div className="mt-1 flex items-baseline gap-1">
        <span className={`font-display text-lg font-semibold ${tone ?? 'text-cad-text-heading'}`}>{value}</span>
        <span className="tabular text-[10px] text-cad-text/60">{unit}</span>
      </div>
    </div>
  )
}

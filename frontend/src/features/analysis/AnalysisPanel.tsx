import { useState } from 'react'
import { Loader2, Activity, AlertTriangle, Info } from 'lucide-react'
import { useDocumentStore } from '../../store/useDocumentStore'

/**
 * Analysis — how the design carries its load, before it becomes a job.
 *
 * An 8-node hexahedral linear-static solve on the body itself, so the answer
 * depends on the shape, where the load is and where the part is held. When that
 * cannot run there is a closed-form fallback, and the one thing this panel must
 * never do is let the two look alike: the estimate is `total_force /
 * volume^(2/3)` clamped to a range, which cannot tell a cube from a cantilever.
 * `solver` and `estimate_only` come straight from the report and are shown
 * before any number is.
 *
 * (Validating the *manufacturing* job happens later, in Validate, by simulating
 * the build.)
 */

const ELEMENT_BUDGETS = [
  { value: 4000, label: 'Coarse' },
  { value: 8000, label: 'Normal' },
  { value: 20000, label: 'Fine' },
] as const

export function AnalysisPanel() {
  const { doc, busy, sim, runSimulate } = useDocumentStore()
  const [elements, setElements] = useState<number>(8000)
  if (!doc) return <p className="text-[11px] text-cad-text/60">Open a part to analyse it.</p>

  // Safety factor drives the readout tint: <1.5 danger, <3 caution, else within spec.
  const sf = sim?.safety_factor ?? 0
  const sfTone = sf < 1.5 ? 'text-cad-danger' : sf < 3 ? 'text-cad-warning' : 'text-cad-success'
  const estimate = sim?.estimate_only === true

  return (
    <div className="space-y-4">
      <p className="text-xs text-cad-text leading-relaxed">
        Linear-static FEA of the current body — von Mises stress, deflection, and margin against the
        material's yield. Loads and supports come from the optimization setup.
      </p>

      <div>
        <label className="eyebrow text-cad-text/60 block mb-1">Mesh</label>
        <div className="flex gap-px rounded-sm overflow-hidden border border-cad-border">
          {ELEMENT_BUDGETS.map((b) => (
            <button
              key={b.value}
              onClick={() => setElements(b.value)}
              className={`flex-1 px-2 py-1 text-[10px] transition-colors ${
                elements === b.value
                  ? 'bg-cad-accent text-cad-bg'
                  : 'bg-cad-bg text-cad-text hover:text-cad-text-heading'
              }`}
            >
              {b.label}
            </button>
          ))}
        </div>
        <p className="text-[10px] text-cad-text/50 mt-1">
          {elements.toLocaleString()} elements — finer resolves stress further and takes longer.
        </p>
      </div>

      <button
        onClick={() => runSimulate(elements)}
        disabled={busy}
        className="w-full flex items-center justify-center gap-2 py-2.5 rounded-sm bg-cad-accent text-cad-bg text-xs font-semibold hover:bg-cad-accent-hot transition-colors disabled:opacity-50"
      >
        {busy ? <Loader2 size={14} className="animate-spin" /> : <Activity size={14} />}
        {busy ? 'Solving…' : 'Run FEA'}
      </button>

      {sim && (
        <>
          {estimate ? (
            <div className="p-2.5 rounded-sm border border-cad-danger/50 bg-cad-danger/10 space-y-1">
              <div className="flex items-center gap-1.5">
                <AlertTriangle size={13} className="text-cad-danger shrink-0" />
                <span className="text-[11px] font-semibold text-cad-danger">This is not an analysis</span>
              </div>
              <p className="text-[10px] leading-snug text-cad-text">
                No solve ran, so these figures come from a closed-form estimate that does not depend on
                the shape, where the load sits, or where the part is held. Two different parts under the
                same load get the same answer — do not size anything from it.
              </p>
              {sim.reason && <p className="text-[10px] text-cad-text/60">Solver failed: {sim.reason}</p>}
            </div>
          ) : (
            <div className="flex items-center justify-between text-[10px] text-cad-text/60">
              <span className="tabular">{sim.solver}</span>
              {sim.mesh && <span className="tabular">{sim.mesh.elements.toLocaleString()} elements</span>}
            </div>
          )}

          <div className="grid grid-cols-2 gap-2">
            <Metric label="Peak stress" value={sim.max_stress.toFixed(1)} unit="MPa" />
            <Metric label="Safety factor" value={sf.toFixed(2)} unit="×" tone={sfTone} />
            <Metric label="Max displ." value={sim.max_displacement.toFixed(3)} unit="mm" />
            <Metric label="Mass" value={sim.mass.toFixed(3)} unit="kg" />
          </div>

          {sim.stress_p99 != null && (
            <div className="flex items-center justify-between p-2 rounded-sm border border-cad-border bg-cad-bg/50">
              <div>
                <div className="eyebrow text-cad-text/60">Stress, 99th percentile</div>
                <div className="text-[10px] text-cad-text/50 mt-0.5 leading-snug">
                  Stable under refinement, but below the true peak — context, not the number to size from.
                </div>
              </div>
              <span className="tabular text-sm text-cad-text-heading shrink-0 ml-2">
                {sim.stress_p99.toFixed(1)} MPa
              </span>
            </div>
          )}

          <dl className="space-y-1 text-[10px] text-cad-text pt-2 border-t border-cad-border">
            <Row label="Compliance" value={`${sim.compliance.toExponential(3)} J`} />
            {sim.material && <Row label="Material" value={sim.material} />}
            {sim.grounded_on && <Row label="Held at" value={sim.grounded_on} />}
            {sim.mesh && <Row label="Element size" value={`${sim.mesh.element_size_mm} mm`} />}
          </dl>

          {sim.notes && sim.notes.length > 0 && !estimate && <Notes notes={sim.notes} />}
        </>
      )}
    </div>
  )
}

/** The solver's own caveats. Collapsed by default; they are long and always true. */
function Notes({ notes }: { notes: string[] }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="pt-2 border-t border-cad-border">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1.5 text-[10px] text-cad-text hover:text-cad-text-heading transition-colors"
      >
        <Info size={11} />
        {open ? 'Hide' : 'How to read these numbers'} ({notes.length})
      </button>
      {open && (
        <ul className="mt-1.5 space-y-1.5">
          {notes.map((n, i) => (
            <li key={i} className="text-[10px] leading-snug text-cad-text/70 pl-3 border-l border-cad-border">
              {n}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between">
      <dt className="text-cad-text/70">{label}</dt>
      <dd className="tabular text-cad-text-heading">{value}</dd>
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

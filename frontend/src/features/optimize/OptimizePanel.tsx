import { useState } from 'react'
import { AlertTriangle, Loader2, Zap } from 'lucide-react'
import { useDocumentStore } from '../../store/useDocumentStore'
import type { Objective, RemovedFeature } from '../../../../shared/types'

const OBJECTIVES: { value: Objective['target']; label: string }[] = [
  { value: 'minimize_mass', label: 'Minimize mass' },
  { value: 'maximize_stiffness', label: 'Maximize stiffness' },
  { value: 'minimize_displacement', label: 'Minimize displacement' },
]

export function OptimizePanel() {
  const { doc, busy, optimizeJob, runOptimize, runOp } = useDocumentStore()
  const [volumeFraction, setVolumeFraction] = useState(0.4)
  const [objective, setObjective] = useState<Objective['target']>('minimize_mass')

  if (!doc) {
    return <p className="text-[11px] text-cad-text/60">Open a part to optimize it.</p>
  }

  const running = busy && optimizeJob !== null
  const pct = optimizeJob ? Math.round(optimizeJob.progress * 100) : 0

  // The job carries the warning while it's fresh; the tree carries it across a
  // reload, so a part reopened later still explains what its solve removed.
  const setup = doc.feature_tree.root?.params?.setup as { removed_features?: RemovedFeature[] } | undefined
  const persisted = setup?.removed_features ?? []
  const removed: RemovedFeature[] = optimizeJob?.removed_features ?? persisted

  const keepFeature = (featureId: string) => runOp('set_feature_keep', { feature_id: featureId, keep: true })

  return (
    <div className="space-y-5">
      <p className="text-xs text-cad-text leading-relaxed">
        Generative optimization removes material from the current body wherever load doesn't flow,
        keeping it grounded on its base. The result folds back in as a{' '}
        <span className="text-cad-accent-hot">Generative</span> feature you can keep editing.
      </p>

      <div>
        <h3 className="sec-head mb-2">Objective</h3>
        <select
          value={objective}
          onChange={(e) => setObjective(e.target.value as Objective['target'])}
          disabled={running}
          className="w-full px-2 py-1.5 rounded-sm bg-cad-bg border border-cad-border text-cad-text-heading text-xs focus:border-cad-accent outline-none disabled:opacity-50"
        >
          {OBJECTIVES.map((o) => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
      </div>

      <div>
        <h3 className="sec-head mb-2">Target volume fraction</h3>
        <div className="flex items-center gap-3">
          <input
            type="range"
            min={0.1}
            max={0.8}
            step={0.05}
            value={volumeFraction}
            onChange={(e) => setVolumeFraction(parseFloat(e.target.value))}
            disabled={running}
            className="flex-1 accent-[color:var(--color-cad-accent)]"
          />
          <span className="tabular text-xs text-cad-text-heading w-10 text-right">
            {Math.round(volumeFraction * 100)}%
          </span>
        </div>
        <p className="text-[10px] text-cad-text/60 mt-1.5">
          Fraction of the body's volume to keep. Lower = lighter, more skeletal.
        </p>
      </div>

      <button
        onClick={() => runOptimize({ volume_fraction: volumeFraction, objective: { target: objective, weight: 1 } })}
        disabled={running}
        className="w-full flex items-center justify-center gap-2 py-2.5 rounded-sm bg-cad-accent text-cad-bg text-xs font-semibold hover:bg-cad-accent-hot transition-colors disabled:opacity-50"
      >
        {running ? <Loader2 size={14} className="animate-spin" /> : <Zap size={14} fill="currentColor" />}
        {running ? 'Optimizing…' : 'Run optimization'}
      </button>

      {optimizeJob && (
        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <span className="eyebrow text-cad-text">{optimizeJob.status}</span>
            <span className="tabular text-xs text-cad-accent-hot">{pct}%</span>
          </div>
          <div className="h-1.5 bg-stress-void/25 rounded-full overflow-hidden">
            <div
              className="h-full rounded-full transition-all duration-500"
              style={{
                width: `${pct}%`,
                background:
                  'linear-gradient(90deg, var(--color-stress-void), var(--color-stress-mid) 45%, var(--color-cad-accent) 80%, var(--color-cad-accent-hot))',
              }}
            />
          </div>
          <p className="text-[10px] text-cad-text/70 truncate">{optimizeJob.message}</p>
        </div>
      )}

      {!running && removed.length > 0 && (
        <div className="rounded-sm border border-cad-warning/40 bg-cad-warning/5 p-2.5 space-y-2">
          <div className="flex items-start gap-2">
            <AlertTriangle size={13} className="text-cad-warning shrink-0 mt-px" />
            <div className="space-y-1">
              <h3 className="text-[11px] font-semibold text-cad-text-heading">
                Optimization removed {removed.length === 1 ? 'a feature' : `${removed.length} features`}
              </h3>
              <p className="text-[10px] text-cad-text/70 leading-snug">
                These carried no load, so the solve ate through them. If the part needs them anyway,
                mark them keep and run again.
              </p>
            </div>
          </div>
          <ul className="space-y-1">
            {removed.map((r) => (
              <li key={r.feature_id ?? r.name} className="flex items-center justify-between gap-2">
                <span className="text-[11px] text-cad-text-heading truncate">
                  {r.name}
                  {r.kept && <span className="ml-1 text-cad-warning">(kept, still removed)</span>}
                </span>
                <span className="flex items-center gap-2 shrink-0">
                  <span className="tabular text-[10px] text-cad-text/60">
                    {Math.round(r.remaining_fraction * 100)}% left
                  </span>
                  {r.feature_id && !r.kept && (
                    <button
                      onClick={() => keepFeature(r.feature_id as string)}
                      disabled={busy}
                      className="px-1.5 py-0.5 rounded-sm border border-cad-border text-[10px] text-cad-text hover:text-cad-accent hover:border-cad-accent transition-colors disabled:opacity-50"
                    >
                      Keep
                    </button>
                  )}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {doc.provenance === 'generative' && !running && (
        <div className="pt-3 border-t border-cad-border">
          <h3 className="sec-head mb-2">Result</h3>
          <dl className="space-y-1 text-[10px] text-cad-text">
            <div className="flex items-center justify-between">
              <dt>Optimized mass</dt>
              <dd className="tabular text-cad-text-heading">{doc.derived.mass.toFixed(3)} kg</dd>
            </div>
          </dl>
        </div>
      )}
    </div>
  )
}

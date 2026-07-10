import { useEffect, useState } from 'react'
import { AlertTriangle, CheckCircle2 } from 'lucide-react'
import { useAppStore } from '../../store/useAppStore'
import { api } from '../../api/client'
import type { ManufacturingResult } from '../../../../shared/types'

export function ManufacturingPanel() {
  const { selectedCandidate } = useAppStore()
  const [result, setResult] = useState<ManufacturingResult | null>(null)
  const [loading, setLoading] = useState(false)
  const [orientation, setOrientation] = useState<'auto' | 'x' | 'y' | 'z'>('auto')

  useEffect(() => {
    if (!selectedCandidate) return
    setLoading(true)
    api
      .manufacture(selectedCandidate.id, orientation)
      .then((res) => setResult(res))
      .finally(() => setLoading(false))
  }, [selectedCandidate, orientation])

  if (!selectedCandidate) {
    return (
      <div className="text-sm text-cad-text">
        No geometry to manufacture. Generate and select a candidate first.
      </div>
    )
  }

  if (loading || !result) {
    return (
      <div className="text-sm text-cad-text">
        Analyzing manufacturability...
      </div>
    )
  }

  const printability = result.printability_score
  const overhangWarning = result.overhang_angle > 45

  return (
    <div className="space-y-5">
      <section>
        <h3 className="sec-head mb-2">Build Orientation</h3>
        <select
          value={orientation}
          onChange={(e) => setOrientation(e.target.value as any)}
          className="w-full px-2 py-1.5 rounded bg-cad-bg border border-cad-border text-cad-text-heading text-xs focus:border-cad-accent outline-none"
        >
          <option value="auto">Auto-align ({result.build_orientation})</option>
          <option value="z">Along Z-axis</option>
          <option value="y">Along Y-axis</option>
          <option value="x">Along X-axis</option>
        </select>
      </section>

      <section>
        <h3 className="sec-head mb-2">Printability</h3>
        <div className="flex items-center gap-3 mb-3">
          <div className="relative w-16 h-16">
            <svg className="w-full h-full -rotate-90" viewBox="0 0 36 36">
              <path className="text-cad-border" d="M18 2.0845 a 15.9155 15.9155 0 0 1 0 31.831 a 15.9155 15.9155 0 0 1 0 -31.831" fill="none" stroke="currentColor" strokeWidth="3" />
              <path
                className={printability >= 80 ? 'text-cad-success' : printability >= 60 ? 'text-cad-warning' : 'text-cad-error'}
                strokeDasharray={`${printability}, 100`}
                d="M18 2.0845 a 15.9155 15.9155 0 0 1 0 31.831 a 15.9155 15.9155 0 0 1 0 -31.831"
                fill="none"
                stroke="currentColor"
                strokeWidth="3"
              />
            </svg>
            <div className="absolute inset-0 flex items-center justify-center text-xs font-medium text-cad-text-heading">
              {printability}
            </div>
          </div>
          <div className="text-xs text-cad-text">
            <div className="text-cad-text-heading">
              {printability >= 80 ? 'Good' : printability >= 60 ? 'Fair' : 'Poor'}
            </div>
            <div>Estimated printability score</div>
          </div>
        </div>

        <div className="grid grid-cols-2 gap-2 text-xs text-cad-text mb-3">
          <div className="p-2 rounded border border-cad-border bg-cad-bg/50">
            <div className="text-cad-text-heading">{result.print_time_minutes} min</div>
            <div>Print time</div>
          </div>
          <div className="p-2 rounded border border-cad-border bg-cad-bg/50">
            <div className="text-cad-text-heading">{(result.support_volume * 1e6).toFixed(2)} cm³</div>
            <div>Support volume</div>
          </div>
        </div>

        <label className="flex items-center justify-between text-xs text-cad-text py-2 border-t border-cad-border/50">
          Generate supports
          <input type="checkbox" checked className="accent-cad-accent" readOnly />
        </label>
      </section>

      <section>
        <h3 className="sec-head mb-2">Checks</h3>
        <div className="space-y-2">
          <div className="flex items-center gap-2 text-xs">
            {overhangWarning ? <AlertTriangle size={14} className="text-cad-warning" /> : <CheckCircle2 size={14} className="text-cad-success" />}
            <span className="text-cad-text">Overhang angle {result.overhang_angle.toFixed(1)}°</span>
          </div>
          <div className="flex items-center gap-2 text-xs">
            {result.feature_size_ok ? <CheckCircle2 size={14} className="text-cad-success" /> : <AlertTriangle size={14} className="text-cad-warning" />}
            <span className="text-cad-text">Feature size {result.feature_size_ok ? 'above' : 'below'} threshold</span>
          </div>
          <div className="flex items-center gap-2 text-xs">
            {result.enclosed_cavities ? <AlertTriangle size={14} className="text-cad-warning" /> : <CheckCircle2 size={14} className="text-cad-success" />}
            <span className="text-cad-text">{result.enclosed_cavities ? 'Possible enclosed cavities' : 'No enclosed cavities detected'}</span>
          </div>
        </div>
      </section>

      <section>
        <h3 className="sec-head mb-2">Machine Preset</h3>
        <div className="text-xs text-cad-text space-y-1">
          <div>LPBF Metal Printer</div>
          <div>Layer thickness: 30 μm</div>
          <div>Laser power: 400 W</div>
          <div>Material: Aluminum 7075</div>
        </div>
      </section>
    </div>
  )
}

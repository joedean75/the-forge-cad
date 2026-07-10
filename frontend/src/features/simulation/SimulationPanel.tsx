import { useEffect, useState } from 'react'
import { Activity } from 'lucide-react'
import { useAppStore } from '../../store/useAppStore'
import { api } from '../../api/client'
import type { SimulationResult } from '../../../../shared/types'

export function SimulationPanel() {
  const { selectedCandidate } = useAppStore()
  const [result, setResult] = useState<SimulationResult | null>(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!selectedCandidate) return
    setLoading(true)
    api
      .simulate(selectedCandidate.id)
      .then((res) => {
        setResult(res)
        useAppStore.getState().setSimulation(res)
      })
      .finally(() => setLoading(false))
  }, [selectedCandidate])

  if (!selectedCandidate) {
    return (
      <div className="text-sm text-cad-text">
        Select a candidate in <span className="text-cad-accent">Topology</span> to run simulation.
      </div>
    )
  }

  if (loading) {
    return (
      <div className="flex items-center gap-2 text-sm text-cad-text">
        <Activity size={16} className="animate-pulse text-cad-accent" />
        Running structural simulation...
      </div>
    )
  }

  if (!result) return null

  const Metric = ({ label, value, unit, color }: { label: string; value: string | number; unit: string; color?: string }) => (
    <div className="p-3 rounded border border-cad-border bg-cad-bg/50">
      <div className={`text-sm font-medium ${color || 'text-cad-text-heading'}`}>
        {value} <span className="text-[10px] text-cad-text">{unit}</span>
      </div>
      <div className="text-[10px] text-cad-text">{label}</div>
    </div>
  )

  return (
    <div className="space-y-5">
      <section>
        <h3 className="sec-head mb-2">FEA Results</h3>
        <div className="grid grid-cols-2 gap-2">
          <Metric label="Max Stress" value={result.max_stress} unit="MPa" color="text-cad-warning" />
          <Metric label="Max Displacement" value={result.max_displacement} unit="mm" />
          <Metric label="Safety Factor" value={result.safety_factor} unit="×" color="text-cad-success" />
          <Metric label="Compliance" value={result.compliance} unit="J" />
          <Metric label="Mass" value={result.mass} unit="kg" />
        </div>
      </section>

      <section>
        <h3 className="sec-head mb-2">Stress Legend</h3>
        <div className="h-4 rounded bg-gradient-to-r from-blue-500 via-green-400 to-red-500" />
        <div className="flex justify-between text-[10px] text-cad-text mt-1">
          <span>0 MPa</span>
          <span>{result.max_stress} MPa</span>
        </div>
      </section>

      <section>
        <h3 className="sec-head mb-2">Analysis Notes</h3>
        <p className="text-xs text-cad-text leading-relaxed">
          Results are computed from the SIMP displacement field using linear-elastic von Mises stress and scaled by the
          selected material's Young's modulus. Safety factor is based on the material yield strength. Validate with a
          certified solver before production.
        </p>
      </section>
    </div>
  )
}

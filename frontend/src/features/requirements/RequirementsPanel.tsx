import { useEffect, useState } from 'react'
import { Plus, Trash2 } from 'lucide-react'
import { useAppStore } from '../../store/useAppStore'
import { api } from '../../api/client'
import type { Material } from '../../../../shared/types'

export function RequirementsPanel() {
  const { spec, setSpec } = useAppStore()
  const [materials, setMaterials] = useState<Material[]>([])

  useEffect(() => {
    api.materials().then(setMaterials).catch(console.error)
  }, [])

  const updateDesignSpace = (axis: 'x' | 'y' | 'z', value: number) => {
    setSpec({
      ...spec,
      design_space: {
        ...spec.design_space,
        size: { ...spec.design_space.size, [axis]: value },
      },
    })
  }

  const addPreserve = () => {
    const id = `p${Date.now()}`
    setSpec({
      ...spec,
      preserve_regions: [
        ...spec.preserve_regions,
        { id, name: `Mount ${spec.preserve_regions.length + 1}`, type: 'sphere', center: { x: 0, y: 0, z: 0 }, radius: 0.2 },
      ],
    })
  }

  const removePreserve = (id: string) => {
    setSpec({ ...spec, preserve_regions: spec.preserve_regions.filter((r) => r.id !== id) })
  }

  const updatePreserve = (id: string, key: 'name' | 'radius', value: string | number) => {
    setSpec({
      ...spec,
      preserve_regions: spec.preserve_regions.map((r) => (r.id === id ? { ...r, [key]: value } : r)),
    })
  }

  const addLoad = () => {
    const id = `l${Date.now()}`
    setSpec({
      ...spec,
      load_cases: [
        ...spec.load_cases,
        { id, name: `Load ${spec.load_cases.length + 1}`, point: { x: 0, y: 0, z: 0 }, force: { x: 0, y: -1000, z: 0 }, load_type: 'static' },
      ],
    })
  }

  const removeLoad = (id: string) => {
    setSpec({ ...spec, load_cases: spec.load_cases.filter((l) => l.id !== id) })
  }

  const updateLoad = (id: string, axis: 'x' | 'y' | 'z', value: number) => {
    setSpec({
      ...spec,
      load_cases: spec.load_cases.map((l) =>
        l.id === id ? { ...l, force: { ...l.force, [axis]: value } } : l
      ),
    })
  }

  return (
    <div className="space-y-5">
      <section>
        <h3 className="sec-head mb-2">Design Space</h3>
        <div className="grid grid-cols-3 gap-2">
          {(['x', 'y', 'z'] as const).map((axis) => (
            <label key={axis} className="text-[10px] text-cad-text">
              {axis.toUpperCase()}
              <input
                type="number"
                step={0.1}
                value={spec.design_space.size[axis]}
                onChange={(e) => updateDesignSpace(axis, parseFloat(e.target.value))}
                className="w-full mt-1 px-2 py-1 rounded bg-cad-bg border border-cad-border text-cad-text-heading text-xs focus:border-cad-accent outline-none"
              />
            </label>
          ))}
        </div>
      </section>

      <section>
        <h3 className="sec-head mb-2">Preserve Regions</h3>
        <div className="space-y-2">
          {spec.preserve_regions.map((r) => (
            <div key={r.id} className="p-2 rounded border border-cad-border bg-cad-bg/50 space-y-2">
              <div className="flex items-center justify-between">
                <input
                  type="text"
                  value={r.name}
                  onChange={(e) => updatePreserve(r.id, 'name', e.target.value)}
                  className="bg-transparent text-xs text-cad-text-heading focus:outline-none w-32"
                />
                <button onClick={() => removePreserve(r.id)} className="text-cad-danger hover:text-red-400">
                  <Trash2 size={12} />
                </button>
              </div>
              <div className="grid grid-cols-2 gap-2">
                <label className="text-[10px] text-cad-text">
                  Radius
                  <input
                    type="number"
                    step={0.05}
                    value={r.radius}
                    onChange={(e) => updatePreserve(r.id, 'radius', parseFloat(e.target.value))}
                    className="w-full mt-1 px-2 py-1 rounded bg-cad-bg border border-cad-border text-cad-text-heading text-xs focus:border-cad-accent outline-none"
                  />
                </label>
              </div>
            </div>
          ))}
        </div>
        <button
          onClick={addPreserve}
          className="mt-2 flex items-center gap-1 text-[10px] text-cad-accent hover:text-blue-400"
        >
          <Plus size={12} /> Add preserve region
        </button>
      </section>

      <section>
        <h3 className="sec-head mb-2">Load Cases</h3>
        <div className="space-y-2">
          {spec.load_cases.map((l) => (
            <div key={l.id} className="p-2 rounded border border-cad-border bg-cad-bg/50 space-y-2">
              <div className="flex items-center justify-between">
                <span className="text-xs text-cad-text-heading">{l.name}</span>
                <button onClick={() => removeLoad(l.id)} className="text-cad-danger hover:text-red-400">
                  <Trash2 size={12} />
                </button>
              </div>
              <div className="grid grid-cols-3 gap-2">
                {(['x', 'y', 'z'] as const).map((axis) => (
                  <label key={axis} className="text-[10px] text-cad-text">
                    F{axis.toUpperCase()}
                    <input
                      type="number"
                      step={100}
                      value={l.force[axis]}
                      onChange={(e) => updateLoad(l.id, axis, parseFloat(e.target.value))}
                      className="w-full mt-1 px-2 py-1 rounded bg-cad-bg border border-cad-border text-cad-text-heading text-xs focus:border-cad-accent outline-none"
                    />
                  </label>
                ))}
              </div>
            </div>
          ))}
        </div>
        <button onClick={addLoad} className="mt-2 flex items-center gap-1 text-[10px] text-cad-accent hover:text-blue-400">
          <Plus size={12} /> Add load case
        </button>
      </section>

      <section>
        <h3 className="sec-head mb-2">Material</h3>
        <select
          value={spec.material_id}
          onChange={(e) => setSpec({ ...spec, material_id: e.target.value })}
          className="w-full px-2 py-1.5 rounded bg-cad-bg border border-cad-border text-cad-text-heading text-xs focus:border-cad-accent outline-none"
        >
          {materials.map((m) => (
            <option key={m.id} value={m.id}>
              {m.name}
            </option>
          ))}
        </select>
        {materials.length > 0 && (
          <div className="mt-2 text-[10px] text-cad-text space-y-1">
            {(() => {
              const m = materials.find((x) => x.id === spec.material_id)
              if (!m) return null
              return (
                <>
                  <div>Density: {m.density} kg/m³</div>
                  <div>Yield: {m.yield_strength} MPa</div>
                  <div>E-Modulus: {m.youngs_modulus} GPa</div>
                </>
              )
            })()}
          </div>
        )}
      </section>

      <section>
        <h3 className="sec-head mb-2">Objective</h3>
        <select
          value={spec.objective.target}
          onChange={(e) =>
            setSpec({
              ...spec,
              objective: { ...spec.objective, target: e.target.value as any },
            })
          }
          className="w-full px-2 py-1.5 rounded bg-cad-bg border border-cad-border text-cad-text-heading text-xs focus:border-cad-accent outline-none"
        >
          <option value="minimize_mass">Minimize Mass</option>
          <option value="maximize_stiffness">Maximize Stiffness</option>
          <option value="minimize_displacement">Minimize Displacement</option>
        </select>
      </section>
    </div>
  )
}

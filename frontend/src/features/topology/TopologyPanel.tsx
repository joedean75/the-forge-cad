import { useEffect, useState } from 'react'
import { useAppStore } from '../../store/useAppStore'
import { api } from '../../api/client'

interface FeatureNodeData {
  type?: string
  name?: string
  params?: Record<string, any>
}

function isVector(value: any): value is { x: number; y: number; z: number } {
  return value && typeof value === 'object' && 'x' in value && 'y' in value && 'z' in value
}

function FeatureEditor({
  node,
  onChange,
}: {
  node: FeatureNodeData
  onChange: (updated: FeatureNodeData) => void
}) {
  if (!node || typeof node !== 'object') return null

  const updateParam = (key: string, value: any) => {
    onChange({
      ...node,
      params: { ...node.params, [key]: value },
    })
  }

  const updateChild = (index: number, child: FeatureNodeData) => {
    const children = [...(node.params?.children || [])]
    children[index] = child
    onChange({
      ...node,
      params: { ...node.params, children },
    })
  }

  const renderVector = (key: string, v: { x: number; y: number; z: number }) => (
    <div className="flex gap-1 mt-0.5">
      {(['x', 'y', 'z'] as const).map((axis) => (
        <label key={axis} className="flex items-center gap-1 text-[10px] text-cad-text">
          {axis.toUpperCase()}
          <input
            type="number"
            step={0.001}
            value={v[axis]}
            onChange={(e) => updateParam(key, { ...v, [axis]: parseFloat(e.target.value) })}
            className="w-16 px-1 py-0.5 rounded bg-cad-bg border border-cad-border text-cad-text-heading text-[10px] focus:border-cad-accent outline-none"
          />
        </label>
      ))}
    </div>
  )

  const renderValue = (key: string, value: any) => {
    if (typeof value === 'number') {
      return (
        <input
          type="number"
          step={0.001}
          value={value}
          onChange={(e) => updateParam(key, parseFloat(e.target.value))}
          className="w-20 px-1 py-0.5 rounded bg-cad-bg border border-cad-border text-cad-text-heading text-[10px] focus:border-cad-accent outline-none"
        />
      )
    }
    if (isVector(value)) {
      return renderVector(key, value)
    }
    if (key === 'operation' && typeof value === 'string') {
      return (
        <select
          value={value}
          onChange={(e) => updateParam(key, e.target.value)}
          className="px-1 py-0.5 rounded bg-cad-bg border border-cad-border text-cad-text-heading text-[10px] focus:border-cad-accent outline-none"
        >
          <option value="union">union</option>
          <option value="subtract">subtract</option>
          <option value="intersect">intersect</option>
        </select>
      )
    }
    return <span className="text-cad-text">{JSON.stringify(value)}</span>
  }

  const label = node.name || node.type || 'Feature'

  return (
    <div className="text-[10px] py-0.5">
      <div className="text-cad-text-heading font-medium">{label}</div>
      {node.params &&
        Object.entries(node.params).map(([key, value]) => {
          if (key === 'children') return null
          return (
            <div key={key} className="pl-2">
              <span className="text-cad-text">{key}:</span>
              <div className="pl-2">{renderValue(key, value)}</div>
            </div>
          )
        })}
      {node.params?.children && (
        <div className="pl-2 border-l border-cad-border/50 mt-1">
          {(node.params.children as FeatureNodeData[]).map((child, idx) => (
            <FeatureEditor key={idx} node={child} onChange={(c) => updateChild(idx, c)} />
          ))}
        </div>
      )}
    </div>
  )
}

export function TopologyPanel() {
  const {
    selectedCandidate,
    setSelectedCandidate,
    showDesignSpace,
    setShowDesignSpace,
    showPreserve,
    setShowPreserve,
    showObstacles,
    setShowObstacles,
    showLoadPaths,
    setShowLoadPaths,
  } = useAppStore()

  const [tree, setTree] = useState<FeatureNodeData | null>(null)
  const [updating, setUpdating] = useState(false)

  useEffect(() => {
    if (selectedCandidate?.cad?.feature_tree) {
      setTree(JSON.parse(JSON.stringify(selectedCandidate.cad.feature_tree)))
    } else {
      setTree(null)
    }
  }, [selectedCandidate])

  if (!selectedCandidate) {
    return (
      <div className="text-sm text-cad-text">
        No candidate selected. Go to <span className="text-cad-accent">Generative</span> and pick an outcome first.
      </div>
    )
  }

  const updateGeometry = async () => {
    if (!tree) return
    setUpdating(true)
    try {
      const updated = await api.reevaluate(selectedCandidate.id, tree)
      setSelectedCandidate(updated)
    } catch (err) {
      console.error('Re-evaluation failed', err)
    } finally {
      setUpdating(false)
    }
  }

  const Toggle = ({
    label,
    value,
    onChange,
  }: {
    label: string
    value: boolean
    onChange: (v: boolean) => void
  }) => (
    <label className="flex items-center justify-between text-xs text-cad-text py-1.5 border-b border-cad-border/50">
      {label}
      <input
        type="checkbox"
        checked={value}
        onChange={(e) => onChange(e.target.checked)}
        className="accent-cad-accent"
      />
    </label>
  )

  return (
    <div className="space-y-5">
      <section>
        <h3 className="sec-head mb-2">
          Selected Outcome
        </h3>
        <div className="p-3 rounded border border-cad-border bg-cad-bg/50 text-xs space-y-2">
          <div className="flex justify-between">
            <span className="text-cad-text">Name</span>
            <span className="text-cad-text-heading">{selectedCandidate.name}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-cad-text">Mass</span>
            <span className="text-cad-text-heading">{selectedCandidate.mass} kg</span>
          </div>
          <div className="flex justify-between">
            <span className="text-cad-text">Stiffness</span>
            <span className="text-cad-text-heading">{selectedCandidate.stiffness.toExponential(2)}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-cad-text">Max Stress</span>
            <span className="text-cad-text-heading">{selectedCandidate.max_stress} MPa</span>
          </div>
          <div className="flex justify-between">
            <span className="text-cad-text">Score</span>
            <span className="text-cad-accent font-medium">{selectedCandidate.score}</span>
          </div>
        </div>
      </section>

      <section>
        <h3 className="sec-head mb-2">Visibility</h3>
        <div className="space-y-1">
          <Toggle label="Design Space" value={showDesignSpace} onChange={setShowDesignSpace} />
          <Toggle label="Preserve Regions" value={showPreserve} onChange={setShowPreserve} />
          <Toggle label="Obstacle Regions" value={showObstacles} onChange={setShowObstacles} />
          <Toggle label="Load Paths" value={showLoadPaths} onChange={setShowLoadPaths} />
        </div>
      </section>

      {tree && (
        <section>
          <div className="flex items-center justify-between mb-2">
            <h3 className="sec-head">Feature Tree</h3>
            <button
              onClick={updateGeometry}
              disabled={updating}
              className="px-2 py-0.5 rounded bg-cad-accent text-white text-[10px] disabled:opacity-50"
            >
              {updating ? 'Updating…' : 'Update Geometry'}
            </button>
          </div>
          <div className="p-2 rounded border border-cad-border bg-cad-bg/50 max-h-72 overflow-auto">
            <FeatureEditor node={tree} onChange={setTree} />
          </div>
        </section>
      )}

      <section>
        <h3 className="sec-head mb-2">Measurements</h3>
        <div className="grid grid-cols-2 gap-2 text-xs">
          <div className="p-2 rounded border border-cad-border bg-cad-bg/50">
            <div className="text-cad-text-heading">{(selectedCandidate.mass * 1.05).toFixed(2)} g</div>
            <div className="text-[10px] text-cad-text">Est. Volume</div>
          </div>
          <div className="p-2 rounded border border-cad-border bg-cad-bg/50">
            <div className="text-cad-text-heading">{(selectedCandidate.mass * 0.4).toFixed(1)} mm²</div>
            <div className="text-[10px] text-cad-text">Surface Area</div>
          </div>
        </div>
      </section>
    </div>
  )
}

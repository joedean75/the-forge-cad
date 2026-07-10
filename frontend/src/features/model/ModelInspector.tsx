import { useState } from 'react'
import { Wand2, Loader2, ArrowUp } from 'lucide-react'
import { useDocumentStore } from '../../store/useDocumentStore'
import { findNode, isBoolean } from '../../util/featureTree'

type Vec = { x: number; y: number; z: number }

/** Number field that edits in a display scale (mm) and commits on blur/Enter. */
function NumberField({
  label,
  value,
  scale,
  suffix,
  onCommit,
}: {
  label: string
  value: number
  scale: number
  suffix: string
  onCommit: (raw: number) => void
}) {
  const [text, setText] = useState<string>((value * scale).toFixed(scale === 1 ? 2 : 1))
  return (
    <label className="text-[10px] text-cad-text flex flex-col gap-1">
      <span className="tabular">{label}</span>
      <div className="flex items-center gap-1 px-2 py-1 rounded-sm bg-cad-bg border border-cad-border focus-within:border-cad-accent transition-colors">
        <input
          type="number"
          value={text}
          onChange={(e) => setText(e.target.value)}
          onBlur={() => {
            const parsed = parseFloat(text)
            if (!Number.isNaN(parsed)) onCommit(parsed / scale)
          }}
          onKeyDown={(e) => e.key === 'Enter' && (e.target as HTMLInputElement).blur()}
          className="tabular w-full bg-transparent text-xs text-cad-text-heading outline-none"
        />
        <span className="text-[9px] text-cad-text/60">{suffix}</span>
      </div>
    </label>
  )
}

function Vec3Field({
  label,
  value,
  scale,
  suffix,
  onCommit,
}: {
  label: string
  value: Vec
  scale: number
  suffix: string
  onCommit: (v: Vec) => void
}) {
  return (
    <div>
      <h3 className="sec-head mb-2">{label}</h3>
      <div className="grid grid-cols-3 gap-2">
        {(['x', 'y', 'z'] as const).map((axis) => (
          <NumberField
            key={axis}
            label={axis.toUpperCase()}
            value={value[axis]}
            scale={scale}
            suffix={suffix}
            onCommit={(raw) => onCommit({ ...value, [axis]: raw })}
          />
        ))}
      </div>
    </div>
  )
}

const MM = 1000

export function ModelInspector() {
  const { doc, selectedFeatureId, aiEdit, busy } = useDocumentStore()
  if (!doc) return <Empty text="No part open" />
  return (
    <div className="space-y-5">
      <AiEditBar onSubmit={aiEdit} busy={busy} />
      {selectedFeatureId ? (
        <FeatureFields />
      ) : (
        <p className="text-[11px] text-cad-text/60">Select a feature in the tree to edit its parameters.</p>
      )}
    </div>
  )
}

function AiEditBar({ onSubmit, busy }: { onSubmit: (s: string) => void; busy: boolean }) {
  const [text, setText] = useState('')
  const submit = () => {
    if (!text.trim() || busy) return
    onSubmit(text)
    setText('')
  }
  return (
    <div>
      <h3 className="sec-head mb-2">Edit with AI</h3>
      <div className="flex items-center gap-2 h-9 px-2.5 rounded-sm bg-cad-bg border border-cad-border focus-within:border-cad-accent transition-colors">
        <Wand2 size={13} className="text-cad-accent shrink-0" />
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && submit()}
          placeholder="e.g. add 4 M4 corner holes"
          disabled={busy}
          className="flex-1 bg-transparent text-xs text-cad-text-heading placeholder:text-cad-text/40 outline-none disabled:opacity-50"
        />
        <button
          onClick={submit}
          disabled={!text.trim() || busy}
          className="flex items-center justify-center h-6 w-6 rounded-sm bg-cad-accent text-cad-bg hover:bg-cad-accent-hot transition-colors disabled:opacity-40"
          title="Apply AI edit"
        >
          {busy ? <Loader2 size={12} className="animate-spin" /> : <ArrowUp size={13} />}
        </button>
      </div>
      <p className="text-[10px] text-cad-text/50 mt-1.5">Modifies the whole part — dimensions, holes, fillets, ribs.</p>
    </div>
  )
}

function FeatureFields() {
  const { doc, selectedFeatureId, runOp, openSketch } = useDocumentStore()
  const node = doc && selectedFeatureId ? findNode(doc.feature_tree, selectedFeatureId) : null
  if (!doc || !node) return null

  const commit = (params: Record<string, unknown>) => {
    runOp('set_feature_params', { feature_id: node.id, params })
  }
  const p = node.params

  return (
    // key on the node id so the value fields re-seed their local state when a
    // different feature is selected (otherwise they keep the prior feature's values).
    <div className="space-y-5" key={node.id}>
      <div>
        <span className="eyebrow text-cad-text/70">{node.type.replace('Feature', '')}</span>
        <h2 className="font-display text-sm font-semibold text-cad-text-heading mt-1">{node.name}</h2>
      </div>

      {node.type === 'BoxFeature' && (
        <>
          <Vec3Field label="Size" value={p.size as Vec} scale={MM} suffix="mm" onCommit={(v) => commit({ size: v })} />
          <Vec3Field label="Center" value={p.center as Vec} scale={MM} suffix="mm" onCommit={(v) => commit({ center: v })} />
        </>
      )}

      {node.type === 'SphereFeature' && (
        <>
          <ScalarRow label="Radius" value={p.radius as number} onCommit={(r) => commit({ radius: r })} />
          <Vec3Field label="Center" value={p.center as Vec} scale={MM} suffix="mm" onCommit={(v) => commit({ center: v })} />
        </>
      )}

      {(node.type === 'CylinderFeature' || node.type === 'ConeFeature') && (
        <>
          <ScalarRow
            label={node.type === 'ConeFeature' ? 'Base radius' : 'Radius'}
            value={(node.type === 'ConeFeature' ? p.base_radius : p.radius) as number}
            onCommit={(r) => commit(node.type === 'ConeFeature' ? { base_radius: r } : { radius: r })}
          />
          <ScalarRow label="Height" value={p.height as number} onCommit={(h) => commit({ height: h })} />
          <Vec3Field label="Center" value={p.center as Vec} scale={MM} suffix="mm" onCommit={(v) => commit({ center: v })} />
          <Vec3Field label="Axis" value={p.axis as Vec} scale={1} suffix="" onCommit={(v) => commit({ axis: v })} />
        </>
      )}

      {node.type === 'ExtrudeFeature' && p.sketch != null && (
        <>
          <button
            onClick={() =>
              openSketch({
                featureId: node.id!,
                sketch: p.sketch as never,
                plane: String(p.plane ?? 'xy'),
                depth: Number(p.depth ?? 0.02),
                offset: Number(p.offset ?? 0),
              })
            }
            className="w-full flex items-center justify-center gap-1.5 py-2 rounded-sm bg-cad-accent text-cad-bg text-xs font-semibold hover:bg-cad-accent-hot transition-colors"
          >
            Edit sketch
          </button>
          <div className="grid grid-cols-2 gap-2">
            <NumberField label="Depth" value={Number(p.depth ?? 0.02)} scale={MM} suffix="mm" onCommit={(d) => commit({ depth: d })} />
            <NumberField label="Offset" value={Number(p.offset ?? 0)} scale={MM} suffix="mm" onCommit={(o) => commit({ offset: o })} />
          </div>
        </>
      )}

      {node.type === 'ExtrudeFeature' && p.sketch == null && (() => {
        const profile = (p.profile ?? {}) as Record<string, number | string>
        const ptype = String(profile.type ?? 'rect')
        const setProfile = (patch: Record<string, unknown>) => commit({ profile: { ...profile, ...patch } })
        return (
          <>
            <div>
              <h3 className="sec-head mb-2">Sketch plane</h3>
              <select
                value={String(p.plane ?? 'xy')}
                onChange={(e) => commit({ plane: e.target.value })}
                className="w-full px-2 py-1.5 rounded-sm bg-cad-bg border border-cad-border text-cad-text-heading text-xs focus:border-cad-accent outline-none"
              >
                <option value="xy">XY (top)</option>
                <option value="xz">XZ (front)</option>
                <option value="yz">YZ (side)</option>
              </select>
            </div>
            <div>
              <h3 className="sec-head mb-2">Profile</h3>
              <select
                value={ptype}
                onChange={(e) =>
                  commit({
                    profile:
                      e.target.value === 'circle'
                        ? { type: 'circle', radius: 0.03, cx: 0, cy: 0 }
                        : { type: 'rect', width: 0.05, height: 0.03, cx: 0, cy: 0 },
                  })
                }
                className="w-full px-2 py-1.5 rounded-sm bg-cad-bg border border-cad-border text-cad-text-heading text-xs focus:border-cad-accent outline-none"
              >
                <option value="rect">Rectangle</option>
                <option value="circle">Circle</option>
                {ptype === 'polygon' && <option value="polygon">Polygon</option>}
              </select>
            </div>
            {ptype === 'rect' && (
              <div className="grid grid-cols-2 gap-2">
                <NumberField label="Width" value={Number(profile.width ?? 0.05)} scale={MM} suffix="mm" onCommit={(w) => setProfile({ width: w })} />
                <NumberField label="Height" value={Number(profile.height ?? 0.03)} scale={MM} suffix="mm" onCommit={(h) => setProfile({ height: h })} />
              </div>
            )}
            {ptype === 'circle' && (
              <ScalarRow label="Radius" value={Number(profile.radius ?? 0.03)} onCommit={(r) => setProfile({ radius: r })} />
            )}
            <div className="grid grid-cols-2 gap-2">
              <NumberField label="Depth" value={Number(p.depth ?? 0.02)} scale={MM} suffix="mm" onCommit={(d) => commit({ depth: d })} />
              <NumberField label="Offset" value={Number(p.offset ?? 0)} scale={MM} suffix="mm" onCommit={(o) => commit({ offset: o })} />
            </div>
          </>
        )
      })()}

      {(node.type === 'FilletFeature' || node.type === 'ChamferFeature') && (
        <ScalarRow label="Blend radius" value={p.radius as number} onCommit={(r) => commit({ radius: r })} />
      )}

      {node.type === 'ShellFeature' && (
        <ScalarRow label="Wall thickness" value={p.thickness as number} onCommit={(t) => commit({ thickness: t })} />
      )}

      {node.type === 'HoleFeature' && (
        <>
          <ScalarRow label="Radius" value={p.radius as number} onCommit={(r) => commit({ radius: r })} />
          <ScalarRow label="Depth" value={p.depth as number} onCommit={(d) => commit({ depth: d })} />
          <Vec3Field label="Center" value={p.center as Vec} scale={MM} suffix="mm" onCommit={(v) => commit({ center: v })} />
          <Vec3Field label="Axis" value={p.axis as Vec} scale={1} suffix="" onCommit={(v) => commit({ axis: v })} />
        </>
      )}

      {node.type === 'PatternFeature' && (
        <>
          <div>
            <h3 className="sec-head mb-2">Pattern</h3>
            <select
              value={String(p.kind)}
              onChange={(e) => commit({ kind: e.target.value })}
              className="w-full px-2 py-1.5 rounded-sm bg-cad-bg border border-cad-border text-cad-text-heading text-xs focus:border-cad-accent outline-none"
            >
              <option value="linear">Linear</option>
              <option value="circular">Circular</option>
            </select>
          </div>
          <IntRow label="Count" value={p.count as number} onCommit={(c) => commit({ count: c })} />
          {p.kind === 'linear' ? (
            <ScalarRow label="Spacing" value={p.spacing as number} onCommit={(s) => commit({ spacing: s })} />
          ) : (
            <Vec3Field label="Center" value={p.center as Vec} scale={MM} suffix="mm" onCommit={(v) => commit({ center: v })} />
          )}
          <Vec3Field label="Axis" value={p.axis as Vec} scale={1} suffix="" onCommit={(v) => commit({ axis: v })} />
        </>
      )}

      {node.type === 'TransformFeature' && (
        <>
          <Vec3Field label="Translate" value={p.translate as Vec} scale={MM} suffix="mm" onCommit={(v) => commit({ translate: v })} />
          <Vec3Field label="Rotate" value={p.rotate as Vec} scale={1} suffix="°" onCommit={(v) => commit({ rotate: v })} />
          <div>
            <h3 className="sec-head mb-2">Scale</h3>
            <NumberField label="" value={p.scale as number} scale={1} suffix="×" onCommit={(s) => commit({ scale: s })} />
          </div>
        </>
      )}

      {isBoolean(node) && (
        <div>
          <h3 className="sec-head mb-2">Operation</h3>
          <select
            value={String(p.operation)}
            onChange={(e) => commit({ operation: e.target.value })}
            className="w-full px-2 py-1.5 rounded-sm bg-cad-bg border border-cad-border text-cad-text-heading text-xs focus:border-cad-accent outline-none"
          >
            <option value="union">Union (add)</option>
            <option value="subtract">Subtract (cut)</option>
            <option value="intersect">Intersect</option>
          </select>
          <p className="text-[10px] text-cad-text/60 mt-2">
            Combines the child features below it in the tree.
          </p>
        </div>
      )}

      {doc.derived.bbox && (
        <div className="pt-3 border-t border-cad-border">
          <h3 className="sec-head mb-2">Body</h3>
          <dl className="space-y-1 text-[10px] text-cad-text">
            <Readout label="Mass" value={`${doc.derived.mass.toFixed(3)} kg`} />
            <Readout label="Volume" value={`${(doc.derived.volume * 1e6).toFixed(1)} cm³`} />
          </dl>
        </div>
      )}
    </div>
  )
}

function ScalarRow({ label, value, onCommit }: { label: string; value: number; onCommit: (r: number) => void }) {
  return (
    <div>
      <h3 className="sec-head mb-2">{label}</h3>
      <NumberField label="" value={value} scale={MM} suffix="mm" onCommit={onCommit} />
    </div>
  )
}

function IntRow({ label, value, onCommit }: { label: string; value: number; onCommit: (n: number) => void }) {
  const [text, setText] = useState(String(value))
  return (
    <div>
      <h3 className="sec-head mb-2">{label}</h3>
      <div className="flex items-center gap-1 px-2 py-1 rounded-sm bg-cad-bg border border-cad-border focus-within:border-cad-accent transition-colors">
        <input
          type="number"
          min={1}
          step={1}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onBlur={() => {
            const n = parseInt(text, 10)
            if (!Number.isNaN(n) && n >= 1) onCommit(n)
          }}
          onKeyDown={(e) => e.key === 'Enter' && (e.target as HTMLInputElement).blur()}
          className="tabular w-full bg-transparent text-xs text-cad-text-heading outline-none"
        />
      </div>
    </div>
  )
}

function Readout({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between">
      <dt>{label}</dt>
      <dd className="tabular text-cad-text-heading">{value}</dd>
    </div>
  )
}

function Empty({ text }: { text: string }) {
  return (
    <div className="flex items-center justify-center h-32 text-center">
      <p className="text-[11px] text-cad-text/60">{text}</p>
    </div>
  )
}

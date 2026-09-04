import { useEffect, useState } from 'react'
import { Wand2, Loader2, ArrowUp, Plus, Trash2, Scale } from 'lucide-react'
import { useDocumentStore } from '../../store/useDocumentStore'
import { findNode, isBoolean } from '../../util/featureTree'
import { api } from '../../api/client'
import { TopologyPicker } from './TopologyPicker'
import type { CatalogMaterial, DocumentParameter, MassProperties, MaterialCategory, SheetProperties } from '../../../../shared/types'

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
      {selectedFeatureId ? <FeatureFields /> : (
        <p className="text-[11px] text-cad-text/60">Select a feature in the tree to edit its parameters.</p>
      )}
      <TopologyPicker />
      <MaterialPicker />
      <ParametersEditor />
      <MassPropertiesPanel />
    </div>
  )
}

const TIER_FILTERS = [
  { id: 'all', label: 'All' },
  { id: 'maker', label: 'Maker' },
  { id: 'professional', label: 'Pro' },
] as const

type TierFilter = (typeof TIER_FILTERS)[number]['id']

function MaterialPicker() {
  const { doc, setMaterial, busy } = useDocumentStore()
  const [materials, setMaterials] = useState<CatalogMaterial[]>([])
  const [categories, setCategories] = useState<MaterialCategory[]>([])
  const [tier, setTier] = useState<TierFilter>('all')

  useEffect(() => {
    api.listMaterials()
      .then((r) => { setMaterials(r.materials); setCategories(r.categories) })
      .catch(() => {})
  }, [])

  const current = doc?.material_id ?? 'al-6061'
  const selected = materials.find((m) => m.id === current)

  // Group into the catalog's category order. The selected material always stays
  // listed even when the tier filter would hide it, so the <select> can't show
  // a blank value for what the document is actually made of.
  const groups = categories
    .map((c) => ({
      category: c,
      items: materials.filter(
        (m) => m.category === c.id && (tier === 'all' || m.tier === tier || m.id === current),
      ),
    }))
    .filter((g) => g.items.length > 0)

  if (!doc) return null

  return (
    <div className="pt-3 border-t border-cad-border">
      <div className="flex items-center justify-between mb-2">
        <h3 className="sec-head">Material</h3>
        <div className="flex gap-px rounded-sm overflow-hidden border border-cad-border">
          {TIER_FILTERS.map((t) => (
            <button
              key={t.id}
              onClick={() => setTier(t.id)}
              className={`px-1.5 py-0.5 text-[10px] uppercase tracking-wide transition-colors ${
                tier === t.id
                  ? 'bg-cad-accent text-cad-bg'
                  : 'bg-cad-bg text-cad-text hover:text-cad-text-heading'
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>
      </div>

      <select
        value={current}
        onChange={(e) => setMaterial(e.target.value)}
        disabled={busy}
        className="w-full px-2 py-1.5 rounded-sm bg-cad-bg border border-cad-border text-cad-text-heading text-xs focus:border-cad-accent outline-none disabled:opacity-50"
      >
        {groups.map((g) => (
          <optgroup key={g.category.id} label={g.category.label}>
            {g.items.map((m) => (
              <option key={m.id} value={m.id}>
                {m.name} · {(m.density / 1000).toFixed(2)} g/cm³
              </option>
            ))}
          </optgroup>
        ))}
      </select>

      {selected && (
        <div className="mt-2 space-y-1">
          <div className="flex items-center gap-2 text-[10px] text-cad-text">
            <span className="uppercase tracking-wide">{selected.tier}</span>
            <span className="opacity-40">·</span>
            <span>{(selected.yield_strength / 1e6).toFixed(0)} MPa yield</span>
            <span className="opacity-40">·</span>
            <span>${selected.cost_per_kg.toFixed(2)}/kg</span>
          </div>
          {selected.notes && (
            <p className="text-[10px] leading-snug text-cad-text opacity-70">{selected.notes}</p>
          )}
        </div>
      )}
    </div>
  )
}

function ParametersEditor() {
  const { doc, setParameters, busy } = useDocumentStore()
  const [rows, setRows] = useState<DocumentParameter[]>(doc?.parameters ?? [])
  useEffect(() => { setRows(doc?.parameters ?? []) }, [doc?.id, doc?.parameters])
  if (!doc) return null

  const commit = (next: DocumentParameter[]) => { setRows(next); setParameters(next) }
  const add = () => setRows([...rows, { name: `p${rows.length + 1}`, expr: '10', unit: 'mm' }])
  const edit = (i: number, patch: Partial<DocumentParameter>) =>
    setRows(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)))

  return (
    <div className="pt-3 border-t border-cad-border">
      <div className="flex items-center justify-between mb-2">
        <h3 className="sec-head">Parameters</h3>
        <button onClick={add} title="Add a driving parameter" className="text-cad-text hover:text-cad-accent-hot"><Plus size={13} /></button>
      </div>
      {rows.length === 0 ? (
        <p className="text-[10px] text-cad-text/50">Name a value once, drive it everywhere. Reference it in a field as <span className="tabular">=name</span>.</p>
      ) : (
        <div className="space-y-1.5">
          {rows.map((r, i) => (
            <div key={i} className="flex items-center gap-1">
              <input
                value={r.name}
                onChange={(e) => edit(i, { name: e.target.value })}
                onBlur={() => commit(rows)}
                className="tabular w-20 px-1.5 py-1 rounded-sm bg-cad-bg border border-cad-border text-cad-text-heading text-[11px] outline-none focus:border-cad-accent"
              />
              <span className="text-cad-text/40 text-[11px]">=</span>
              <input
                value={r.expr}
                onChange={(e) => edit(i, { expr: e.target.value })}
                onBlur={() => commit(rows)}
                onKeyDown={(e) => e.key === 'Enter' && commit(rows)}
                className="tabular flex-1 min-w-0 px-1.5 py-1 rounded-sm bg-cad-bg border border-cad-border text-cad-text-heading text-[11px] outline-none focus:border-cad-accent"
              />
              <span className="tabular text-[9px] text-cad-text/50 w-14 text-right">
                {r.value != null ? `${r.value}${r.unit === 'mm' ? 'mm' : ''}` : ''}
              </span>
              <button onClick={() => commit(rows.filter((_, j) => j !== i))} className="text-cad-text/60 hover:text-cad-danger"><Trash2 size={11} /></button>
            </div>
          ))}
        </div>
      )}
      {busy && <span className="text-[9px] text-cad-text/40">updating…</span>}
    </div>
  )
}

function MassPropertiesPanel() {
  const { doc } = useDocumentStore()
  const [mp, setMp] = useState<MassProperties | SheetProperties | null>(null)
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  // Re-fetch is manual — the button avoids a tessellation on every edit.
  useEffect(() => { setMp(null); setErr(null) }, [doc?.derived.solid_hash])
  if (!doc) return null

  const compute = async () => {
    setLoading(true); setErr(null)
    try { setMp(await api.massProperties(doc.id)) }
    catch (e) { setErr(e instanceof Error ? e.message : 'Unavailable') }
    finally { setLoading(false) }
  }

  return (
    <div className="pt-3 border-t border-cad-border">
      <div className="flex items-center justify-between mb-2">
        <h3 className="sec-head">Mass properties</h3>
        <button onClick={compute} disabled={loading} className="flex items-center gap-1 text-[10px] text-cad-text hover:text-cad-accent-hot disabled:opacity-50">
          {loading ? <Loader2 size={11} className="animate-spin" /> : <Scale size={11} />} Compute
        </button>
      </div>
      {err && <p className="text-[10px] text-cad-danger">{err}</p>}
      {/* Switched on `kind`, never on whether `mass_kg` is there. A sheet has
          no volume, no mass and no centre of mass — printing zeros for them
          reads as "no material assigned", which is the wrong thing to
          conclude and the reason those keys are absent rather than zero. */}
      {mp?.kind === 'sheet' && (
        <dl className="space-y-1 text-[10px] text-cad-text">
          <Row label="Body" value="sheet — no enclosed volume" />
          <Row label="Surface" value={`${mp.surface_area_cm2.toFixed(1)} cm²`} />
          <Row label="Centroid" value={mp.centroid_mm.map((v) => v.toFixed(1)).join(', ') + ' mm'} />
          <p className="text-[9px] text-cad-text/50 leading-snug pt-1">{mp.note}</p>
        </dl>
      )}
      {mp?.kind === 'solid' && (
        <dl className="space-y-1 text-[10px] text-cad-text">
          <Row label="Mass" value={`${mp.mass_kg.toFixed(4)} kg`} />
          <Row label="Volume" value={`${mp.volume_cm3.toFixed(2)} cm³`} />
          <Row label="Surface" value={`${mp.surface_area_cm2.toFixed(1)} cm²`} />
          <Row label="COG" value={mp.center_of_mass_mm.map((v) => v.toFixed(1)).join(', ') + ' mm'} />
        </dl>
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

      {/* A sketch as its own feature: no body, and every extrude/revolve/loft
          reading it moves when it is edited. */}
      {node.type === 'SketchFeature' && (
        <>
          <button
            onClick={() =>
              openSketch({
                featureId: node.id!,
                sketch: p.sketch as never,
                plane: String(p.plane ?? 'xy'),
                depth: 0,
                offset: Number(p.offset ?? 0),
                standalone: true,
              })
            }
            className="w-full flex items-center justify-center gap-1.5 py-2 rounded-sm bg-cad-accent text-cad-bg text-xs font-semibold hover:bg-cad-accent-hot transition-colors"
          >
            Edit sketch
          </button>
          <div className="text-[11px] text-cad-text/70 leading-relaxed">
            {(() => {
              const solved = (p.solved ?? {}) as Record<string, unknown>
              const status = String(solved.status ?? 'unsolved').replace(/_/g, '-')
              const dof = Number(solved.dof ?? 0)
              return `${status}${dof > 0 ? ` · ${dof} degree(s) of freedom` : ''}`
            })()}
          </div>
          <div className="text-[11px] text-cad-text/50 leading-relaxed">
            This sketch builds no body of its own. Give its id to an extrude, revolve
            or loft; editing it moves every one of them.
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

      {node.type === 'RevolveFeature' && (
        <>
          <div>
            <h3 className="sec-head mb-2">Revolve axis</h3>
            <select value={String(p.axis ?? 'z')} onChange={(e) => commit({ axis: e.target.value })}
              className="w-full px-2 py-1.5 rounded-sm bg-cad-bg border border-cad-border text-cad-text-heading text-xs focus:border-cad-accent outline-none">
              <option value="x">X</option><option value="y">Y</option><option value="z">Z</option>
            </select>
          </div>
          <div>
            <h3 className="sec-head mb-2">Angle</h3>
            <NumberField label="" value={p.angle as number} scale={1} suffix="°" onCommit={(a) => commit({ angle: a })} />
          </div>
          <Vec3Field label="Axis center" value={p.center as Vec} scale={MM} suffix="mm" onCommit={(v) => commit({ center: v })} />
        </>
      )}

      {node.type === 'MirrorFeature' && (
        <>
          <div>
            <h3 className="sec-head mb-2">Mirror plane</h3>
            <select value={String(p.plane ?? 'yz')} onChange={(e) => commit({ plane: e.target.value })}
              className="w-full px-2 py-1.5 rounded-sm bg-cad-bg border border-cad-border text-cad-text-heading text-xs focus:border-cad-accent outline-none">
              <option value="yz">YZ (mirror left/right)</option>
              <option value="xz">XZ (mirror front/back)</option>
              <option value="xy">XY (mirror up/down)</option>
            </select>
          </div>
          <ScalarRow label="Plane offset" value={p.offset as number} onCommit={(o) => commit({ offset: o })} />
        </>
      )}

      {node.type === 'SweepFeature' && (
        <ScalarRow label="Tube radius" value={p.radius as number} onCommit={(r) => commit({ radius: r })} />
      )}

      {node.type === 'LoftFeature' && (
        <>
          <div>
            <h3 className="sec-head mb-2">Loft plane</h3>
            <select value={String(p.plane ?? 'xy')} onChange={(e) => commit({ plane: e.target.value })}
              className="w-full px-2 py-1.5 rounded-sm bg-cad-bg border border-cad-border text-cad-text-heading text-xs focus:border-cad-accent outline-none">
              <option value="xy">XY</option><option value="xz">XZ</option><option value="yz">YZ</option>
            </select>
          </div>
          <div className="grid grid-cols-2 gap-2">
            <NumberField label="Bottom" value={p.offset_a as number} scale={MM} suffix="mm" onCommit={(o) => commit({ offset_a: o })} />
            <NumberField label="Top" value={p.offset_b as number} scale={MM} suffix="mm" onCommit={(o) => commit({ offset_b: o })} />
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

import { useState } from 'react'
import {
  Box, Circle, Cylinder, Cone, Combine, Trash2, Plus, Minus,
  Blend, Triangle, Shell, CircleDot, Grid3x3, Move3d,
  Eye, EyeOff, ChevronUp, ChevronDown, PencilRuler,
} from 'lucide-react'
import { useDocumentStore } from '../../store/useDocumentStore'
import {
  flatten,
  isSuppressed,
  isBoolean,
  type PrimitiveType,
  type BooleanOp,
  type ModifierType,
} from '../../util/featureTree'

const SHAPE_OF: Record<PrimitiveType, string> = {
  BoxFeature: 'box',
  CylinderFeature: 'cylinder',
  SphereFeature: 'sphere',
  ConeFeature: 'cone',
}
const MODIFIER_OF: Record<ModifierType, string> = {
  FilletFeature: 'fillet',
  ChamferFeature: 'chamfer',
  ShellFeature: 'shell',
  HoleFeature: 'hole',
  PatternFeature: 'pattern',
  TransformFeature: 'transform',
}
import type { FeatureNode } from '../../../../shared/types'

const TYPE_ICON: Record<string, typeof Box> = {
  BoxFeature: Box,
  SphereFeature: Circle,
  CylinderFeature: Cylinder,
  ConeFeature: Cone,
  BooleanFeature: Combine,
  FilletFeature: Blend,
  ChamferFeature: Triangle,
  ShellFeature: Shell,
  HoleFeature: CircleDot,
  PatternFeature: Grid3x3,
  TransformFeature: Move3d,
  ExtrudeFeature: PencilRuler,
}

const MODIFIERS: { type: ModifierType; label: string; icon: typeof Box }[] = [
  { type: 'FilletFeature', label: 'Fillet', icon: Blend },
  { type: 'ChamferFeature', label: 'Chamfer', icon: Triangle },
  { type: 'ShellFeature', label: 'Shell', icon: Shell },
  { type: 'HoleFeature', label: 'Hole', icon: CircleDot },
  { type: 'PatternFeature', label: 'Pattern', icon: Grid3x3 },
  { type: 'TransformFeature', label: 'Move', icon: Move3d },
]

const ADD_TYPES: { type: PrimitiveType; label: string; icon: typeof Box }[] = [
  { type: 'BoxFeature', label: 'Box', icon: Box },
  { type: 'CylinderFeature', label: 'Cyl', icon: Cylinder },
  { type: 'SphereFeature', label: 'Sph', icon: Circle },
  { type: 'ConeFeature', label: 'Cone', icon: Cone },
]

function nodeLabel(node: FeatureNode): string {
  if (isBoolean(node)) {
    const op = String(node.params.operation ?? 'union')
    return `${node.name} · ${op}`
  }
  return node.name
}

export function FeatureTreePanel() {
  const { doc, selectedFeatureId, select, runOp, rename, busy,
    sketchPick, startSketchPick, cancelSketchPick, pickSketchOnFace } = useDocumentStore()
  const [op, setOp] = useState<BooleanOp>('union')
  const [editingName, setEditingName] = useState(false)

  if (!doc) {
    return (
      <aside className="w-72 border-r border-cad-border bg-cad-panel flex flex-col shrink-0 z-10">
        <div className="h-11 border-b border-cad-border flex items-center px-4">
          <span className="eyebrow text-cad-text/70">History</span>
        </div>
        <div className="flex-1 flex items-center justify-center p-4 text-center">
          <p className="text-[11px] text-cad-text/60">No part open</p>
        </div>
      </aside>
    )
  }

  const nodes = flatten(doc.feature_tree)

  // All geometry mutations flow through the shared operation API (runOp).
  const addFeature = async (type: PrimitiveType) => {
    const r = await runOp('add_primitive', { shape: SHAPE_OF[type], operation: op })
    if (r?.feature_id) select(r.feature_id as string)
  }

  const deleteFeature = async (id: string) => {
    await runOp('delete_feature', { feature_id: id })
    if (selectedFeatureId === id) select(null)
  }

  const modify = async (type: ModifierType) => {
    const r = await runOp('add_modifier', { modifier: MODIFIER_OF[type] })
    if (r?.feature_id) select(r.feature_id as string)
  }

  const toggleSuppress = (id: string, cur: boolean) => runOp('set_feature_suppressed', { feature_id: id, suppressed: !cur })
  const move = (id: string, dir: 'up' | 'down') => runOp('reorder_feature', { feature_id: id, direction: dir })
  const rootId = doc.feature_tree.root?.id

  return (
    <aside className="w-72 border-r border-cad-border bg-cad-panel flex flex-col shrink-0 z-10">
      {/* document header */}
      <div className="h-11 border-b border-cad-border flex items-center gap-2.5 px-4">
        <span aria-hidden className="w-0.5 h-3.5 bg-cad-accent rounded-full" />
        <div className="leading-none min-w-0 flex-1">
          <span className="eyebrow text-cad-text/70">{doc.provenance.replace('_', '-')}</span>
          {editingName ? (
            <input
              autoFocus
              defaultValue={doc.name}
              onBlur={(e) => {
                rename(e.target.value.trim() || doc.name)
                setEditingName(false)
              }}
              onKeyDown={(e) => e.key === 'Enter' && (e.target as HTMLInputElement).blur()}
              className="w-full bg-transparent font-display text-sm font-semibold text-cad-text-heading leading-none mt-1 outline-none border-b border-cad-accent"
            />
          ) : (
            <h2
              onClick={() => setEditingName(true)}
              title="Rename"
              className="font-display text-sm font-semibold text-cad-text-heading leading-none mt-1 truncate cursor-text"
            >
              {doc.name}
            </h2>
          )}
        </div>
      </div>

      {/* feature list */}
      <div className="flex-1 overflow-y-auto py-2">
        {nodes.map(({ node, depth }) => {
          const Icon = TYPE_ICON[node.type] ?? Box
          const active = node.id === selectedFeatureId
          const suppressed = isSuppressed(node)
          const id = node.id ?? ''
          const isRoot = node.id === rootId
          return (
            <div
              key={node.id}
              className="group flex items-center pr-1.5"
              style={{ paddingLeft: 12 + depth * 14 }}
            >
              <button
                onClick={() =>
                  sketchPick && node.id
                    ? pickSketchOnFace(node.id, 'top')
                    : select(node.id ?? null)
                }
                title={sketchPick ? 'Sketch on this feature’s top face' : undefined}
                className={`flex-1 flex items-center gap-2 py-1.5 min-w-0 text-left transition-colors ${
                  sketchPick
                    ? 'text-cad-accent-hot hover:text-cad-accent cursor-copy'
                    : suppressed ? 'opacity-40' : active ? 'text-cad-accent-hot' : 'text-cad-text hover:text-cad-text-heading'
                }`}
              >
                <Icon size={14} className={active && !suppressed ? 'text-cad-accent' : 'text-cad-text/70'} />
                <span className={`text-xs truncate ${suppressed ? 'line-through' : ''}`}>{nodeLabel(node)}</span>
              </button>
              <div className="flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition">
                {!isRoot && (
                  <>
                    <button onClick={() => move(id, 'up')} title="Move up" className="text-cad-text hover:text-cad-text-heading">
                      <ChevronUp size={12} />
                    </button>
                    <button onClick={() => move(id, 'down')} title="Move down" className="text-cad-text hover:text-cad-text-heading">
                      <ChevronDown size={12} />
                    </button>
                  </>
                )}
                <button
                  onClick={() => toggleSuppress(id, suppressed)}
                  title={suppressed ? 'Unsuppress' : 'Suppress'}
                  className={suppressed ? 'text-cad-accent-hot' : 'text-cad-text hover:text-cad-text-heading'}
                >
                  {suppressed ? <EyeOff size={12} /> : <Eye size={12} />}
                </button>
                {nodes.length > 1 && !isRoot && (
                  <button onClick={() => deleteFeature(id)} className="text-cad-text hover:text-cad-danger transition" title="Delete feature">
                    <Trash2 size={12} />
                  </button>
                )}
              </div>
            </div>
          )
        })}
      </div>

      {/* add / cut controls */}
      <div className="border-t border-cad-border p-3 space-y-2">
        <div className="flex items-center gap-1 p-0.5 rounded-sm bg-cad-bg border border-cad-border">
          {(
            [
              { id: 'union', label: 'Add', icon: Plus },
              { id: 'subtract', label: 'Cut', icon: Minus },
            ] as const
          ).map((o) => (
            <button
              key={o.id}
              onClick={() => setOp(o.id)}
              className={`flex-1 flex items-center justify-center gap-1 py-1 rounded-[3px] text-[11px] font-medium transition-colors ${
                op === o.id ? 'bg-cad-accent/15 text-cad-accent-hot' : 'text-cad-text hover:text-cad-text-heading'
              }`}
            >
              <o.icon size={12} />
              {o.label}
            </button>
          ))}
        </div>
        <div className="grid grid-cols-4 gap-1">
          {ADD_TYPES.map((t) => (
            <button
              key={t.type}
              onClick={() => addFeature(t.type)}
              disabled={busy}
              title={`${op === 'subtract' ? 'Cut' : 'Add'} ${t.label}`}
              className="flex flex-col items-center gap-1 py-2 rounded-sm border border-cad-border text-cad-text hover:text-cad-text-heading hover:border-stress-void transition disabled:opacity-40"
            >
              <t.icon size={15} />
              <span className="text-[9px]">{t.label}</span>
            </button>
          ))}
        </div>
        {sketchPick ? (
          <button
            onClick={cancelSketchPick}
            className="w-full flex items-center justify-center gap-1.5 py-1.5 rounded-sm border border-cad-accent/60 bg-cad-accent/10 text-cad-accent-hot transition"
          >
            <PencilRuler size={13} /> <span className="text-[10px]">Pick a face or plane… (cancel)</span>
          </button>
        ) : (
          <button
            onClick={startSketchPick}
            disabled={busy}
            title="Start a sketch — then click a face in the viewport, a feature here, or a base plane"
            className="w-full flex items-center justify-center gap-1.5 py-1.5 rounded-sm border border-cad-accent/40 text-cad-accent-hot hover:bg-cad-accent/10 transition disabled:opacity-40"
          >
            <PencilRuler size={13} /> <span className="text-[10px]">New Sketch…</span>
          </button>
        )}

        <div>
          <span className="eyebrow text-cad-text/60 block mb-1.5">Modify body</span>
          <div className="grid grid-cols-6 gap-1">
            {MODIFIERS.map((m) => (
              <button
                key={m.type}
                onClick={() => modify(m.type)}
                disabled={busy}
                title={m.label}
                className="flex items-center justify-center py-1.5 rounded-sm border border-cad-border text-cad-text hover:text-cad-accent-hot hover:border-cad-accent/50 transition disabled:opacity-40"
              >
                <m.icon size={14} />
              </button>
            ))}
          </div>
        </div>
      </div>
    </aside>
  )
}

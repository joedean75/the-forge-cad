import { useState } from 'react'
import {
  Box, Circle, Cylinder, Cone, Combine, Trash2,
  Blend, Triangle, Shell, CircleDot, Grid3x3, Move3d,
  Eye, EyeOff, ChevronUp, ChevronDown, PencilRuler, Upload,
} from 'lucide-react'
import { useDocumentStore } from '../../store/useDocumentStore'
import { flatten, isSuppressed, isBoolean } from '../../util/featureTree'
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
  ImportFeature: Upload,
}

function nodeLabel(node: FeatureNode): string {
  if (isBoolean(node)) {
    const op = String(node.params.operation ?? 'union')
    return `${node.name} · ${op}`
  }
  return node.name
}

export function FeatureTreePanel() {
  const { doc, selectedFeatureId, select, runOp, rename,
    sketchPick, pickSketchOnFace } = useDocumentStore()
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

  // Mutations flow through the shared operation API; creation tools live in
  // the ToolRibbon above the viewport — this panel is pure history.
  const deleteFeature = async (id: string) => {
    await runOp('delete_feature', { feature_id: id })
    if (selectedFeatureId === id) select(null)
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

      {/* sketch-pick state rides the ribbon; here we only hint the tree's role in it */}
      {sketchPick && (
        <div className="border-t border-cad-border px-3 py-2 flex items-center gap-1.5 text-cad-accent-hot">
          <PencilRuler size={12} className="shrink-0" />
          <span className="text-[10px]">Click a feature to sketch on its top face — Esc cancels</span>
        </div>
      )}
    </aside>
  )
}

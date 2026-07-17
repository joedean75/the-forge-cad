import { useEffect, useState } from 'react'
import {
  Box, Circle, Cylinder, Cone, Plus, Minus,
  Blend, Triangle, Shell, CircleDot, Grid3x3, Move3d, PencilRuler,
} from 'lucide-react'
import { useDocumentStore } from '../store/useDocumentStore'
import { flatten, type BooleanOp, type ModifierType, type PrimitiveType } from '../util/featureTree'

/**
 * The CAD tool ribbon: creation and modification tools where every mainstream
 * suite puts them — a horizontal, grouped, labeled bar above the viewport, in
 * build order (Sketch → Create → Modify). The feature tree stays pure history.
 * All geometry mutations flow through the shared operation API (runOp).
 */

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

const PRIMITIVES: { type: PrimitiveType; label: string; icon: typeof Box; hint: string }[] = [
  { type: 'BoxFeature', label: 'Box', icon: Box, hint: 'block' },
  { type: 'CylinderFeature', label: 'Cylinder', icon: Cylinder, hint: 'rod / bore' },
  { type: 'SphereFeature', label: 'Sphere', icon: Circle, hint: 'ball' },
  { type: 'ConeFeature', label: 'Cone', icon: Cone, hint: 'taper' },
]

const MODIFIERS: { type: ModifierType; label: string; icon: typeof Box; hint: string }[] = [
  { type: 'FilletFeature', label: 'Fillet', icon: Blend, hint: 'round the edges' },
  { type: 'ChamferFeature', label: 'Chamfer', icon: Triangle, hint: 'bevel the edges' },
  { type: 'ShellFeature', label: 'Shell', icon: Shell, hint: 'hollow to a wall thickness' },
  { type: 'HoleFeature', label: 'Hole', icon: CircleDot, hint: 'drill a round hole' },
  { type: 'PatternFeature', label: 'Pattern', icon: Grid3x3, hint: 'repeat linearly or around an axis' },
  { type: 'TransformFeature', label: 'Move', icon: Move3d, hint: 'translate / rotate / scale' },
]

function Tool({
  icon: Icon,
  label,
  title,
  onClick,
  disabled,
  active,
}: {
  icon: typeof Box
  label: string
  title: string
  onClick: () => void
  disabled?: boolean
  active?: boolean
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      title={title}
      className={`flex flex-col items-center justify-center gap-1 min-w-[52px] px-1.5 py-1.5 rounded-sm transition-colors outline-none focus-visible:ring-2 focus-visible:ring-cad-accent/60 disabled:opacity-40 ${
        active
          ? 'bg-cad-accent/12 text-cad-accent-hot'
          : 'text-cad-text hover:text-cad-text-heading hover:bg-cad-raised'
      }`}
    >
      <Icon size={17} strokeWidth={active ? 2.1 : 1.7} />
      <span className="text-[9px] font-medium leading-none">{label}</span>
    </button>
  )
}

function Group({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col justify-center self-stretch py-1">
      <div className="flex items-center gap-0.5">{children}</div>
      <span className="eyebrow text-cad-text/40 text-center leading-none mt-1">{label}</span>
    </div>
  )
}

function Divider() {
  return <span aria-hidden className="w-px self-stretch my-2.5 bg-cad-border" />
}

export function ToolRibbon() {
  const { doc, mode, busy, runOp, select,
    sketchPick, startSketchPick, cancelSketchPick } = useDocumentStore()
  const [op, setOp] = useState<BooleanOp>('union')

  // CAD-standard keys: Esc cancels a sketch pick; Delete removes the selected
  // feature (never while typing in a field).
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && useDocumentStore.getState().sketchPick) {
        useDocumentStore.getState().cancelSketchPick()
        return
      }
      if (e.key !== 'Delete') return
      const t = e.target as HTMLElement | null
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return
      const st = useDocumentStore.getState()
      const id = st.selectedFeatureId
      if (!st.doc || !id || st.busy) return
      const nodes = flatten(st.doc.feature_tree)
      if (id === st.doc.feature_tree.root?.id || nodes.length <= 1) return
      st.runOp('delete_feature', { feature_id: id })
      st.select(null)
    }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [])

  if (!doc || mode !== 'model') return null

  const addPrimitive = async (type: PrimitiveType) => {
    const r = await runOp('add_primitive', { shape: SHAPE_OF[type], operation: op })
    if (r?.feature_id) select(r.feature_id as string)
  }

  const addModifier = async (type: ModifierType) => {
    const r = await runOp('add_modifier', { modifier: MODIFIER_OF[type] })
    if (r?.feature_id) select(r.feature_id as string)
  }

  const cut = op === 'subtract'

  return (
    <div className="border-b border-cad-border bg-cad-panel flex items-stretch gap-2 px-3 shrink-0 z-10">
      <Group label="Sketch">
        <Tool
          icon={PencilRuler}
          label={sketchPick ? 'Esc to cancel' : 'New sketch'}
          title={
            sketchPick
              ? 'Picking — click a face in the viewport or a feature in the tree (Esc to cancel)'
              : 'New sketch — pick a face or plane, draw a profile, extrude it'
          }
          onClick={sketchPick ? cancelSketchPick : startSketchPick}
          disabled={busy}
          active={sketchPick}
        />
      </Group>

      <Divider />

      <Group label="Create">
        {/* Whether the next primitive adds material or cuts it away. */}
        <div
          className="flex flex-col items-stretch gap-0.5 mr-1.5 p-0.5 rounded-sm bg-cad-bg border border-cad-border"
          title="Whether the next shape adds material or cuts it away"
        >
          {(
            [
              { id: 'union', label: 'Add', icon: Plus },
              { id: 'subtract', label: 'Cut', icon: Minus },
            ] as const
          ).map((o) => (
            <button
              key={o.id}
              onClick={() => setOp(o.id)}
              className={`flex items-center gap-1 px-1.5 py-0.5 rounded-[3px] text-[10px] font-medium transition-colors outline-none focus-visible:ring-2 focus-visible:ring-cad-accent/60 ${
                op === o.id ? 'bg-cad-accent/15 text-cad-accent-hot' : 'text-cad-text hover:text-cad-text-heading'
              }`}
            >
              <o.icon size={11} />
              {o.label}
            </button>
          ))}
        </div>
        {PRIMITIVES.map((t) => (
          <Tool
            key={t.type}
            icon={t.icon}
            label={t.label}
            title={`${cut ? 'Cut a' : 'Add a'} ${t.label.toLowerCase()} (${t.hint})`}
            onClick={() => addPrimitive(t.type)}
            disabled={busy}
          />
        ))}
      </Group>

      <Divider />

      <Group label="Modify">
        {MODIFIERS.map((m) => (
          <Tool
            key={m.type}
            icon={m.icon}
            label={m.label}
            title={`${m.label} — ${m.hint}`}
            onClick={() => addModifier(m.type)}
            disabled={busy}
          />
        ))}
      </Group>
    </div>
  )
}

import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  Box, Circle, Cylinder, Cone, Plus, Minus,
  Blend, Triangle, Shell, CircleDot, Grid3x3, Move3d, PencilRuler,
  Rotate3d, FlipHorizontal2, Spline, Layers, ArrowUpFromLine,
  Sheet, CornerUpRight, CornerDownRight, UnfoldHorizontal,
} from 'lucide-react'
import { api } from '../api/client'
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

/** Flat-pattern readout returned by the `flat_pattern` operation. */
type FlatPattern = {
  blank_size_mm: { x: number; y: number }
  thickness: number
  k_factor: number
  bend_count: number
  bends: {
    edge: string
    angle: number
    radius: number
    bend_allowance_mm: number
    distance_from_edge_mm: number
  }[]
}

export function ToolRibbon() {
  const { doc, mode, busy, runOp, select, selectedFeatureId,
    sketchPick, startSketchPick, cancelSketchPick } = useDocumentStore()
  const [op, setOp] = useState<BooleanOp>('union')
  const [flatPattern, setFlatPattern] = useState<FlatPattern | null>(null)

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

  const rootId = doc.feature_tree.root?.id
  // Pattern/Transform act on the *selected* feature (e.g. replicate one hole);
  // the others wrap the whole body. A selection that is the root = whole body.
  const targetable = selectedFeatureId && selectedFeatureId !== rootId ? selectedFeatureId : undefined

  const addModifier = async (type: ModifierType) => {
    const usesTarget = type === 'PatternFeature' || type === 'TransformFeature'
    const args: Record<string, unknown> = { modifier: MODIFIER_OF[type] }
    if (usesTarget && targetable) args.target = targetable
    const r = await runOp('add_modifier', args)
    if (r?.feature_id) select(r.feature_id as string)
  }

  // A selected SketchFeature is an outline waiting for a consumer, so the form
  // tools build from it instead of from a default profile. That is the whole
  // point of a sketch being a feature: one outline, several bodies.
  const selectedSketch = selectedFeatureId && flatten(doc.feature_tree)
    .some((f) => f.node.id === selectedFeatureId && f.node.type === 'SketchFeature')
    ? selectedFeatureId : undefined

  // Advanced forms (revolve/sweep/loft) add a default body; mirror wraps the
  // body or the selected feature. Params are then edited in the inspector.
  const addForm = async (opName: string, extra: Record<string, unknown> = {}) => {
    const r = await runOp(opName, { operation: op, ...extra })
    if (r?.feature_id) select(r.feature_id as string)
  }
  const fromSketch = (key: string) => (selectedSketch ? { [key]: selectedSketch } : {})
  const addMirror = async () => {
    const r = await runOp('add_mirror', targetable ? { target: targetable } : {})
    if (r?.feature_id) select(r.feature_id as string)
  }

  // Sheet metal: a base flange is a normal body, then flanges bend off its
  // edges. `hasSheetMetal` gates the flange/unfold tools so they only offer
  // themselves once there is something to bend.
  const hasSheetMetal = doc
    ? flatten(doc.feature_tree).some((f) => f.node.type === 'SheetMetalFeature')
    : false
  const addSheetMetal = async () => {
    const r = await runOp('add_sheet_metal', { operation: op })
    if (r?.feature_id) select(r.feature_id as string)
  }
  const addFlange = async (edge: string, angle: number) => {
    const r = await runOp('add_flange', { edge, angle })
    if (r?.feature_id) select(r.feature_id as string)
  }
  const unfold = async () => {
    const r = await runOp('flat_pattern', {})
    if (r) setFlatPattern(r as FlatPattern)
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

      <Group label="Forms">
        <Tool icon={ArrowUpFromLine} label="Extrude"
          title={selectedSketch
            ? 'Extrude the selected sketch — it keeps reading that sketch, so editing it moves this body'
            : 'Extrude a default rectangle (select a sketch first to extrude that instead)'}
          onClick={() => addForm('add_extrude', fromSketch('sketch'))} disabled={busy} />
        <Tool icon={Rotate3d} label="Revolve"
          title={selectedSketch
            ? 'Revolve the selected sketch about an axis'
            : 'Revolve a profile about an axis — shaft, wheel, pulley, bottle'}
          onClick={() => addForm('add_revolve', fromSketch('sketch'))} disabled={busy} />
        <Tool icon={Spline} label="Sweep" title="Sweep a round profile along a path — pipe, tube, wire, rail"
          onClick={() => addForm('add_sweep')} disabled={busy} />
        <Tool icon={Layers} label="Loft"
          title={selectedSketch
            ? 'Loft from the selected sketch to a default section'
            : 'Blend between two profiles — adapters, tapers, funnels'}
          onClick={() => addForm('add_loft', fromSketch('sketch_a'))} disabled={busy} />
        <Tool icon={FlipHorizontal2} label="Mirror"
          title={targetable ? 'Mirror the selected feature across a plane' : 'Mirror the whole body across a plane (select a feature to mirror just that)'}
          onClick={addMirror} disabled={busy} />
      </Group>

      <Divider />

      <Group label="Sheet metal">
        <Tool icon={Sheet} label="Base flange"
          title="Start a sheet-metal part — a flat plate of constant thickness to bend flanges from"
          onClick={addSheetMetal} disabled={busy} />
        <Tool icon={CornerUpRight} label="Flange up"
          title={hasSheetMetal ? 'Bend a 90° flange up off the +X edge' : 'Add a base flange first'}
          onClick={() => addFlange('x+', 90)} disabled={busy || !hasSheetMetal} />
        <Tool icon={CornerDownRight} label="Flange down"
          title={hasSheetMetal ? 'Bend a 90° flange down off the +X edge' : 'Add a base flange first'}
          onClick={() => addFlange('x+', -90)} disabled={busy || !hasSheetMetal} />
        <Tool icon={UnfoldHorizontal} label="Unfold"
          title={hasSheetMetal ? 'Flat pattern: developed blank size, bend allowances and bend lines' : 'Add a base flange first'}
          onClick={unfold} disabled={busy || !hasSheetMetal} />
      </Group>

      <Divider />

      <Group label="Modify">
        {MODIFIERS.map((m) => {
          const targets = (m.type === 'PatternFeature' || m.type === 'TransformFeature') && targetable
          return (
            <Tool
              key={m.type}
              icon={m.icon}
              label={m.label}
              title={targets ? `${m.label} the selected feature — ${m.hint}`
                : m.type === 'PatternFeature' || m.type === 'TransformFeature'
                  ? `${m.label} — select a feature first to ${m.label.toLowerCase()} just that one, else the whole body`
                  : `${m.label} — ${m.hint}`}
              onClick={() => addModifier(m.type)}
              disabled={busy}
            />
          )
        })}
      </Group>

      {flatPattern && (
        <FlatPatternReadout
          pattern={flatPattern}
          onClose={() => setFlatPattern(null)}
          onExport={() => doc && api.downloadFlatPattern(doc.id, doc.name)}
        />
      )}
    </div>
  )
}

/** Brake setup sheet: the blank to cut and where each bend line lands. */
function FlatPatternReadout({ pattern, onClose, onExport }: {
  pattern: FlatPattern
  onClose: () => void
  onExport: () => void
}) {
  // Portalled to <body>: the ribbon sits inside the editor's layout, whose
  // panels form their own stacking contexts, so a panel rendered in place paints
  // *underneath* the inspector no matter how high its z-index goes.
  return createPortal(
    <div className="fixed right-3 top-28 z-50 w-72 rounded-sm border border-cad-border bg-cad-panel shadow-lg">
      <div className="flex items-center justify-between px-3 py-2 border-b border-cad-border">
        <h3 className="sec-head">Flat pattern</h3>
        <button onClick={onClose} className="text-cad-text hover:text-cad-text-heading text-xs px-1">✕</button>
      </div>
      <div className="px-3 py-2 space-y-2 text-xs">
        <div className="flex justify-between">
          <span className="text-cad-text">Blank</span>
          <span className="text-cad-text-heading font-mono">
            {pattern.blank_size_mm.x.toFixed(2)} × {pattern.blank_size_mm.y.toFixed(2)} mm
          </span>
        </div>
        <div className="flex justify-between">
          <span className="text-cad-text">Thickness · K</span>
          <span className="text-cad-text-heading font-mono">
            {(pattern.thickness * 1000).toFixed(2)} mm · {pattern.k_factor}
          </span>
        </div>
        {pattern.bend_count === 0 ? (
          <p className="text-cad-text opacity-70">No bends — this blank is already flat.</p>
        ) : (
          <div className="pt-1 border-t border-cad-border">
            <div className="grid grid-cols-4 gap-1 text-[10px] uppercase tracking-wide text-cad-text pb-1">
              <span>Edge</span><span>Angle</span><span>Allow</span><span>At</span>
            </div>
            {pattern.bends.map((b, i) => (
              <div key={i} className="grid grid-cols-4 gap-1 font-mono text-[11px] text-cad-text-heading">
                <span>{b.edge}</span>
                <span>{b.angle}°</span>
                <span>{b.bend_allowance_mm.toFixed(2)}</span>
                <span>{b.distance_from_edge_mm.toFixed(1)}</span>
              </div>
            ))}
          </div>
        )}
        <button
          onClick={onExport}
          className="w-full mt-1 px-2 py-1.5 rounded-sm border border-cad-border text-cad-text-heading hover:border-cad-accent hover:text-cad-accent transition-colors text-[11px]"
          title="Blank outline on the CUT layer, bend lines on BEND — ready for a laser or waterjet"
        >
          Export DXF
        </button>
      </div>
    </div>,
    document.body,
  )
}

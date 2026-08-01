import { useEffect, useMemo, useRef, useState } from 'react'
import {
  X, MousePointer2, Minus as LineIcon, Square, Circle as CircleIcon, Loader2,
  MoveHorizontal, MoveVertical, Ruler, GitCompareArrows, Spline, Equal, Dot, Anchor, Trash2,
  Eye, EyeOff, Scissors, MoveRight, Copy, FlipHorizontal2, Target, Triangle, PenLine,
} from 'lucide-react'
import { api } from '../../api/client'
import { useDocumentStore } from '../../store/useDocumentStore'
import { findNode } from '../../util/featureTree'
import { PartBackdrop, useMeshTriangles, faceToPlaneOffset } from './PartBackdrop'
import { SketchDimensions } from './SketchDimensions'
import * as edit from './sketchEdit'
import type { Arc, Cir, Con, Entities, Ln, Pt } from './sketchEdit'
import type { SketchSolution, SketchStatus } from '../../../../shared/types'

type Sel = { kind: 'point' | 'line' | 'circle' | 'arc'; id: string }
type Tool = 'select' | 'line' | 'rect' | 'circle' | 'arc' | 'trim' | 'extend'

let _idc = 0
const nid = (p: string) => `${p}${(_idc += 1)}`

const TOOLS: { id: Tool; label: string; icon: typeof Square }[] = [
  { id: 'select', label: 'Select', icon: MousePointer2 },
  { id: 'line', label: 'Line', icon: LineIcon },
  { id: 'rect', label: 'Rect', icon: Square },
  { id: 'circle', label: 'Circle', icon: CircleIcon },
  { id: 'arc', label: 'Arc', icon: Spline },
  { id: 'trim', label: 'Trim', icon: Scissors },
  { id: 'extend', label: 'Extend', icon: MoveRight },
]

// What the active tool expects next — shown as a hint so drawing is self-explanatory.
const TOOL_HINT: Record<Tool, string> = {
  select: 'Click an entity to select · Shift-click for multiple · then apply a constraint',
  line: 'Click to place each point · Esc to finish the chain',
  rect: 'Click two opposite corners',
  circle: 'Click the center, then a point on the rim',
  arc: 'Click center, then start point, then end point',
  trim: 'Click the span you want gone — it is cut back to the nearest crossings',
  extend: 'Click near the end of a line to lengthen it to the next thing it meets',
}

/**
 * The solver's verdict, in the colour it deserves.
 *
 * `under_constrained` is deliberately not a warning colour: a sketch is
 * under-constrained for most of the time anyone is drawing one, and treating
 * the normal state as an error trains people to ignore the indicator.
 */
const STATUS_STYLE: Record<SketchStatus, { label: string; className: string }> = {
  well_constrained: { label: 'fully constrained', className: 'text-cad-ok border-cad-ok/40 bg-cad-ok/10' },
  under_constrained: { label: 'under-constrained', className: 'text-cad-text border-cad-border bg-cad-bg' },
  over_constrained: { label: 'over-constrained', className: 'text-cad-warn border-cad-warn/40 bg-cad-warn/10' },
  conflicting: { label: 'conflicting', className: 'text-cad-danger border-cad-danger/40 bg-cad-danger/10' },
}

// Tessellate an arc (mm, y-up) from p_from to p_to about center for SVG preview.
function arcPolyline(
  center: { x: number; y: number }, from: { x: number; y: number }, to: { x: number; y: number }, ccw: boolean, seg = 32,
): string {
  const a0 = Math.atan2(from.y - center.y, from.x - center.x)
  const a1 = Math.atan2(to.y - center.y, to.x - center.x)
  const r0 = Math.hypot(from.x - center.x, from.y - center.y)
  const r1 = Math.hypot(to.x - center.x, to.y - center.y)
  let da = a1 - a0
  if (ccw) { while (da <= 1e-9) da += 2 * Math.PI } else { while (da >= -1e-9) da -= 2 * Math.PI }
  const out: string[] = []
  for (let i = 0; i <= seg; i++) {
    const t = i / seg
    const ang = a0 + da * t
    const r = r0 + (r1 - r0) * t
    out.push(`${center.x + r * Math.cos(ang)},${-(center.y + r * Math.sin(ang))}`)
  }
  return out.join(' ')
}

export function SketchCanvas() {
  const { runOp, sketchOpen, sketchEdit, sketchPreset, closeSketch, startSketchPick, doc } = useDocumentStore()
  const svgRef = useRef<SVGSVGElement>(null)
  const [pts, setPts] = useState<Pt[]>([])
  const [lines, setLines] = useState<Ln[]>([])
  const [arcs, setArcs] = useState<Arc[]>([])
  const [circles, setCircles] = useState<Cir[]>([])
  const [cons, setCons] = useState<Con[]>([])
  const [tool, setTool] = useState<Tool>('rect')
  const [sel, setSel] = useState<Sel[]>([])
  const [draft, setDraft] = useState<{ kind: Tool; pointId?: string; centerId?: string; aId?: string; x: number; y: number } | null>(null)
  const [cursor, setCursor] = useState<{ x: number; y: number } | null>(null)
  const [plane, setPlane] = useState('xy')
  const [depth, setDepth] = useState(10)
  const [offset, setOffset] = useState(0)
  // Symbolic sketch-on-face reference: when set, plane/offset are resolved from
  // the referenced feature's live geometry at regen (survives upstream edits).
  const [onFace, setOnFace] = useState<{ featureId: string; face: string } | null>(null)
  const [op, setOp] = useState<'union' | 'subtract'>('union')
  const [status, setStatus] = useState('')
  const [solution, setSolution] = useState<SketchSolution | null>(null)
  const [busy, setBusy] = useState(false)
  const [showPart, setShowPart] = useState(true)
  // Newly drawn geometry is construction while this is on: a centreline or a
  // bolt circle constrains the sketch without being part of the outline.
  const [drawConstruction, setDrawConstruction] = useState(false)
  // A standalone SketchFeature rather than a sketch-and-extrude in one: several
  // features can then read it, and editing it moves all of them.
  const [asFeature, setAsFeature] = useState(false)
  // Editing a standalone sketch: it has no depth, and the consumers own the body.
  const standalone = Boolean(sketchEdit?.standalone) || (asFeature && !sketchEdit)

  // The existing body, shown behind the sketch so you can see what you're drawing
  // on. Only fetched/parsed while the sketch is actually open.
  const partTris = useMeshTriangles(sketchOpen && showPart ? doc?.derived.mesh_url : null)
  // Display plane + section position: when sketching on a face, mirror the
  // backend datum resolution from that feature's live geometry; else the free plane.
  const backdrop = useMemo<{ plane: 'xy' | 'xz' | 'yz'; sectionMm: number; originMm: [number, number] }>(() => {
    if (onFace && doc) {
      const node = findNode(doc.feature_tree, onFace.featureId)
      const r = node ? faceToPlaneOffset(node, onFace.face) : null
      if (r) return { plane: r.plane, sectionMm: r.offsetMm, originMm: r.originMm }
    }
    return { plane: plane as 'xy' | 'xz' | 'yz', sectionMm: offset, originMm: [0, 0] }
  }, [onFace, doc, plane, offset])

  // Load an existing sketch when opened in edit mode; blank otherwise (metres -> mm).
  useEffect(() => {
    if (!sketchOpen) return
    if (sketchEdit) {
      const s = sketchEdit.sketch as {
        points: { id: string; x: number; y: number }[]
        lines?: Ln[]
        arcs?: Arc[]
        circles?: { id: string; center: string; radius: number; construction?: boolean }[]
        constraints?: Con[]
      }
      setPts(s.points.map((p) => ({ id: p.id, x: p.x * 1000, y: p.y * 1000 })))
      setLines((s.lines ?? []).map((l) => ({ ...l })))
      setArcs((s.arcs ?? []).map((a) => ({ ...a, ccw: a.ccw ?? true })))
      setCircles((s.circles ?? []).map((c) => ({ ...c, radius: c.radius * 1000 })))
      setCons((s.constraints ?? []).map((c) => {
        const o: Con = { ...c }
        if (c.type === 'distance' || c.type === 'radius' || c.type === 'arc_radius') o.value = (c.value as number) * 1000
        if (c.type === 'fixed') {
          if (c.x != null) o.x = (c.x as number) * 1000
          if (c.y != null) o.y = (c.y as number) * 1000
        }
        return o
      }))
      setPlane(sketchEdit.plane)
      setDepth(sketchEdit.depth * 1000)
      setOffset(sketchEdit.offset * 1000)
    } else {
      // New sketch: seed the plane/face from the pick step (viewport face, a tree
      // feature, or a base plane); default to a free XY plane.
      setPts([]); setLines([]); setArcs([]); setCircles([]); setCons([])
      setPlane(sketchPreset?.plane ?? 'xy'); setDepth(10); setOffset(0)
    }
    setOnFace(sketchEdit ? null : (sketchPreset?.onFace ?? null))
    setSel([]); setDraft(null); setStatus(''); setSolution(null)
  }, [sketchOpen, sketchEdit, sketchPreset])

  if (!sketchOpen) return null

  const TH = 4 // snap / hit threshold (mm)
  const getPt = (id: string) => pts.find((p) => p.id === id)!
  const dist = edit.dist

  const entities = (): Entities => ({ pts, lines, arcs, circles, cons })
  const apply = (next: Entities) => {
    setPts(next.pts); setLines(next.lines); setArcs(next.arcs)
    setCircles(next.circles); setCons(next.cons); setSel([])
  }

  const toWorld = (e: React.MouseEvent): { x: number; y: number } => {
    const svg = svgRef.current!
    const pt = svg.createSVGPoint()
    pt.x = e.clientX
    pt.y = e.clientY
    const p = pt.matrixTransform(svg.getScreenCTM()!.inverse())
    return { x: p.x, y: -p.y } // SVG y-down -> sketch y-up
  }

  const addPoint = (x: number, y: number) => {
    const id = nid('p')
    setPts((s) => [...s, { id, x, y }])
    return id
  }
  const snapPoint = (w: { x: number; y: number }) => {
    let best: string | null = null
    let bd = TH
    for (const p of pts) {
      const d = dist(w, p)
      if (d < bd) { bd = d; best = p.id }
    }
    return best
  }

  const hitTest = (w: { x: number; y: number }): Sel | null => {
    for (const p of pts) if (dist(w, p) < TH) return { kind: 'point', id: p.id }
    for (const c of circles) if (Math.abs(dist(w, getPt(c.center)) - c.radius) < TH) return { kind: 'circle', id: c.id }
    for (const ar of arcs) if (Math.abs(dist(w, getPt(ar.center)) - dist(getPt(ar.a), getPt(ar.center))) < TH) return { kind: 'arc', id: ar.id }
    for (const l of lines) {
      const a = getPt(l.a), b = getPt(l.b)
      const L = dist(a, b) || 1e-6
      const t = Math.max(0, Math.min(1, ((w.x - a.x) * (b.x - a.x) + (w.y - a.y) * (b.y - a.y)) / (L * L)))
      const px = a.x + t * (b.x - a.x), py = a.y + t * (b.y - a.y)
      if (Math.hypot(w.x - px, w.y - py) < TH) return { kind: 'line', id: l.id }
    }
    return null
  }

  const buildRect = (x1: number, y1: number, x2: number, y2: number) => {
    const p0 = nid('p'), p1 = nid('p'), p2 = nid('p'), p3 = nid('p')
    const newPts: Pt[] = [
      { id: p0, x: x1, y: y1 }, { id: p1, x: x2, y: y1 },
      { id: p2, x: x2, y: y2 }, { id: p3, x: x1, y: y2 },
    ]
    const l0 = nid('l'), l1 = nid('l'), l2 = nid('l'), l3 = nid('l')
    const mark = drawConstruction ? { construction: true } : {}
    const newLines: Ln[] = [
      { id: l0, a: p0, b: p1, ...mark }, { id: l1, a: p1, b: p2, ...mark },
      { id: l2, a: p2, b: p3, ...mark }, { id: l3, a: p3, b: p0, ...mark },
    ]
    setPts((s) => [...s, ...newPts])
    setLines((s) => [...s, ...newLines])
    setCons((s) => [
      ...s,
      { id: nid('c'), type: 'horizontal', line: l0 },
      { id: nid('c'), type: 'horizontal', line: l2 },
      { id: nid('c'), type: 'vertical', line: l1 },
      { id: nid('c'), type: 'vertical', line: l3 },
    ])
  }

  const canvasClick = (e: React.MouseEvent) => {
    const w = toWorld(e)
    const mark = drawConstruction ? { construction: true } : {}
    if (tool === 'select') {
      const hit = hitTest(w)
      if (!hit) { setSel([]); return }
      setSel((s) =>
        e.shiftKey
          ? s.some((x) => x.id === hit.id) ? s.filter((x) => x.id !== hit.id) : [...s, hit]
          : [hit],
      )
      return
    }
    if (tool === 'trim') {
      const hit = hitTest(w)
      if (!hit || hit.kind === 'point') { setStatus('Click a line or circle to trim.'); return }
      const next = edit.trim(entities(), hit, w)
      if (next === entities()) setStatus('Nothing crosses that — trim needs something to cut back to.')
      else { apply(next); setStatus('') }
      return
    }
    if (tool === 'extend') {
      const hit = hitTest(w)
      if (!hit || hit.kind !== 'line') { setStatus('Extend works on a line.'); return }
      const next = edit.extend(entities(), hit.id, w)
      if (next === entities()) setStatus('Nothing beyond that end to extend to.')
      else { apply(next); setStatus('') }
      return
    }
    if (tool === 'line') {
      const id = snapPoint(w) ?? addPoint(w.x, w.y)
      if (!draft) setDraft({ kind: 'line', pointId: id, x: w.x, y: w.y })
      else {
        setLines((s) => [...s, { id: nid('l'), a: draft.pointId!, b: id, ...mark }])
        setDraft({ kind: 'line', pointId: id, x: w.x, y: w.y })
      }
    } else if (tool === 'rect') {
      if (!draft) setDraft({ kind: 'rect', x: w.x, y: w.y })
      else { buildRect(draft.x, draft.y, w.x, w.y); setDraft(null) }
    } else if (tool === 'circle') {
      if (!draft) {
        const id = snapPoint(w) ?? addPoint(w.x, w.y)
        setDraft({ kind: 'circle', pointId: id, x: w.x, y: w.y })
      } else {
        setCircles((s) => [...s, { id: nid('c'), center: draft.pointId!, radius: dist(draft, w), ...mark }])
        setDraft(null)
      }
    } else if (tool === 'arc') {
      // 3-click arc: center, then start point, then end point.
      if (!draft) {
        const id = snapPoint(w) ?? addPoint(w.x, w.y)
        setDraft({ kind: 'arc', centerId: id, x: w.x, y: w.y })
      } else if (!draft.aId) {
        const id = snapPoint(w) ?? addPoint(w.x, w.y)
        setDraft({ ...draft, aId: id })
      } else {
        const bId = snapPoint(w) ?? addPoint(w.x, w.y)
        const c = getPt(draft.centerId!), a = getPt(draft.aId)
        // CCW when the signed area of (a - c) x (b - c) is positive → picks the minor arc.
        const cross = (a.x - c.x) * (w.y - c.y) - (a.y - c.y) * (w.x - c.x)
        setArcs((s) => [...s, { id: nid('a'), a: draft.aId!, b: bId, center: draft.centerId!, ccw: cross > 0, ...mark }])
        setDraft(null)
      }
    }
  }

  const addConstraint = (type: string) => {
    const p = sel.filter((s) => s.kind === 'point').map((s) => s.id)
    const l = sel.filter((s) => s.kind === 'line').map((s) => s.id)
    const c = sel.filter((s) => s.kind === 'circle').map((s) => s.id)
    const ar = sel.filter((s) => s.kind === 'arc').map((s) => s.id)
    const curve = [...c, ...ar]   // tangent/concentric/equal_radius take either
    let con: Con | null = null
    if ((type === 'horizontal' || type === 'vertical') && l[0]) con = { id: nid('c'), type, line: l[0] }
    else if (type === 'coincident' && p.length >= 2) con = { id: nid('c'), type, a: p[0], b: p[1] }
    else if ((type === 'parallel' || type === 'perpendicular' || type === 'equal_length') && l.length >= 2) {
      con = { id: nid('c'), type, line1: l[0], line2: l[1] }
    } else if (type === 'fixed' && p[0]) { const pt = getPt(p[0]); con = { id: nid('c'), type, point: p[0], x: pt.x, y: pt.y } }
    else if (type === 'midpoint' && p[0] && l[0]) con = { id: nid('c'), type, point: p[0], line: l[0] }
    else if (type === 'point_on_line' && p[0] && l[0]) con = { id: nid('c'), type, point: p[0], line: l[0] }
    else if (type === 'point_on_circle' && p[0] && curve[0]) con = { id: nid('c'), type, point: p[0], circle: curve[0] }
    else if (type === 'concentric' && curve.length >= 2) con = { id: nid('c'), type, a: curve[0], b: curve[1] }
    else if (type === 'equal_radius' && curve.length >= 2) con = { id: nid('c'), type, a: curve[0], b: curve[1] }
    else if (type === 'symmetric' && p.length >= 2 && l[0]) con = { id: nid('c'), type, a: p[0], b: p[1], line: l[0] }
    else if (type === 'tangent' && l[0] && curve[0]) con = { id: nid('c'), type, line: l[0], arc: curve[0] }
    else if (type === 'tangent' && curve.length >= 2) con = { id: nid('c'), type, a: curve[0], b: curve[1] }
    else if (type === 'angle' && l.length >= 2) {
      const v = prompt('Angle between the two lines (degrees):', '90')
      if (v == null) return
      con = { id: nid('c'), type, line1: l[0], line2: l[1], value: parseFloat(v) }
    } else if (type === 'distance' && p.length >= 2) {
      const v = prompt('Distance (mm):', dist(getPt(p[0]), getPt(p[1])).toFixed(1))
      if (v == null) return
      con = { id: nid('c'), type, a: p[0], b: p[1], value: parseFloat(v) }
    } else if (type === 'radius' && c[0]) {
      const cir = circles.find((x) => x.id === c[0])!
      const v = prompt('Radius (mm):', cir.radius.toFixed(1))
      if (v == null) return
      con = { id: nid('c'), type, circle: c[0], value: parseFloat(v) }
    } else if (type === 'arc_radius' && ar[0]) {
      const a = arcs.find((x) => x.id === ar[0])!
      const cur = dist(getPt(a.a), getPt(a.center))
      const v = prompt('Arc radius (mm):', cur.toFixed(1))
      if (v == null) return
      con = { id: nid('c'), type, arc: ar[0], value: parseFloat(v) }
    }
    if (!con) { setStatus(`Select the right entities for ${type}.`); return }
    setCons((s) => [...s, con!])
    setStatus('')
  }

  /** Edit a dimension in place, by clicking its figure on the canvas. */
  const editDimension = (constraintId: string) => {
    const con = cons.find((c) => c.id === constraintId)
    if (!con) return
    const unit = con.type === 'angle' ? 'degrees' : 'mm'
    const next = prompt(`${con.type} (${unit}):`, String(con.value ?? ''))
    if (next == null) return
    const value = parseFloat(next)
    if (!Number.isFinite(value)) return
    setCons((s) => s.map((c) => (c.id === constraintId ? { ...c, value } : c)))
  }

  const toggleConstruction = () => {
    if (!sel.length) { setDrawConstruction((v) => !v); return }
    const ids = new Set(sel.map((s) => s.id))
    const flip = <T extends { id: string; construction?: boolean }>(x: T): T =>
      (ids.has(x.id) ? { ...x, construction: !x.construction } : x)
    setLines((s) => s.map(flip)); setArcs((s) => s.map(flip)); setCircles((s) => s.map(flip))
  }

  const doMirror = () => {
    const axis = sel.find((s) => s.kind === 'line')
    const rest = sel.filter((s) => s.id !== axis?.id)
    if (!axis || !rest.length) { setStatus('Select the entities to mirror plus the line to mirror across.'); return }
    apply(edit.mirror(entities(), rest, axis.id))
    setStatus('')
  }

  const doOffset = () => {
    if (!sel.length) { setStatus('Select lines or circles to offset.'); return }
    const v = prompt('Offset distance (mm, negative for the other side):', '5')
    if (v == null) return
    const distance = parseFloat(v)
    if (!Number.isFinite(distance) || distance === 0) return
    apply(edit.offset(entities(), sel, distance))
    setStatus('')
  }

  // Build the solver spec (metres).
  const buildSpec = () => ({
    points: pts.map((p) => ({ id: p.id, x: p.x / 1000, y: p.y / 1000 })),
    lines,
    arcs,
    circles: circles.map((c) => ({ ...c, radius: c.radius / 1000 })),
    constraints: cons.map((c) => {
      const o: Con = { ...c }
      if (c.type === 'distance' || c.type === 'radius' || c.type === 'arc_radius') o.value = (c.value as number) / 1000
      if (c.type === 'fixed') {
        if (c.x != null) o.x = (c.x as number) / 1000
        if (c.y != null) o.y = (c.y as number) / 1000
      }
      return o
    }),
  })

  const solve = async () => {
    setBusy(true)
    try {
      const res = await api.solveSketch(buildSpec() as never)
      setPts((s) => s.map((p) => (res.points[p.id] ? { ...p, x: res.points[p.id][0] * 1000, y: res.points[p.id][1] * 1000 } : p)))
      setCircles((s) => s.map((c) => (res.radii[c.id] != null ? { ...c, radius: res.radii[c.id] * 1000 } : c)))
      setSolution(res)
      setStatus(res.status === 'conflicting' ? res.message : '')
    } catch (e) {
      setSolution(null)
      setStatus(e instanceof Error ? e.message : 'Solve failed')
    } finally {
      setBusy(false)
    }
  }

  const finish = async () => {
    const drawn = lines.filter((l) => !l.construction).length
      + arcs.filter((a) => !a.construction).length
      + circles.filter((c) => !c.construction).length
    if (drawn < 1) { setStatus('Draw something that is not construction geometry.'); return }
    setBusy(true)
    try {
      const base: Record<string, unknown> = { sketch: buildSpec(), plane, offset: offset / 1000 }
      if (onFace) base.on_face = onFace
      if (sketchEdit) {
        await runOp('update_sketch', standalone
          ? { feature_id: sketchEdit.featureId, ...base }
          : { feature_id: sketchEdit.featureId, ...base, depth: depth / 1000 })
      }
      else if (asFeature) await runOp('create_sketch', base)
      else await runOp('add_sketch', { ...base, depth: depth / 1000, operation: op })
      closeSketch()
    } catch (e) {
      setStatus(e instanceof Error ? e.message : 'Extrude failed')
    } finally {
      setBusy(false)
    }
  }

  const reset = () => {
    setPts([]); setLines([]); setArcs([]); setCircles([]); setCons([])
    setSel([]); setDraft(null); setStatus(''); setSolution(null)
  }

  const isSel = (id: string) => sel.some((s) => s.id === id)
  const sx = (x: number) => x
  const sy = (y: number) => -y
  // Construction geometry reads as a thin dashed cyan line: present, clearly
  // not part of the outline.
  const stroke = (id: string, construction?: boolean) =>
    (isSel(id) ? '#ffb86b' : construction ? '#5c8fb8' : '#c2ccd8')
  const dash = (construction?: boolean) => (construction ? '3 2' : undefined)
  const conflicting = new Set(
    (solution?.conflicts ?? []).map((c) => (typeof c.index === 'number' ? cons[c.index]?.id : undefined)),
  )
  const verdict = solution ? STATUS_STYLE[solution.status] : null

  return (
    <div className="fixed inset-0 z-50 flex flex-col bg-cad-bg" onKeyDown={(e) => e.key === 'Escape' && setDraft(null)}>
      {/* header */}
      <div className="h-12 border-b border-cad-border bg-cad-panel flex items-center justify-between px-4 shrink-0">
        <div className="flex items-center gap-2.5">
          <span aria-hidden className="w-0.5 h-3.5 bg-cad-accent rounded-full" />
          <div className="leading-none">
            <span className="eyebrow text-cad-text/70">Sketch</span>
            <h2 className="font-display text-sm font-semibold text-cad-text-heading leading-none mt-1">{sketchEdit ? 'Edit Sketch' : '2D Constrained Sketch'}</h2>
          </div>
          {/* The solver's verdict. `converged` alone means nothing — an
              unconstrained sketch converges trivially — so this reports the
              status and the degrees of freedom still free. */}
          {verdict && (
            <span
              title={solution!.message}
              className={`tabular ml-2 px-2 py-0.5 rounded-full border text-[10px] ${verdict.className}`}
            >
              {verdict.label}
              {solution!.dof > 0 && ` · ${solution!.dof} dof`}
              {solution!.redundant > 0 && ` · ${solution!.redundant} redundant`}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <span className="tabular text-[10px] text-cad-text/70 mr-1 max-w-[280px] truncate" title={status}>{status}</span>
          {/* Sketch plane: either attached to a feature's face (picked in the
              viewport / feature tree — tracks that face through later edits) or a
              free base plane. The face is chosen by clicking, not a dropdown. */}
          {onFace && doc ? (
            <div className="flex items-center gap-1.5 h-7 pl-2 pr-1 rounded-sm border border-cad-accent/50 bg-cad-accent/10" title="Sketch attached to this face — it follows the feature when resized">
              <span className="text-[10px] text-cad-accent-hot whitespace-nowrap max-w-[160px] truncate">
                on {findNode(doc.feature_tree, onFace.featureId)?.name ?? 'part'} · {onFace.face}
              </span>
              <button onClick={() => { closeSketch(); startSketchPick() }} title="Pick a different face or plane" className="p-0.5 rounded-sm text-cad-text hover:text-cad-accent-hot"><MousePointer2 size={12} /></button>
              <button onClick={() => setOnFace(null)} title="Detach — sketch on a free plane instead" className="p-0.5 rounded-sm text-cad-text hover:text-cad-danger"><X size={12} /></button>
            </div>
          ) : (
            <label className="flex items-center gap-1 text-[10px] text-cad-text" title="Base sketch plane">
              plane
              <select value={plane} onChange={(e) => setPlane(e.target.value)} className="tabular px-2 py-1 rounded-sm bg-cad-bg border border-cad-border text-cad-text-heading text-[11px] outline-none">
                <option value="xy">XY</option><option value="xz">XZ</option><option value="yz">YZ</option>
              </select>
            </label>
          )}
          <button
            onClick={() => setShowPart((v) => !v)}
            disabled={!doc?.derived.mesh_url}
            title={showPart ? 'Hide the existing part' : 'Show the existing part behind the sketch'}
            className={`flex items-center gap-1 px-2 py-1.5 rounded-sm border text-[10px] transition-colors disabled:opacity-40 ${showPart ? 'border-cad-accent/50 text-cad-accent-hot bg-cad-accent/10' : 'border-cad-border text-cad-text hover:text-cad-text-heading'}`}
          >
            {showPart ? <Eye size={12} /> : <EyeOff size={12} />} Part
          </button>
          <label className={`flex items-center gap-1 text-[10px] text-cad-text ${standalone ? 'opacity-40' : ''}`}>
            depth
            <input type="number" value={depth} min={0.5} step={0.5} disabled={standalone} onChange={(e) => setDepth(parseFloat(e.target.value) || 0)} className="tabular w-14 px-1.5 py-1 rounded-sm bg-cad-bg border border-cad-border text-cad-text-heading text-[11px] outline-none disabled:opacity-40" />mm
          </label>
          <label className={`flex items-center gap-1 text-[10px] text-cad-text ${onFace ? 'opacity-40' : ''}`} title="Plane offset along its normal (ignored when on-face is set)">
            offset
            <input type="number" value={offset} disabled={!!onFace} step={0.5} onChange={(e) => setOffset(parseFloat(e.target.value) || 0)} className="tabular w-14 px-1.5 py-1 rounded-sm bg-cad-bg border border-cad-border text-cad-text-heading text-[11px] outline-none disabled:opacity-40" />mm
          </label>
          {!sketchEdit && (
            <>
              {/* A sketch on its own can be read by several features, and
                  editing it moves all of them; the combined form extrudes once. */}
              <button
                onClick={() => setAsFeature((v) => !v)}
                title={asFeature
                  ? 'Saving as a reusable sketch feature — extrude or revolve it afterwards'
                  : 'Save as a reusable sketch feature instead of extruding now'}
                className={`flex items-center gap-1 px-2 py-1.5 rounded-sm border text-[10px] transition-colors ${asFeature ? 'border-cad-accent/50 text-cad-accent-hot bg-cad-accent/10' : 'border-cad-border text-cad-text hover:text-cad-text-heading'}`}
              >
                <PenLine size={12} /> Sketch only
              </button>
              {!asFeature && (
                <div className="flex items-center gap-0.5 p-0.5 rounded-sm bg-cad-bg border border-cad-border">
                  {(['union', 'subtract'] as const).map((o) => (
                    <button key={o} onClick={() => setOp(o)} className={`px-2 py-1 rounded-[3px] text-[10px] ${op === o ? 'bg-cad-accent/15 text-cad-accent-hot' : 'text-cad-text'}`}>{o === 'union' ? 'Add' : 'Cut'}</button>
                  ))}
                </div>
              )}
            </>
          )}
          <button onClick={solve} disabled={busy} className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-sm border border-cad-border text-cad-text hover:text-cad-text-heading text-[11px] disabled:opacity-40">
            {busy ? <Loader2 size={12} className="animate-spin" /> : <Spline size={12} />} Solve
          </button>
          <button onClick={finish} disabled={busy} className="flex items-center gap-1.5 px-3 py-1.5 rounded-sm bg-cad-accent text-cad-bg font-semibold text-[11px] hover:bg-cad-accent-hot disabled:opacity-40">
            {sketchEdit ? 'Update' : asFeature ? 'Save sketch' : 'Finish → Extrude'}
          </button>
          <button onClick={closeSketch} className="p-1.5 rounded-sm text-cad-text hover:text-cad-text-heading hover:bg-cad-raised"><X size={16} /></button>
        </div>
      </div>

      <div className="flex flex-1 min-h-0">
        {/* tools */}
        <div className="w-14 border-r border-cad-border bg-cad-panel flex flex-col items-center py-3 gap-1.5 shrink-0">
          {TOOLS.map((t) => (
            <button key={t.id} onClick={() => { setTool(t.id); setDraft(null) }} title={t.label}
              className={`w-10 h-10 rounded-lg flex flex-col items-center justify-center gap-0.5 transition ${tool === t.id ? 'bg-cad-accent/10 text-cad-accent' : 'text-cad-text hover:bg-cad-raised'}`}>
              <t.icon size={16} /><span className="text-[7px]">{t.label}</span>
            </button>
          ))}
          <div className="w-8 h-px bg-cad-border my-1" />
          <button onClick={doOffset} title="Offset the selection by a distance"
            className="w-10 h-10 rounded-lg flex flex-col items-center justify-center gap-0.5 text-cad-text hover:bg-cad-raised">
            <Copy size={16} /><span className="text-[7px]">Offset</span>
          </button>
          <button onClick={doMirror} title="Mirror the selection across a selected line"
            className="w-10 h-10 rounded-lg flex flex-col items-center justify-center gap-0.5 text-cad-text hover:bg-cad-raised">
            <FlipHorizontal2 size={16} /><span className="text-[7px]">Mirror</span>
          </button>
          <button onClick={toggleConstruction}
            title={sel.length
              ? 'Toggle the selection between real and construction geometry'
              : 'Draw construction geometry — constrained, but not part of the outline'}
            className={`w-10 h-10 rounded-lg flex flex-col items-center justify-center gap-0.5 transition ${drawConstruction && !sel.length ? 'bg-cad-accent/10 text-cad-accent' : 'text-cad-text hover:bg-cad-raised'}`}>
            <PenLine size={16} /><span className="text-[7px]">Constr</span>
          </button>
        </div>

        {/* canvas */}
        <div className="flex-1 min-w-0 relative" style={{ background: 'radial-gradient(120% 120% at 50% 30%, #1a1f27 0%, var(--color-cad-bg) 60%)' }}>
          <svg ref={svgRef} className="w-full h-full" viewBox="-130 -95 260 190" preserveAspectRatio="xMidYMid meet"
            onClick={canvasClick} onMouseMove={(e) => setCursor(toWorld(e))} style={{ cursor: tool === 'select' ? 'default' : 'crosshair' }}>
            {/* grid */}
            {Array.from({ length: 27 }, (_, i) => -130 + i * 10).map((g) => (
              <line key={'gx' + g} x1={g} y1={-95} x2={g} y2={95} stroke="#20252e" strokeWidth={g % 50 === 0 ? 0.4 : 0.2} />
            ))}
            {Array.from({ length: 20 }, (_, i) => -95 + i * 10).map((g) => (
              <line key={'gy' + g} x1={-130} y1={g} x2={130} y2={g} stroke="#20252e" strokeWidth={g % 50 === 0 ? 0.4 : 0.2} />
            ))}
            <line x1={-130} y1={0} x2={130} y2={0} stroke="#2c333e" strokeWidth={0.5} />
            <line x1={0} y1={-95} x2={0} y2={95} stroke="#2c333e" strokeWidth={0.5} />

            {/* existing part: dim silhouette + bright section where the plane cuts */}
            {showPart && <PartBackdrop positions={partTris} plane={backdrop.plane} sectionMm={backdrop.sectionMm} originMm={backdrop.originMm} />}

            {/* circles */}
            {circles.map((c) => {
              const ct = getPt(c.center)
              return <circle key={c.id} cx={sx(ct.x)} cy={sy(ct.y)} r={c.radius} fill="none"
                stroke={stroke(c.id, c.construction)} strokeDasharray={dash(c.construction)} strokeWidth={1} />
            })}
            {/* lines */}
            {lines.map((l) => {
              const a = getPt(l.a), b = getPt(l.b)
              return <line key={l.id} x1={sx(a.x)} y1={sy(a.y)} x2={sx(b.x)} y2={sy(b.y)}
                stroke={stroke(l.id, l.construction)} strokeDasharray={dash(l.construction)}
                strokeWidth={l.construction ? 0.7 : 1.4} strokeLinecap="round" />
            })}
            {/* arcs */}
            {arcs.map((ar) => (
              <polyline key={ar.id} points={arcPolyline(getPt(ar.center), getPt(ar.a), getPt(ar.b), ar.ccw)} fill="none"
                stroke={stroke(ar.id, ar.construction)} strokeDasharray={dash(ar.construction)}
                strokeWidth={ar.construction ? 0.7 : 1.4} strokeLinecap="round" strokeLinejoin="round" />
            ))}

            {/* dimensions, drawn where they apply */}
            <SketchDimensions
              cons={cons}
              look={{
                pt: (id) => pts.find((p) => p.id === id),
                line: (id) => lines.find((l) => l.id === id),
                arc: (id) => arcs.find((a) => a.id === id),
                circle: (id) => circles.find((c) => c.id === id),
              }}
              onEdit={editDimension}
            />

            {/* draft preview */}
            {draft && cursor && draft.kind === 'line' && <line x1={sx(draft.x)} y1={sy(draft.y)} x2={sx(cursor.x)} y2={sy(cursor.y)} stroke="#e8683c" strokeWidth={1} strokeDasharray="2 2" />}
            {draft && cursor && draft.kind === 'rect' && <rect x={sx(Math.min(draft.x, cursor.x))} y={sy(Math.max(draft.y, cursor.y))} width={Math.abs(cursor.x - draft.x)} height={Math.abs(cursor.y - draft.y)} fill="none" stroke="#e8683c" strokeWidth={1} strokeDasharray="2 2" />}
            {draft && cursor && draft.kind === 'circle' && <circle cx={sx(draft.x)} cy={sy(draft.y)} r={dist(draft, cursor)} fill="none" stroke="#e8683c" strokeWidth={1} strokeDasharray="2 2" />}
            {/* arc draft: after center+start clicks, preview the sweep to the cursor */}
            {draft && cursor && draft.kind === 'arc' && draft.aId && (() => {
              const c = getPt(draft.centerId!), a = getPt(draft.aId!)
              const cross = (a.x - c.x) * (cursor.y - c.y) - (a.y - c.y) * (cursor.x - c.x)
              return <polyline points={arcPolyline(c, a, cursor, cross > 0)} fill="none" stroke="#e8683c" strokeWidth={1} strokeDasharray="2 2" />
            })()}
            {draft && cursor && draft.kind === 'arc' && !draft.aId && <line x1={sx(draft.x)} y1={sy(draft.y)} x2={sx(cursor.x)} y2={sy(cursor.y)} stroke="#e8683c" strokeWidth={0.6} strokeDasharray="1 2" />}
            {/* points */}
            {pts.map((p) => <circle key={p.id} cx={sx(p.x)} cy={sy(p.y)} r={isSel(p.id) ? 2 : 1.4} fill={isSel(p.id) ? '#ffb86b' : '#7d8ba0'} />)}
          </svg>

          {/* active-tool hint */}
          <div className="absolute top-3 left-1/2 -translate-x-1/2 text-[10px] text-cad-text/80 bg-cad-panel/80 backdrop-blur-sm px-3 py-1 rounded-full border border-cad-border pointer-events-none">
            {TOOL_HINT[tool]}
          </div>

          {/* backdrop legend */}
          {showPart && partTris && (
            <div className="absolute top-3 right-3 flex flex-col gap-1 text-[9px] text-cad-text/70 bg-cad-panel/70 px-2 py-1.5 rounded-sm border border-cad-border pointer-events-none">
              <span className="flex items-center gap-1.5"><span className="w-3 h-2 rounded-sm" style={{ background: 'rgba(125,139,160,0.4)' }} /> existing part</span>
              <span className="flex items-center gap-1.5"><span className="w-3 h-0.5" style={{ background: '#5cc9a7' }} /> section at this plane</span>
            </div>
          )}

          <div className="tabular absolute bottom-3 left-3 text-[10px] text-cad-text/70 bg-cad-panel/70 px-2 py-1 rounded-sm border border-cad-border">
            {cursor ? `${cursor.x.toFixed(1)}, ${cursor.y.toFixed(1)} mm` : 'grid: 10 mm'} · {pts.length} pts · {lines.length} lines · {arcs.length} arcs · {circles.length} circles
          </div>
        </div>

        {/* constraints */}
        <div className="w-60 border-l border-cad-border bg-cad-panel flex flex-col shrink-0">
          <div className="h-10 border-b border-cad-border flex items-center px-4"><span className="eyebrow text-cad-text/70">Constraints</span></div>
          <div className="p-3 grid grid-cols-3 gap-1.5">
            {([
              ['horizontal', 'Horiz', MoveHorizontal], ['vertical', 'Vert', MoveVertical],
              ['coincident', 'Coincid', Dot], ['distance', 'Dist', Ruler],
              ['parallel', 'Parallel', GitCompareArrows], ['perpendicular', 'Perp', GitCompareArrows],
              ['equal_length', 'Equal', Equal], ['radius', 'Radius', CircleIcon],
              ['arc_radius', 'Arc R', Spline], ['fixed', 'Fix', Anchor],
              ['tangent', 'Tangent', Spline], ['concentric', 'Concen', Target],
              ['equal_radius', 'Eq R', Equal], ['symmetric', 'Symm', FlipHorizontal2],
              ['angle', 'Angle', Triangle], ['midpoint', 'Midpt', Dot],
              ['point_on_line', 'On line', Dot], ['point_on_circle', 'On circ', Target],
            ] as const).map(([t, label, Icon]) => (
              <button key={t} onClick={() => addConstraint(t)} title={label}
                className="flex flex-col items-center gap-1 py-2 rounded-sm border border-cad-border text-cad-text hover:text-cad-accent-hot hover:border-cad-accent/50 transition">
                <Icon size={14} /><span className="text-[8px]">{label}</span>
              </button>
            ))}
          </div>
          <div className="px-3 pb-1 text-[10px] text-cad-text/50">Select entities (Select tool, Shift for multi), then click a constraint. Dimensions are edited on the canvas.</div>
          <div className="flex-1 overflow-y-auto px-3 py-2 space-y-1">
            {cons.map((c) => (
              <div key={c.id} className={`group flex items-center justify-between text-[10px] py-0.5 ${conflicting.has(c.id) ? 'text-cad-danger' : 'text-cad-text'}`}>
                <span className="tabular truncate" title={conflicting.has(c.id) ? 'This constraint cannot hold with the others' : undefined}>
                  {c.type}{c.value != null ? ` ${(c.value as number).toFixed(1)}${c.type === 'angle' ? '°' : 'mm'}` : ''}
                </span>
                <button onClick={() => setCons((s) => s.filter((x) => x.id !== c.id))} className="opacity-0 group-hover:opacity-100 text-cad-text hover:text-cad-danger"><Trash2 size={10} /></button>
              </div>
            ))}
          </div>
          {solution && solution.status !== 'well_constrained' && (
            <div className="px-3 pb-2 text-[10px] text-cad-text/70 border-t border-cad-border pt-2">{solution.message}</div>
          )}
          <button onClick={reset} className="m-3 py-1.5 rounded-sm border border-cad-border text-cad-text hover:text-cad-danger text-[11px]">Clear sketch</button>
        </div>
      </div>
    </div>
  )
}

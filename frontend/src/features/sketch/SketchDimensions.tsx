import type { Con, Pt, Arc, Cir, Ln, XY } from './sketchEdit'

/**
 * Dimensions drawn on the sketch, not listed beside it.
 *
 * A dimensional constraint is the thing you read a drawing by, and a sidebar
 * row saying `distance 60.0mm` cannot tell you *which* 60 mm. These render in
 * place, and clicking the figure edits the value — which is how every drafting
 * tool has worked for thirty years.
 *
 * Everything here is in sketch millimetres, y-up; SVG y is flipped on the way
 * out, exactly as the canvas does.
 */

const TEXT = '#8fd7f4'
const RULE = 'rgba(143,215,244,0.55)'

type Lookup = {
  pt: (id: string) => Pt | undefined
  line: (id: string) => Ln | undefined
  arc: (id: string) => Arc | undefined
  circle: (id: string) => Cir | undefined
}

const sy = (y: number) => -y

/**
 * Every length here is multiplied by `k`, the millimetres-per-260-of-view.
 * Without it a dimension's witness lines and type are drawn in model
 * millimetres, so zooming in makes the annotation grow with the part — the one
 * thing an annotation must not do.
 */
function Figure({ at, text, k, onEdit, dim }: {
  at: XY; text: string; k: number; onEdit?: () => void; dim?: boolean
}) {
  return (
    <text
      x={at.x} y={sy(at.y)} fill={dim ? 'rgba(143,215,244,0.5)' : TEXT}
      fontSize={4 * k} textAnchor="middle" dominantBaseline="middle"
      style={{ cursor: onEdit ? 'pointer' : 'default', userSelect: 'none' }}
      onClick={onEdit && ((e) => { e.stopPropagation(); onEdit() })}
    >
      {text}
    </text>
  )
}

/** A filled arrowhead at `at`, pointing along `(dx, dy)`. */
function Arrow({ at, dx, dy, k }: { at: XY; dx: number; dy: number; k: number }) {
  const len = Math.hypot(dx, dy) || 1
  const ux = dx / len, uy = dy / len
  const size = 2.2 * k
  const tail = { x: at.x - ux * size, y: at.y - uy * size }
  const wing = { x: -uy * size * 0.36, y: ux * size * 0.36 }
  return (
    <polygon
      points={[
        `${at.x},${sy(at.y)}`,
        `${tail.x + wing.x},${sy(tail.y + wing.y)}`,
        `${tail.x - wing.x},${sy(tail.y - wing.y)}`,
      ].join(' ')}
      fill={RULE}
    />
  )
}

function Distance({ a, b, value, k, onEdit }: {
  a: Pt; b: Pt; value: number; k: number; onEdit: () => void
}) {
  const dx = b.x - a.x, dy = b.y - a.y
  const length = Math.hypot(dx, dy) || 1
  // Witness lines stand off perpendicular to the measured span, so the figure
  // never sits on top of the geometry it measures.
  const ox = (-dy / length) * 6 * k, oy = (dx / length) * 6 * k
  const a2 = { x: a.x + ox, y: a.y + oy }
  const b2 = { x: b.x + ox, y: b.y + oy }
  return (
    <g>
      <line x1={a.x} y1={sy(a.y)} x2={a2.x + ox * 0.3} y2={sy(a2.y + oy * 0.3)}
        stroke={RULE} strokeWidth={0.3 * k} />
      <line x1={b.x} y1={sy(b.y)} x2={b2.x + ox * 0.3} y2={sy(b2.y + oy * 0.3)}
        stroke={RULE} strokeWidth={0.3 * k} />
      <line x1={a2.x} y1={sy(a2.y)} x2={b2.x} y2={sy(b2.y)} stroke={RULE} strokeWidth={0.4 * k} />
      <Arrow at={a2} dx={-dx} dy={-dy} k={k} />
      <Arrow at={b2} dx={dx} dy={dy} k={k} />
      <Figure k={k} at={{ x: (a2.x + b2.x) / 2 + ox * 0.5, y: (a2.y + b2.y) / 2 + oy * 0.5 }}
        text={`${value.toFixed(1)}`} onEdit={onEdit} />
    </g>
  )
}

function Radial({ centre, radius, value, prefix, k, onEdit }: {
  centre: Pt; radius: number; value: number; prefix: string; k: number; onEdit: () => void
}) {
  // A leader out at 45°, which keeps it clear of horizontal and vertical edges.
  const rim = { x: centre.x + radius * 0.707, y: centre.y + radius * 0.707 }
  const label = { x: rim.x + 6 * k, y: rim.y + 5 * k }
  return (
    <g>
      <line x1={centre.x} y1={sy(centre.y)} x2={label.x} y2={sy(label.y)}
        stroke={RULE} strokeWidth={0.3 * k} />
      <Arrow at={rim} dx={0.707} dy={0.707} k={k} />
      <circle cx={centre.x} cy={sy(centre.y)} r={0.6 * k} fill={RULE} />
      <Figure k={k} at={{ x: label.x + 4 * k, y: label.y }}
        text={`${prefix}${value.toFixed(1)}`} onEdit={onEdit} />
    </g>
  )
}

function Angle({ apex, first, second, value, k, onEdit }: {
  apex: XY; first: XY; second: XY; value: number; k: number; onEdit: () => void
}) {
  const r = 14 * k
  const a0 = Math.atan2(first.y - apex.y, first.x - apex.x)
  const a1 = Math.atan2(second.y - apex.y, second.x - apex.x)
  const steps = 20
  const points: string[] = []
  for (let i = 0; i <= steps; i++) {
    const t = a0 + (a1 - a0) * (i / steps)
    points.push(`${apex.x + r * Math.cos(t)},${sy(apex.y + r * Math.sin(t))}`)
  }
  const mid = (a0 + a1) / 2
  return (
    <g>
      <polyline points={points.join(' ')} fill="none" stroke={RULE} strokeWidth={0.4 * k} />
      <Figure k={k} at={{ x: apex.x + (r + 6 * k) * Math.cos(mid), y: apex.y + (r + 6 * k) * Math.sin(mid) }}
        text={`${value.toFixed(1)}°`} onEdit={onEdit} />
    </g>
  )
}

/** Every dimensional constraint, rendered where it applies. */
export function SketchDimensions({ cons, look, onEdit, k }: {
  cons: Con[]
  look: Lookup
  onEdit: (constraintId: string) => void
  /** Millimetres per 260 of view — annotations keep their size on screen. */
  k: number
}) {
  return (
    <g>
      {cons.map((c) => {
        const edit = () => onEdit(c.id)
        if (c.type === 'distance') {
          const a = look.pt(c.a as string), b = look.pt(c.b as string)
          if (!a || !b) return null
          return <Distance key={c.id} a={a} b={b} value={c.value as number} k={k} onEdit={edit} />
        }
        if (c.type === 'radius') {
          const circle = look.circle((c.circle ?? c.arc) as string)
          const centre = circle && look.pt(circle.center)
          if (!circle || !centre) return null
          return <Radial key={c.id} centre={centre} radius={circle.radius}
            value={c.value as number} prefix="R" k={k} onEdit={edit} />
        }
        if (c.type === 'arc_radius') {
          const arc = look.arc(c.arc as string)
          const centre = arc && look.pt(arc.center)
          const start = arc && look.pt(arc.a)
          if (!arc || !centre || !start) return null
          const radius = Math.hypot(start.x - centre.x, start.y - centre.y)
          return <Radial key={c.id} centre={centre} radius={radius}
            value={c.value as number} prefix="R" k={k} onEdit={edit} />
        }
        if (c.type === 'angle') {
          const l1 = look.line(c.line1 as string), l2 = look.line(c.line2 as string)
          if (!l1 || !l2) return null
          const a1 = look.pt(l1.a), b1 = look.pt(l1.b)
          const a2 = look.pt(l2.a), b2 = look.pt(l2.b)
          if (!a1 || !b1 || !a2 || !b2) return null
          // The shared endpoint is the apex when there is one; otherwise the
          // midpoint of the first line, which at least puts the arc nearby.
          const shared = [a1, b1].find((p) => p.id === a2.id || p.id === b2.id)
          const apex = shared ?? { x: (a1.x + b1.x) / 2, y: (a1.y + b1.y) / 2 }
          const away = (from: XY, p: XY, q: XY) => (Math.hypot(p.x - from.x, p.y - from.y)
            > Math.hypot(q.x - from.x, q.y - from.y) ? p : q)
          return <Angle key={c.id} apex={apex} first={away(apex, a1, b1)} second={away(apex, a2, b2)}
            value={c.value as number} k={k} onEdit={edit} />
        }
        return null
      })}
    </g>
  )
}

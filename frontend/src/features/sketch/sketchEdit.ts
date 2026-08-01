/**
 * Sketch editing operations — trim, extend, offset, mirror.
 *
 * These are pure functions over the canvas's entity lists (millimetres, y-up),
 * kept out of `SketchCanvas` because they are geometry rather than interaction.
 *
 * They edit the *drawing*, not the constraint system: trimming a line produces
 * shorter lines, offsetting produces new entities. Constraints referring to a
 * point that survives keep working; constraints on an entity that a trim
 * replaced are dropped, because a constraint naming a line that no longer
 * exists is a solve failure with no useful message. That is the honest trade —
 * a real trim in a parametric sketcher re-anchors constraints, which needs the
 * solver to own the geometry rather than the canvas.
 */

export type Pt = { id: string; x: number; y: number }
export type Ln = { id: string; a: string; b: string; construction?: boolean }
export type Arc = {
  id: string; a: string; b: string; center: string; ccw: boolean; construction?: boolean
}
export type Cir = { id: string; center: string; radius: number; construction?: boolean }
export type Con = { id: string; type: string; [k: string]: unknown }

export type Entities = { pts: Pt[]; lines: Ln[]; arcs: Arc[]; circles: Cir[]; cons: Con[] }

export type XY = { x: number; y: number }

const EPS = 1e-7

export const dist = (a: XY, b: XY) => Math.hypot(a.x - b.x, a.y - b.y)

/** Where two segments cross, as parameters along each. Null when parallel. */
export function lineLine(a1: XY, a2: XY, b1: XY, b2: XY): { t: number; u: number; at: XY } | null {
  const rx = a2.x - a1.x, ry = a2.y - a1.y
  const sx = b2.x - b1.x, sy = b2.y - b1.y
  const denom = rx * sy - ry * sx
  if (Math.abs(denom) < EPS) return null
  const t = ((b1.x - a1.x) * sy - (b1.y - a1.y) * sx) / denom
  const u = ((b1.x - a1.x) * ry - (b1.y - a1.y) * rx) / denom
  return { t, u, at: { x: a1.x + t * rx, y: a1.y + t * ry } }
}

/** Where a segment crosses a circle, as parameters along the segment. */
export function lineCircle(a: XY, b: XY, c: XY, r: number): { t: number; at: XY }[] {
  const dx = b.x - a.x, dy = b.y - a.y
  const fx = a.x - c.x, fy = a.y - c.y
  const qa = dx * dx + dy * dy
  if (qa < EPS) return []
  const qb = 2 * (fx * dx + fy * dy)
  const qc = fx * fx + fy * fy - r * r
  const disc = qb * qb - 4 * qa * qc
  if (disc < 0) return []
  const root = Math.sqrt(disc)
  return [(-qb - root) / (2 * qa), (-qb + root) / (2 * qa)].map((t) => ({
    t, at: { x: a.x + t * dx, y: a.y + t * dy },
  }))
}

/** Angle of `p` about `c`, normalised into [0, 2pi). */
export const angleAt = (c: XY, p: XY) => {
  const a = Math.atan2(p.y - c.y, p.x - c.x)
  return a < 0 ? a + 2 * Math.PI : a
}

let counter = 0
export const newId = (prefix: string) => `${prefix}e${(counter += 1)}`

const point = (list: Pt[], at: XY): { id: string; pts: Pt[] } => {
  const existing = list.find((p) => dist(p, at) < 1e-6)
  if (existing) return { id: existing.id, pts: list }
  const id = newId('p')
  return { id, pts: [...list, { id, x: at.x, y: at.y }] }
}

/** Every crossing of `line` with the rest of the sketch, as parameters along it. */
function crossingsOnLine(e: Entities, line: Ln, get: (id: string) => Pt): number[] {
  const a = get(line.a), b = get(line.b)
  const out: number[] = []
  for (const other of e.lines) {
    if (other.id === line.id) continue
    const hit = lineLine(a, b, get(other.a), get(other.b))
    if (hit && hit.u > EPS && hit.u < 1 - EPS) out.push(hit.t)
  }
  for (const c of e.circles) out.push(...lineCircle(a, b, get(c.center), c.radius).map((h) => h.t))
  for (const arc of e.arcs) {
    const centre = get(arc.center)
    const radius = dist(get(arc.a), centre)
    for (const hit of lineCircle(a, b, centre, radius)) {
      if (onSweep(arc, centre, hit.at, get)) out.push(hit.t)
    }
  }
  return out.filter((t) => t > EPS && t < 1 - EPS).sort((x, y) => x - y)
}

function onSweep(arc: Arc, centre: XY, at: XY, get: (id: string) => Pt): boolean {
  const a0 = angleAt(centre, get(arc.a))
  const a1 = angleAt(centre, get(arc.b))
  const target = angleAt(centre, at)
  const span = arc.ccw ? (a1 - a0 + 2 * Math.PI) % (2 * Math.PI)
    : (a0 - a1 + 2 * Math.PI) % (2 * Math.PI)
  const delta = arc.ccw ? (target - a0 + 2 * Math.PI) % (2 * Math.PI)
    : (a0 - target + 2 * Math.PI) % (2 * Math.PI)
  return delta <= span
}

/** Constraints that only name entities still present. */
function surviving(cons: Con[], alive: Set<string>): Con[] {
  const keys = ['point', 'a', 'b', 'line', 'line1', 'line2', 'circle', 'arc', 'arc1', 'arc2']
  return cons.filter((c) => keys.every((k) => {
    const value = c[k]
    return typeof value !== 'string' || alive.has(value)
  }))
}

function aliveIds(e: Entities): Set<string> {
  return new Set([...e.pts, ...e.lines, ...e.arcs, ...e.circles].map((x) => x.id))
}

/** Drop constraints left dangling by an edit. */
export function prune(e: Entities): Entities {
  return { ...e, cons: surviving(e.cons, aliveIds(e)) }
}

/**
 * Cut the span of `target` that contains `at` back to its nearest crossings.
 *
 * A line with no crossing on one side loses that whole end; a line crossed on
 * both sides loses only the middle. A circle trimmed anywhere becomes an arc
 * running the *other* way round between the two crossings either side.
 */
export function trim(e: Entities, target: { kind: string; id: string }, at: XY): Entities {
  const get = (id: string) => e.pts.find((p) => p.id === id)!

  if (target.kind === 'line') {
    const line = e.lines.find((l) => l.id === target.id)
    if (!line) return e
    const a = get(line.a), b = get(line.b)
    const length2 = (b.x - a.x) ** 2 + (b.y - a.y) ** 2 || 1
    const clickT = ((at.x - a.x) * (b.x - a.x) + (at.y - a.y) * (b.y - a.y)) / length2
    const cuts = crossingsOnLine(e, line, get)
    if (!cuts.length) return e

    const lower = [0, ...cuts].filter((t) => t <= clickT).pop() ?? 0
    const upper = [...cuts, 1].find((t) => t >= clickT) ?? 1
    const spans = ([[0, lower], [upper, 1]] as [number, number][])
      .filter(([s, t]) => t - s > 1e-6)

    let pts = e.pts
    const kept: Ln[] = []
    for (const [s, t] of spans) {
      const start = point(pts, { x: a.x + s * (b.x - a.x), y: a.y + s * (b.y - a.y) })
      pts = start.pts
      const end = point(pts, { x: a.x + t * (b.x - a.x), y: a.y + t * (b.y - a.y) })
      pts = end.pts
      kept.push({ id: newId('l'), a: start.id, b: end.id, construction: line.construction })
    }
    return prune({ ...e, pts, lines: [...e.lines.filter((l) => l.id !== line.id), ...kept] })
  }

  if (target.kind === 'circle') {
    const circle = e.circles.find((c) => c.id === target.id)
    if (!circle) return e
    const centre = get(circle.center)
    const hits: XY[] = []
    for (const line of e.lines) {
      hits.push(...lineCircle(get(line.a), get(line.b), centre, circle.radius)
        .filter((h) => h.t > EPS && h.t < 1 - EPS).map((h) => h.at))
    }
    if (hits.length < 2) return e
    const clickAngle = angleAt(centre, at)
    const angles = hits.map((h) => angleAt(centre, h)).sort((x, y) => x - y)
    const after = angles.find((a) => a > clickAngle) ?? angles[0] + 2 * Math.PI
    const before = [...angles].reverse().find((a) => a < clickAngle) ?? angles[angles.length - 1] - 2 * Math.PI
    // Keep the complement of the clicked span: from `after` round to `before`.
    const onRim = (angle: number): XY => ({
      x: centre.x + circle.radius * Math.cos(angle), y: centre.y + circle.radius * Math.sin(angle),
    })
    let pts = e.pts
    const start = point(pts, onRim(after)); pts = start.pts
    const end = point(pts, onRim(before)); pts = end.pts
    const arc: Arc = { id: newId('a'), a: start.id, b: end.id, center: circle.center,
      ccw: true, construction: circle.construction }
    return prune({ ...e, pts, circles: e.circles.filter((c) => c.id !== circle.id),
      arcs: [...e.arcs, arc] })
  }

  return e
}

/**
 * Lengthen a line until it meets something, from whichever end is nearer `at`.
 *
 * The line's *infinite* extent is intersected with every other entity, and the
 * nearest hit beyond the chosen end wins. Nothing beyond it means nothing to
 * extend to, and the line is left alone rather than stretched by a guess.
 */
export function extend(e: Entities, lineId: string, at: XY): Entities {
  const get = (id: string) => e.pts.find((p) => p.id === id)!
  const line = e.lines.find((l) => l.id === lineId)
  if (!line) return e
  const a = get(line.a), b = get(line.b)
  const fromB = dist(at, b) < dist(at, a)
  const origin = fromB ? a : b
  const moving = fromB ? line.b : line.a
  const dx = (fromB ? b.x : a.x) - origin.x
  const dy = (fromB ? b.y : a.y) - origin.y

  // Parameters along origin -> moving end; anything > 1 is past that end.
  const far = { x: origin.x + dx * 1e4, y: origin.y + dy * 1e4 }
  const candidates: number[] = []
  for (const other of e.lines) {
    if (other.id === lineId) continue
    const hit = lineLine(origin, far, get(other.a), get(other.b))
    if (hit && hit.u >= -EPS && hit.u <= 1 + EPS) candidates.push(hit.t * 1e4)
  }
  for (const c of e.circles) {
    candidates.push(...lineCircle(origin, far, get(c.center), c.radius).map((h) => h.t * 1e4))
  }
  const beyond = candidates.filter((t) => t > 1 + 1e-6).sort((x, y) => x - y)
  if (!beyond.length) return e

  const t = beyond[0]
  const target = { x: origin.x + dx * t, y: origin.y + dy * t }
  return { ...e, pts: e.pts.map((p) => (p.id === moving ? { ...p, ...target } : p)) }
}

/** A copy of the selected lines and circles, moved `distance` mm outward. */
export function offset(e: Entities, selection: { kind: string; id: string }[], distance: number): Entities {
  const get = (id: string) => e.pts.find((p) => p.id === id)!
  let pts = e.pts
  const lines: Ln[] = []
  const circles: Cir[] = []

  for (const sel of selection) {
    if (sel.kind === 'line') {
      const line = e.lines.find((l) => l.id === sel.id)
      if (!line) continue
      const a = get(line.a), b = get(line.b)
      const length = dist(a, b) || 1
      // Left-hand normal; a negative distance offsets the other way.
      const nx = -(b.y - a.y) / length * distance
      const ny = (b.x - a.x) / length * distance
      const start = point(pts, { x: a.x + nx, y: a.y + ny }); pts = start.pts
      const end = point(pts, { x: b.x + nx, y: b.y + ny }); pts = end.pts
      lines.push({ id: newId('l'), a: start.id, b: end.id, construction: line.construction })
    } else if (sel.kind === 'circle') {
      const circle = e.circles.find((c) => c.id === sel.id)
      if (!circle || circle.radius + distance <= 0) continue
      circles.push({ id: newId('c'), center: circle.center,
        radius: circle.radius + distance, construction: circle.construction })
    }
  }
  if (!lines.length && !circles.length) return e
  return { ...e, pts, lines: [...e.lines, ...lines], circles: [...e.circles, ...circles] }
}

/**
 * Mirror the selected entities across a line, as new geometry.
 *
 * Deliberately a copy rather than a symmetry constraint: the `symmetric`
 * constraint holds two points either side of an axis, which is what you want
 * once the geometry exists. This is how it comes to exist.
 */
export function mirror(e: Entities, selection: { kind: string; id: string }[], axisId: string): Entities {
  const get = (id: string) => e.pts.find((p) => p.id === id)!
  const axis = e.lines.find((l) => l.id === axisId)
  if (!axis) return e
  const a = get(axis.a), b = get(axis.b)
  const dx = b.x - a.x, dy = b.y - a.y
  const length2 = dx * dx + dy * dy || 1

  const reflect = (p: XY): XY => {
    const t = ((p.x - a.x) * dx + (p.y - a.y) * dy) / length2
    const footX = a.x + t * dx, footY = a.y + t * dy
    return { x: 2 * footX - p.x, y: 2 * footY - p.y }
  }

  let pts = e.pts
  const map = new Map<string, string>()
  const mirroredPoint = (id: string) => {
    const seen = map.get(id)
    if (seen) return seen
    const made = point(pts, reflect(get(id)))
    pts = made.pts
    map.set(id, made.id)
    return made.id
  }

  const lines: Ln[] = []
  const arcs: Arc[] = []
  const circles: Cir[] = []
  for (const sel of selection) {
    if (sel.kind === 'line' && sel.id !== axisId) {
      const line = e.lines.find((l) => l.id === sel.id)
      if (line) lines.push({ id: newId('l'), a: mirroredPoint(line.a), b: mirroredPoint(line.b),
        construction: line.construction })
    } else if (sel.kind === 'arc') {
      const arc = e.arcs.find((x) => x.id === sel.id)
      // Reflection reverses the sweep: a CCW arc mirrors to a CW one.
      if (arc) arcs.push({ id: newId('a'), a: mirroredPoint(arc.a), b: mirroredPoint(arc.b),
        center: mirroredPoint(arc.center), ccw: !arc.ccw, construction: arc.construction })
    } else if (sel.kind === 'circle') {
      const circle = e.circles.find((c) => c.id === sel.id)
      if (circle) circles.push({ id: newId('c'), center: mirroredPoint(circle.center),
        radius: circle.radius, construction: circle.construction })
    }
  }
  if (!lines.length && !arcs.length && !circles.length) return e
  return { ...e, pts, lines: [...e.lines, ...lines], arcs: [...e.arcs, ...arcs],
    circles: [...e.circles, ...circles] }
}

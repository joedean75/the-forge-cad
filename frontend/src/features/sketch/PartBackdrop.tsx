import { useEffect, useState } from 'react'
import { STLLoader } from 'three/examples/jsm/loaders/STLLoader.js'
import { collectProxies } from '../../util/featureTree'
import type { FeatureNode } from '../../../../shared/types'

/**
 * The part, behind the 2D sketch, so you can see what you are drawing on.
 *
 * Three layers, each answering a different question, and each optional:
 *
 * - **shaded** — the body as an orthographic shaded view down the sketch
 *   plane's normal. Depth-sorted and Lambert-lit, so a boss reads as a boss
 *   rather than as a blob. This is "what does the part look like from here".
 * - **section** — the material the plane actually cuts, filled. This is the one
 *   that matters while sketching: an outline tells you where the part *is*, a
 *   filled section tells you what is under your pen at this height.
 * - **contour** — a one-pixel silhouette, including the rims of any holes,
 *   recovered by eroding the projected mask. It is what makes the shading read
 *   as an object instead of a gradient.
 *
 * Coordinates match the sketch canvas exactly (mm, y-up), mirroring the kernel's
 * plane mapping in `evaluator/sdf.py`:
 *   xy → (x, y), normal z · xz → (x, z), normal y · yz → (y, z), normal x
 *
 * **The projected mask has to have its winding normalised.** Filling every
 * triangle into one path and calling `fill(path, 'nonzero')` is the obvious
 * thing and it is wrong for a *closed* body: the near and far faces project
 * with opposite winding, sum to zero, and the silhouette comes out empty. That
 * is what used to happen — the backdrop was invisible for every real part, and
 * only the section line showed. Flipping each triangle to a positive projected
 * area first makes nonzero a union, which is what was meant.
 */

type Plane = 'xy' | 'xz' | 'yz'

/** What to draw behind the sketch. */
export type BackdropMode = 'shaded' | 'section' | 'outline' | 'off'

/** The visible window, in sketch millimetres. Height follows from the aspect. */
export type View = { cx: number; cy: number; span: number }

/** The canvas is a fixed 260 × 190 mm window at zoom 1, and keeps that shape. */
export const VIEW_ASPECT = 190 / 260
export const spanV = (span: number) => span * VIEW_ASPECT

// Which world components form the sketch (u, v) axes and the normal w.
const AXES: Record<Plane, [number, number, number]> = {
  xy: [0, 1, 2],
  xz: [0, 2, 1],
  yz: [1, 2, 0],
}

// Mirror of backend datum.FACE_MAP: face -> [plane, normal-axis index, take-max].
const FACE_MAP: Record<string, [Plane, number, boolean]> = {
  top: ['xy', 2, true], bottom: ['xy', 2, false],
  right: ['yz', 0, true], left: ['yz', 0, false],
  back: ['xz', 1, true], front: ['xz', 1, false],
}

/**
 * Resolve the display plane + section offset (mm) for sketching on a feature's
 * face, from that feature's proxy AABB — a client-side echo of the backend
 * datum resolution so the sketch view reflects where the plane will land.
 */
export function faceToPlaneOffset(
  node: FeatureNode,
  face: string,
): { plane: Plane; offsetMm: number; originMm: [number, number] } | null {
  const entry = FACE_MAP[face]
  if (!entry) return null
  const [plane, axis, takeMax] = entry
  const proxies = collectProxies(node)
  if (!proxies.length) return null
  const keys = ['x', 'y', 'z'] as const
  // Track the feature's AABB on all three axes: the normal axis gives the plane
  // offset; the two in-plane axes give the face centre (so the sketch zeroes on
  // the face, mirroring the backend datum frame).
  const lo = [Infinity, Infinity, Infinity]
  const hi = [-Infinity, -Infinity, -Infinity]
  for (const p of proxies) {
    for (let a = 0; a < 3; a++) {
      const c = (p.center as Record<string, number>)[keys[a]]
      let half = 0
      if (p.kind === 'box' && p.size) half = (p.size as Record<string, number>)[keys[a]] / 2
      else if (p.kind === 'sphere') half = p.radius ?? 0
      else if (p.kind === 'cyl') half = Math.max(p.radius ?? 0, (p.height ?? 0) / 2)
      lo[a] = Math.min(lo[a], c - half)
      hi[a] = Math.max(hi[a], c + half)
    }
  }
  const [ui, vi] = AXES[plane]
  const u0 = ((lo[ui] + hi[ui]) / 2) * 1000
  const v0 = ((lo[vi] + hi[vi]) / 2) * 1000
  return { plane, offsetMm: (takeMax ? hi[axis] : lo[axis]) * 1000, originMm: [u0, v0] }
}

/** Fetch + parse the document's STL data-URI into raw triangle positions (metres). */
export function useMeshTriangles(url: string | null | undefined): Float32Array | null {
  const [positions, setPositions] = useState<Float32Array | null>(null)
  useEffect(() => {
    let alive = true
    if (!url) { setPositions(null); return }
    fetch(url)
      .then((r) => r.arrayBuffer())
      .then((buf) => {
        const geom = new STLLoader().parse(buf)
        if (alive) setPositions(geom.getAttribute('position').array as Float32Array)
      })
      .catch(() => { if (alive) setPositions(null) })
    return () => { alive = false }
  }, [url])
  return positions
}

/** The part's extent in sketch millimetres, for framing the view. */
export function partBounds(
  positions: Float32Array | null, plane: Plane, originMm: [number, number],
): { u0: number; v0: number; u1: number; v1: number } | null {
  if (!positions || !positions.length) return null
  const [ui, vi] = AXES[plane]
  let u0 = Infinity, v0 = Infinity, u1 = -Infinity, v1 = -Infinity
  for (let i = 0; i + 2 < positions.length; i += 3) {
    const u = positions[i + ui] * 1000 - originMm[0]
    const v = positions[i + vi] * 1000 - originMm[1]
    if (u < u0) u0 = u
    if (u > u1) u1 = u
    if (v < v0) v0 = v
    if (v > v1) v1 = v
  }
  return Number.isFinite(u0) ? { u0, v0, u1, v1 } : null
}

// Cap on triangles rasterized. A projected body is covered by far fewer
// triangles than the mesh holds, so an even sample still reads as a mass — and
// it keeps the rasterization off the far side of a frame budget.
const MAX_TRIS = 20000

// Pixels across the rendered window. Re-rasterized per view, so this is
// resolution *at the current zoom* rather than over the whole model.
const RASTER_W = 1100

/** Segments (display mm, y-up) where the mesh crosses the plane w = sectionM. */
function computeSection(
  pos: Float32Array, plane: Plane, sectionM: number, origin: [number, number],
): number[][] {
  const [ui, vi, wi] = AXES[plane]
  const [ox, oy] = origin
  const segs: number[][] = []
  for (let t = 0; t + 8 < pos.length; t += 9) {
    const wa = pos[t + wi] - sectionM
    const wb = pos[t + 3 + wi] - sectionM
    const wc = pos[t + 6 + wi] - sectionM
    // Gather up to two edge crossings — inlined, no per-triangle allocation.
    let x0 = 0, y0 = 0, x1 = 0, y1 = 0, n = 0
    if ((wa > 0) !== (wb > 0)) {
      const f = wa / (wa - wb)
      x0 = (pos[t + ui] + f * (pos[t + 3 + ui] - pos[t + ui])) * 1000 - ox
      y0 = (pos[t + vi] + f * (pos[t + 3 + vi] - pos[t + vi])) * 1000 - oy
      n = 1
    }
    if (n < 2 && (wb > 0) !== (wc > 0)) {
      const f = wb / (wb - wc)
      const x = (pos[t + 3 + ui] + f * (pos[t + 6 + ui] - pos[t + 3 + ui])) * 1000 - ox
      const y = (pos[t + 3 + vi] + f * (pos[t + 6 + vi] - pos[t + 3 + vi])) * 1000 - oy
      if (n === 0) { x0 = x; y0 = y; n = 1 } else { x1 = x; y1 = y; n = 2 }
    }
    if (n < 2 && (wc > 0) !== (wa > 0)) {
      const f = wc / (wc - wa)
      const x = (pos[t + 6 + ui] + f * (pos[t + ui] - pos[t + 6 + ui])) * 1000 - ox
      const y = (pos[t + 6 + vi] + f * (pos[t + vi] - pos[t + 6 + vi])) * 1000 - oy
      if (n === 0) { x0 = x; y0 = y; n = 1 } else { x1 = x; y1 = y; n = 2 }
    }
    if (n === 2) segs.push([x0, y0, x1, y1])
  }
  return segs
}

type Frame = {
  W: number; H: number
  X: (u: number) => number
  Y: (v: number) => number
}

function frameOf(view: View): Frame {
  const W = RASTER_W
  const H = Math.round(RASTER_W * VIEW_ASPECT)
  const u0 = view.cx - view.span / 2
  const v1 = view.cy + spanV(view.span) / 2
  return {
    W, H,
    X: (u) => ((u - u0) / view.span) * W,
    Y: (v) => ((v1 - v) / spanV(view.span)) * H,
  }
}

function canvasOf(f: Frame): [HTMLCanvasElement, CanvasRenderingContext2D] | null {
  const canvas = document.createElement('canvas')
  canvas.width = f.W
  canvas.height = f.H
  const ctx = canvas.getContext('2d')
  return ctx ? [canvas, ctx] : null
}

/** Triangles in display millimetres, depth-sorted far-to-near, with normals. */
function projected(pos: Float32Array, plane: Plane, origin: [number, number]) {
  const [ui, vi, wi] = AXES[plane]
  const [ox, oy] = origin
  const total = Math.floor(pos.length / 9)
  const stride = 9 * Math.max(1, Math.ceil(total / MAX_TRIS))
  const count = Math.ceil(total / (stride / 9))

  const u = new Float32Array(count * 3)
  const v = new Float32Array(count * 3)
  const lit = new Float32Array(count)
  const depth = new Float32Array(count)
  // Light from the upper left and slightly toward the viewer, in (u, v, w).
  const L = [-0.35, 0.52, 0.78]
  const norm = Math.hypot(L[0], L[1], L[2])
  let n = 0
  for (let t = 0; t + 8 < pos.length; t += stride) {
    const au = pos[t + ui] * 1000 - ox, av = pos[t + vi] * 1000 - oy, aw = pos[t + wi] * 1000
    const bu = pos[t + 3 + ui] * 1000 - ox, bv = pos[t + 3 + vi] * 1000 - oy, bw = pos[t + 3 + wi] * 1000
    const cu = pos[t + 6 + ui] * 1000 - ox, cv = pos[t + 6 + vi] * 1000 - oy, cw = pos[t + 6 + wi] * 1000

    const e1 = [bu - au, bv - av, bw - aw]
    const e2 = [cu - au, cv - av, cw - aw]
    let nu = e1[1] * e2[2] - e1[2] * e2[1]
    let nv = e1[2] * e2[0] - e1[0] * e2[2]
    let nw = e1[0] * e2[1] - e1[1] * e2[0]
    const len = Math.hypot(nu, nv, nw) || 1
    nu /= len; nv /= len; nw /= len

    const i3 = n * 3
    u[i3] = au; u[i3 + 1] = bu; u[i3 + 2] = cu
    v[i3] = av; v[i3 + 1] = bv; v[i3 + 2] = cv
    // `abs` because a closed body shows both windings and neither is "behind";
    // what is wanted is form, not which way the triangle happens to face.
    lit[n] = Math.abs((nu * L[0] + nv * L[1] + nw * L[2]) / norm)
    depth[n] = (aw + bw + cw) / 3
    n += 1
  }

  // **Depth carries most of the shading here, not the light.** Looking straight
  // down a plane's normal, every face parallel to it has the same Lambert term,
  // so a pocket floor and the plate around it come out the same grey and the
  // view says nothing. Mapping distance to brightness makes a boss read as
  // raised and a pocket as sunk, which is the question someone sketching on the
  // plane is actually asking.
  let near = -Infinity, far = Infinity
  for (let i = 0; i < n; i++) {
    if (depth[i] > near) near = depth[i]
    if (depth[i] < far) far = depth[i]
  }
  const range = near - far || 1
  const shade = new Float32Array(n)
  for (let i = 0; i < n; i++) {
    shade[i] = (0.42 + 0.58 * ((depth[i] - far) / range)) * (0.78 + 0.22 * lit[i])
  }

  const order = new Int32Array(n)
  for (let i = 0; i < n; i++) order[i] = i
  // Painter's algorithm: far first, so the near surface wins. "Near" is +w,
  // because the sketch is looked at down the plane's own normal.
  const sorted = Array.from(order).sort((a, b) => depth[a] - depth[b])
  return { u, v, shade, count: n, order: sorted }
}

/** Every triangle as one path, each wound positive so `nonzero` unions them. */
function maskPath(tri: ReturnType<typeof projected>, f: Frame): Path2D {
  const path = new Path2D()
  for (let i = 0; i < tri.count; i++) {
    const k = i * 3
    const ax = f.X(tri.u[k]), ay = f.Y(tri.v[k])
    const bx = f.X(tri.u[k + 1]), by = f.Y(tri.v[k + 1])
    const cx = f.X(tri.u[k + 2]), cy = f.Y(tri.v[k + 2])
    path.moveTo(ax, ay)
    // Negative projected area means this face points away; drawing it in the
    // same winding as the near faces is what makes the union a union.
    if ((bx - ax) * (cy - ay) - (by - ay) * (cx - ax) < 0) {
      path.lineTo(cx, cy); path.lineTo(bx, by)
    } else {
      path.lineTo(bx, by); path.lineTo(cx, cy)
    }
    path.closePath()
  }
  return path
}

/** A one-pixel outline of the projected mask, holes included. */
function contourImage(tri: ReturnType<typeof projected>, f: Frame, colour: string): string | null {
  const made = canvasOf(f)
  const eroded = canvasOf(f)
  if (!made || !eroded) return null
  const [maskCanvas, mask] = made
  const [outCanvas, out] = eroded
  const path = maskPath(tri, f)

  mask.fillStyle = '#fff'
  mask.fill(path, 'nonzero')

  // Erode by intersecting the mask with itself shifted one pixel each way, then
  // subtract: what survives is exactly the boundary, on the outside *and* around
  // every hole. Cheaper and more faithful than guessing at silhouette triangles.
  out.drawImage(maskCanvas, 0, 0)
  out.globalCompositeOperation = 'destination-in'
  for (const [dx, dy] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) out.drawImage(maskCanvas, dx, dy)
  out.globalCompositeOperation = 'source-over'

  mask.globalCompositeOperation = 'destination-out'
  mask.drawImage(outCanvas, 0, 0)
  mask.globalCompositeOperation = 'source-in'
  mask.fillStyle = colour
  mask.fillRect(0, 0, f.W, f.H)
  return maskCanvas.toDataURL()
}

/**
 * The material the plane cuts, filled by scanline parity over the section edges.
 *
 * Parity rather than chaining the segments into loops: the segments come out of
 * the mesh unordered and a watertight body gives an even number of crossings on
 * every row regardless, so the fill is exact without the loop walk — and a
 * missing segment degrades to a thin stripe rather than to a filled screen.
 */
function sectionFill(segments: number[][], f: Frame, colour: string): string | null {
  if (segments.length < 2) return null
  const made = canvasOf(f)
  if (!made) return null
  const [canvas, ctx] = made

  // Bucket segments by the pixel rows they span, so each row only looks at what
  // can actually cross it.
  const rows: number[][] = Array.from({ length: f.H }, () => [])
  for (let i = 0; i < segments.length; i++) {
    const [, y0, , y1] = segments[i]
    const top = Math.max(0, Math.floor(Math.min(f.Y(y0), f.Y(y1))))
    const bottom = Math.min(f.H - 1, Math.ceil(Math.max(f.Y(y0), f.Y(y1))))
    if (bottom < 0 || top > f.H - 1) continue
    for (let r = top; r <= bottom; r++) rows[r].push(i)
  }

  ctx.fillStyle = colour
  const crossings: number[] = []
  for (let r = 0; r < f.H; r++) {
    if (!rows[r].length) continue
    crossings.length = 0
    const y = r + 0.5
    for (const i of rows[r]) {
      const [x0, my0, x1, my1] = segments[i]
      const ya = f.Y(my0), yb = f.Y(my1)
      if ((ya > y) === (yb > y)) continue      // half-open: a vertex counts once
      const t = (y - ya) / (yb - ya)
      crossings.push(f.X(x0) + t * (f.X(x1) - f.X(x0)))
    }
    if (crossings.length < 2) continue
    crossings.sort((a, b) => a - b)
    for (let k = 0; k + 1 < crossings.length; k += 2) {
      ctx.fillRect(crossings[k], r, crossings[k + 1] - crossings[k], 1)
    }
  }
  return canvas.toDataURL()
}

/** The shaded body: depth-sorted, Lambert-lit triangles. */
function shadedImage(tri: ReturnType<typeof projected>, f: Frame): string | null {
  const made = canvasOf(f)
  if (!made) return null
  const [canvas, ctx] = made
  // Stroking each triangle in its own fill colour closes the antialiased seam
  // between neighbours. Without it a flat face is crazed with hairline cracks —
  // adjacent triangles each cover half of the boundary pixel and the two
  // half-coverages composite to less than one, so the shared edge shows.
  ctx.lineWidth = 1
  ctx.lineJoin = 'round'
  for (const i of tri.order) {
    const k = i * 3
    const s = tri.shade[i]
    // A muted steel, so near-white sketch strokes stay the brightest thing on
    // the canvas — this is a backdrop, not the subject.
    const colour = `rgb(${Math.round(112 * s)},${Math.round(128 * s)},${Math.round(152 * s)})`
    ctx.fillStyle = colour
    ctx.strokeStyle = colour
    ctx.beginPath()
    ctx.moveTo(f.X(tri.u[k]), f.Y(tri.v[k]))
    ctx.lineTo(f.X(tri.u[k + 1]), f.Y(tri.v[k + 1]))
    ctx.lineTo(f.X(tri.u[k + 2]), f.Y(tri.v[k + 2]))
    ctx.closePath()
    ctx.fill()
    ctx.stroke()
  }
  return canvas.toDataURL()
}

type Layers = { shaded: string | null; contour: string | null; section: string | null }

export function PartBackdrop({
  positions,
  plane,
  sectionMm,
  originMm = [0, 0],
  view,
  mode = 'shaded',
}: {
  positions: Float32Array | null
  plane: Plane
  /** Plane position along its normal, in mm (sketch units). */
  sectionMm: number
  /** In-plane face centre (mm) — the backdrop shifts by −origin so the face is
   *  centered on the sketch origin, matching where geometry lands on-face. */
  originMm?: [number, number]
  view: View
  mode?: BackdropMode
}) {
  const [layers, setLayers] = useState<Layers>({ shaded: null, contour: null, section: null })
  const [edges, setEdges] = useState<number[][]>([])

  const [ox, oy] = originMm
  // Rasterizing runs off the render path so opening the sketch, panning and
  // zooming never wait on the mesh — the canvas paints, the part catches up.
  useEffect(() => {
    if (!positions || mode === 'off') {
      setLayers({ shaded: null, contour: null, section: null })
      setEdges([])
      return
    }
    let alive = true
    const id = window.setTimeout(() => {
      if (!alive) return
      const f = frameOf(view)
      const tri = projected(positions, plane, [ox, oy])
      const segments = computeSection(positions, plane, sectionMm / 1000, [ox, oy])
      setEdges(segments)
      setLayers({
        shaded: mode === 'shaded' ? shadedImage(tri, f) : null,
        contour: mode === 'shaded' || mode === 'outline' ? contourImage(tri, f, '#aebdd1') : null,
        // Under the shaded view the cut is a hint — the shading is the subject
        // and a heavy tint over the whole plate flattens it back out. In
        // `section` mode the cut *is* the subject, so it carries.
        section: sectionFill(segments, f,
          mode === 'shaded' ? 'rgba(92,201,167,0.10)' : 'rgba(92,201,167,0.24)'),
      })
    }, 0)
    return () => { alive = false; window.clearTimeout(id) }
  }, [positions, plane, sectionMm, ox, oy, view, mode])

  if (!positions || mode === 'off') return null
  const x = view.cx - view.span / 2
  const y = -view.cy - spanV(view.span) / 2
  const w = view.span
  const h = spanV(view.span)
  const line = view.span / 260   // keep stroke weight constant on screen

  return (
    <g pointerEvents="none">
      {layers.shaded && (
        <image href={layers.shaded} x={x} y={y} width={w} height={h}
          preserveAspectRatio="none" opacity={0.72} />
      )}
      {layers.section && (
        <image href={layers.section} x={x} y={y} width={w} height={h} preserveAspectRatio="none" />
      )}
      {layers.contour && (
        <image href={layers.contour} x={x} y={y} width={w} height={h}
          preserveAspectRatio="none" opacity={mode === 'outline' ? 0.8 : 0.65} />
      )}
      {/* The material boundary at the active sketch plane, drawn as vectors so
          it stays crisp at any zoom — it is the line you are sketching against. */}
      {edges.map((s, i) => (
        <line key={i} x1={s[0]} y1={-s[1]} x2={s[2]} y2={-s[3]}
          stroke="#5cc9a7" strokeWidth={0.7 * line} opacity={0.9} />
      ))}
    </g>
  )
}

import { useEffect, useState } from 'react'
import { STLLoader } from 'three/examples/jsm/loaders/STLLoader.js'
import { collectProxies } from '../../util/featureTree'
import type { FeatureNode } from '../../../../shared/types'

/**
 * Renders the current part behind the 2D sketch so you can see what you are
 * sketching on: a dim silhouette of the whole body projected onto the sketch
 * plane, plus a bright cross-section outline where the plane actually cuts
 * material. Coordinates match the sketch canvas exactly (mm, y-up), mirroring
 * the kernel's plane mapping in `evaluator/sdf.py`:
 *   xy → (x, y), normal z · xz → (x, z), normal y · yz → (y, z), normal x
 */

type Plane = 'xy' | 'xz' | 'yz'

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

// Cap on triangles rasterized for the silhouette. A solid part's projected
// outline is filled by far fewer triangles than the full mesh, so an even sample
// still reads as a mass — and it keeps the Path2D fill from stalling the thread.
const SIL_MAX_TRIS = 12000

/** Segments (mm, y-up) where the mesh crosses the plane w = sectionM. */
function computeSection(pos: Float32Array, plane: Plane, sectionM: number): number[][] {
  const [ui, vi, wi] = AXES[plane]
  const segs: number[][] = []
  for (let t = 0; t + 8 < pos.length; t += 9) {
    const wa = pos[t + wi] - sectionM
    const wb = pos[t + 3 + wi] - sectionM
    const wc = pos[t + 6 + wi] - sectionM
    // Gather up to two edge crossings — inlined, no per-triangle allocation.
    let x0 = 0, y0 = 0, x1 = 0, y1 = 0, n = 0
    if ((wa > 0) !== (wb > 0)) {
      const f = wa / (wa - wb)
      x0 = (pos[t + ui] + f * (pos[t + 3 + ui] - pos[t + ui])) * 1000
      y0 = (pos[t + vi] + f * (pos[t + 3 + vi] - pos[t + vi])) * 1000
      n = 1
    }
    if (n < 2 && (wb > 0) !== (wc > 0)) {
      const f = wb / (wb - wc)
      const x = (pos[t + 3 + ui] + f * (pos[t + 6 + ui] - pos[t + 3 + ui])) * 1000
      const y = (pos[t + 3 + vi] + f * (pos[t + 6 + vi] - pos[t + 3 + vi])) * 1000
      if (n === 0) { x0 = x; y0 = y; n = 1 } else { x1 = x; y1 = y; n = 2 }
    }
    if (n < 2 && (wc > 0) !== (wa > 0)) {
      const f = wc / (wc - wa)
      const x = (pos[t + 6 + ui] + f * (pos[t + ui] - pos[t + 6 + ui])) * 1000
      const y = (pos[t + 6 + vi] + f * (pos[t + vi] - pos[t + 6 + vi])) * 1000
      if (n === 0) { x0 = x; y0 = y; n = 1 } else { x1 = x; y1 = y; n = 2 }
    }
    if (n === 2) segs.push([x0, y0, x1, y1])
  }
  return segs
}

/** Rasterize a (down-sampled) projected silhouette to a data-URL image. */
function silhouetteImage(pos: Float32Array, plane: Plane): string | null {
  const [ui, vi] = AXES[plane]
  const PX = 3 // px per mm across the 260×190 mm viewBox
  const canvas = document.createElement('canvas')
  canvas.width = 260 * PX
  canvas.height = 190 * PX
  const ctx = canvas.getContext('2d')
  if (!ctx) return null
  const triCount = Math.floor(pos.length / 9)
  const step = 9 * Math.max(1, Math.ceil(triCount / SIL_MAX_TRIS))
  const path = new Path2D()
  const X = (m: number) => (m * 1000 + 130) * PX
  const Y = (m: number) => (95 - m * 1000) * PX
  for (let t = 0; t + 8 < pos.length; t += step) {
    path.moveTo(X(pos[t + ui]), Y(pos[t + vi]))
    path.lineTo(X(pos[t + 3 + ui]), Y(pos[t + 3 + vi]))
    path.lineTo(X(pos[t + 6 + ui]), Y(pos[t + 6 + vi]))
    path.closePath()
  }
  ctx.fillStyle = 'rgba(125, 139, 160, 0.16)' // steel, faint
  ctx.fill(path, 'nonzero')
  return canvas.toDataURL()
}

export function PartBackdrop({
  positions,
  plane,
  sectionMm,
  originMm = [0, 0],
}: {
  positions: Float32Array | null
  plane: Plane
  /** Plane position along its normal, in mm (sketch units). */
  sectionMm: number
  /** In-plane face centre (mm) — the backdrop shifts by −origin so the face is
   *  centered on the sketch origin, matching where geometry lands on-face. */
  originMm?: [number, number]
}) {
  // Both derivations run off the render path (deferred effect) so opening the
  // sketch never waits on rasterizing the mesh — the canvas paints first.
  const [image, setImage] = useState<string | null>(null)
  const [section, setSection] = useState<number[][]>([])

  useEffect(() => {
    if (!positions) { setImage(null); return }
    let alive = true
    const id = window.setTimeout(() => { if (alive) setImage(silhouetteImage(positions, plane)) }, 0)
    return () => { alive = false; window.clearTimeout(id) }
  }, [positions, plane])

  useEffect(() => {
    if (!positions) { setSection([]); return }
    let alive = true
    const id = window.setTimeout(() => { if (alive) setSection(computeSection(positions, plane, sectionMm / 1000)) }, 0)
    return () => { alive = false; window.clearTimeout(id) }
  }, [positions, plane, sectionMm])

  if (!positions) return null
  // Shift the whole backdrop so the on-face centre sits at the sketch origin.
  // SVG y is flipped (screen y-down vs sketch y-up), so translate by (−u0, +v0).
  const [ox, oy] = originMm
  return (
    <g pointerEvents="none" transform={`translate(${-ox} ${oy})`}>
      {image && <image href={image} x={-130} y={-95} width={260} height={190} preserveAspectRatio="none" />}
      {/* material boundary at the active sketch plane */}
      {section.map((s, i) => (
        <line key={i} x1={s[0]} y1={-s[1]} x2={s[2]} y2={-s[3]} stroke="#5cc9a7" strokeWidth={0.7} opacity={0.85} />
      ))}
    </g>
  )
}

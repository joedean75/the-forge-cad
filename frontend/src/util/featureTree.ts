import type { FeatureNode, FeatureTreeDict } from '../../../shared/types'

/**
 * Pure, immutable helpers for reading and editing a serialized FeatureTree
 * ({ version, root }). The node shape mirrors the backend kernel:
 *   - primitives:  { type: 'BoxFeature', params: { size:{x,y,z}, center:{x,y,z} } }
 *   - boolean:     { type: 'BooleanFeature', params: { operation, children: FeatureNode[] } }
 */

export type BooleanOp = 'union' | 'subtract' | 'intersect'

export interface FlatNode {
  node: FeatureNode
  depth: number
}

let _idCounter = 0
function genId(): string {
  _idCounter += 1
  return `f${Date.now().toString(36)}${_idCounter}`
}

/**
 * The tree to read as *geometry*, which is not always the tree to display.
 *
 * A parameter is stored as its expression — `size.x` is the string `"=length"`,
 * because that is the design intent and the inspector has to show it. Anything
 * doing arithmetic on those fields needs the numbers instead, and the server
 * publishes them as `derived.numeric_tree`: the same nodes and the same ids
 * with the expressions substituted. Resolving them here would be a second copy
 * of the evaluator, drifting from the one that produced the body on screen.
 *
 * Falls back to the stored tree, which is correct for the (overwhelmingly
 * common) document with no parameters at all, and for one whose parameters do
 * not currently resolve.
 */
export function geometryTree(doc: {
  feature_tree: FeatureTreeDict
  derived?: { numeric_tree?: FeatureTreeDict | null }
}): FeatureTreeDict {
  return doc.derived?.numeric_tree ?? doc.feature_tree
}

export function isBoolean(node: FeatureNode): boolean {
  return node.type === 'BooleanFeature' || node.type === 'FilletFeature' || node.type === 'ChamferFeature'
}

/** Any node may carry operands under params.children (primitives carry none). */
export function childrenOf(node: FeatureNode): FeatureNode[] {
  return (node.params.children as FeatureNode[] | undefined) ?? []
}

/** Depth-first flatten for the tree panel. Boolean children render nested. */
export function flatten(tree: FeatureTreeDict): FlatNode[] {
  const out: FlatNode[] = []
  const walk = (node: FeatureNode, depth: number) => {
    out.push({ node, depth })
    for (const child of childrenOf(node)) walk(child, depth + 1)
  }
  if (tree?.root) walk(tree.root, 0)
  return out
}

export function findNode(tree: FeatureTreeDict, id: string): FeatureNode | null {
  for (const { node } of flatten(tree)) {
    if (node.id === id) return node
  }
  return null
}

/** Ensure every node has a stable id (backend leaves some null). Returns a new tree. */
export function ensureIds(tree: FeatureTreeDict): FeatureTreeDict {
  const clone = structuredClone(tree)
  const walk = (node: FeatureNode) => {
    if (!node.id) node.id = genId()
    for (const child of childrenOf(node)) walk(child)
  }
  if (clone.root) walk(clone.root)
  return clone
}

/** Immutably replace a node's params (merged) by id. */
export function updateNodeParams(
  tree: FeatureTreeDict,
  id: string,
  params: Record<string, unknown>,
): FeatureTreeDict {
  const clone = structuredClone(tree)
  const walk = (node: FeatureNode): boolean => {
    if (node.id === id) {
      node.params = { ...node.params, ...params }
      return true
    }
    return childrenOf(node).some(walk)
  }
  if (clone.root) walk(clone.root)
  return clone
}

export function renameNode(tree: FeatureTreeDict, id: string, name: string): FeatureTreeDict {
  const clone = structuredClone(tree)
  const walk = (node: FeatureNode): boolean => {
    if (node.id === id) {
      node.name = name
      return true
    }
    return childrenOf(node).some(walk)
  }
  if (clone.root) walk(clone.root)
  return clone
}

const DEFAULT_PARAMS: Record<string, Record<string, unknown>> = {
  BoxFeature: { size: { x: 0.05, y: 0.05, z: 0.05 }, center: { x: 0, y: 0, z: 0 } },
  SphereFeature: { radius: 0.03, center: { x: 0, y: 0, z: 0 } },
  CylinderFeature: { radius: 0.02, height: 0.06, center: { x: 0, y: 0, z: 0 }, axis: { x: 0, y: 0, z: 1 } },
  ConeFeature: { base_radius: 0.03, height: 0.06, center: { x: 0, y: 0, z: 0 }, axis: { x: 0, y: 0, z: 1 } },
}

export type PrimitiveType = keyof typeof DEFAULT_PARAMS

export function makePrimitive(type: PrimitiveType, name?: string): FeatureNode {
  return {
    type,
    id: genId(),
    name: name ?? type.replace('Feature', ''),
    params: structuredClone(DEFAULT_PARAMS[type]),
  }
}

/**
 * Combine a new primitive with the existing root under a boolean op.
 * If the root is already a boolean of the same op, append; otherwise wrap.
 */
export function addPrimitive(
  tree: FeatureTreeDict,
  primitive: FeatureNode,
  op: BooleanOp,
): FeatureTreeDict {
  const clone = structuredClone(tree)
  const root = clone.root
  if (isBoolean(root) && (root.params.operation as BooleanOp) === op) {
    ;(root.params.children as FeatureNode[]).push(primitive)
    return clone
  }
  clone.root = {
    type: 'BooleanFeature',
    id: genId(),
    name: op.charAt(0).toUpperCase() + op.slice(1),
    params: { operation: op, children: [root, primitive] },
  }
  return clone
}

// ── Snap points: exact CAD-meaningful points from the parametric tree ──

type P3 = { x: number; y: number; z: number }

/**
 * Collect snap targets in part space (metres) from the feature tree: primitive
 * centers, box corners/face-centers, cylinder/hole/cone axis ends, sphere poles.
 * Transforms/patterns are not composed (points come from each node's own params),
 * which covers the common boolean-of-primitives parts.
 */
export function collectSnapPoints(tree: FeatureTreeDict): P3[] {
  const pts: P3[] = []
  const add = (x: number, y: number, z: number) => pts.push({ x, y, z })

  const walk = (node: FeatureNode) => {
    const p = node.params as Record<string, P3 | number | undefined>
    const c = (p.center as P3) ?? { x: 0, y: 0, z: 0 }
    const axis = (p.axis as P3) ?? { x: 0, y: 0, z: 1 }

    switch (node.type) {
      case 'BoxFeature': {
        const s = p.size as P3
        const hx = s.x / 2, hy = s.y / 2, hz = s.z / 2
        add(c.x, c.y, c.z)
        for (const sx of [-1, 1]) for (const sy of [-1, 1]) for (const sz of [-1, 1])
          add(c.x + sx * hx, c.y + sy * hy, c.z + sz * hz)
        add(c.x + hx, c.y, c.z); add(c.x - hx, c.y, c.z)
        add(c.x, c.y + hy, c.z); add(c.x, c.y - hy, c.z)
        add(c.x, c.y, c.z + hz); add(c.x, c.y, c.z - hz)
        break
      }
      case 'CylinderFeature':
      case 'HoleFeature': {
        const h = (node.type === 'HoleFeature' ? p.depth : p.height) as number
        add(c.x, c.y, c.z)
        add(c.x + (axis.x * h) / 2, c.y + (axis.y * h) / 2, c.z + (axis.z * h) / 2)
        add(c.x - (axis.x * h) / 2, c.y - (axis.y * h) / 2, c.z - (axis.z * h) / 2)
        break
      }
      case 'ConeFeature': {
        const h = p.height as number
        add(c.x, c.y, c.z)
        add(c.x + (axis.x * h) / 2, c.y + (axis.y * h) / 2, c.z + (axis.z * h) / 2)
        add(c.x - (axis.x * h) / 2, c.y - (axis.y * h) / 2, c.z - (axis.z * h) / 2)
        break
      }
      case 'SphereFeature': {
        const r = p.radius as number
        add(c.x, c.y, c.z)
        add(c.x + r, c.y, c.z); add(c.x - r, c.y, c.z)
        add(c.x, c.y + r, c.z); add(c.x, c.y - r, c.z)
        add(c.x, c.y, c.z + r); add(c.x, c.y, c.z - r)
        break
      }
    }
    for (const ch of childrenOf(node)) walk(ch)
  }
  if (tree?.root) walk(tree.root)

  // Dedupe to ~0.1mm.
  const seen = new Set<string>()
  const out: P3[] = []
  for (const q of pts) {
    const k = `${q.x.toFixed(4)},${q.y.toFixed(4)},${q.z.toFixed(4)}`
    if (!seen.has(k)) {
      seen.add(k)
      out.push(q)
    }
  }
  return out
}

// ── Selection proxies: wireframe stand-ins for a feature's primitives ──

export interface FeatureProxy {
  kind: 'box' | 'sphere' | 'cyl'
  center: P3
  size?: P3
  radius?: number
  height?: number
  axis?: P3
}

/** Collect wireframe proxies for every primitive under a node (for selection highlight). */
export function collectProxies(node: FeatureNode): FeatureProxy[] {
  const out: FeatureProxy[] = []
  const walk = (n: FeatureNode) => {
    const p = n.params as Record<string, P3 | number | undefined>
    const c = (p.center as P3) ?? { x: 0, y: 0, z: 0 }
    const axis = (p.axis as P3) ?? { x: 0, y: 0, z: 1 }
    switch (n.type) {
      case 'BoxFeature':
        out.push({ kind: 'box', center: c, size: p.size as P3 })
        break
      case 'SphereFeature':
        out.push({ kind: 'sphere', center: c, radius: p.radius as number })
        break
      case 'CylinderFeature':
        out.push({ kind: 'cyl', center: c, radius: p.radius as number, height: p.height as number, axis })
        break
      case 'ConeFeature':
        out.push({ kind: 'cyl', center: c, radius: p.base_radius as number, height: p.height as number, axis })
        break
      case 'HoleFeature':
        out.push({ kind: 'cyl', center: c, radius: p.radius as number, height: p.depth as number, axis })
        break
    }
    for (const ch of childrenOf(n)) walk(ch)
  }
  walk(node)
  return out
}

/** Approx distance (part units) from a point to a proxy's surface (0 inside). */
function proxyDistance(px: FeatureProxy, p: P3): number {
  const c = px.center
  if (px.kind === 'box' && px.size) {
    const dx = Math.max(0, Math.abs(p.x - c.x) - px.size.x / 2)
    const dy = Math.max(0, Math.abs(p.y - c.y) - px.size.y / 2)
    const dz = Math.max(0, Math.abs(p.z - c.z) - px.size.z / 2)
    return Math.hypot(dx, dy, dz)
  }
  if (px.kind === 'sphere') {
    return Math.abs(Math.hypot(p.x - c.x, p.y - c.y, p.z - c.z) - (px.radius ?? 0))
  }
  // cylinder / cone / hole: radial distance to the axis segment minus radius.
  const a = px.axis ?? { x: 0, y: 0, z: 1 }
  const alen = Math.hypot(a.x, a.y, a.z) || 1
  const ax = a.x / alen, ay = a.y / alen, az = a.z / alen
  const rx = p.x - c.x, ry = p.y - c.y, rz = p.z - c.z
  const along = rx * ax + ry * ay + rz * az
  const radial = Math.hypot(rx - along * ax, ry - along * ay, rz - along * az) - (px.radius ?? 0)
  const axial = Math.max(0, Math.abs(along) - (px.height ?? 0) / 2)
  return Math.hypot(Math.max(0, radial), axial)
}

/**
 * The id of the primitive feature nearest a part-space point — for clicking a
 * body in the 3D viewport to select the feature that owns that surface.
 */
export function pickFeatureAt(tree: FeatureTreeDict, point: P3): string | null {
  let bestId: string | null = null
  let bestD = Infinity
  for (const { node } of flatten(tree)) {
    const own = collectProxies({ ...node, params: { ...node.params, children: [] } })
    for (const px of own) {
      const d = proxyDistance(px, point)
      if (d < bestD) {
        bestD = d
        bestId = node.id ?? null
      }
    }
  }
  return bestId
}

// ── Modifier features (Phase 2) ──

export type ModifierType =
  | 'FilletFeature'
  | 'ChamferFeature'
  | 'ShellFeature'
  | 'HoleFeature'
  | 'PatternFeature'
  | 'TransformFeature'

const MODIFIER_DEFAULTS: Record<ModifierType, Record<string, unknown>> = {
  FilletFeature: { radius: 0.01, operation: 'union' },
  ChamferFeature: { radius: 0.01, operation: 'union' },
  ShellFeature: { thickness: 0.004 },
  HoleFeature: { radius: 0.005, depth: 0.1, center: { x: 0, y: 0, z: 0 }, axis: { x: 0, y: 0, z: 1 } },
  PatternFeature: {
    kind: 'linear',
    count: 3,
    spacing: 0.05,
    axis: { x: 1, y: 0, z: 0 },
    center: { x: 0, y: 0, z: 0 },
  },
  TransformFeature: {
    translate: { x: 0.05, y: 0, z: 0 },
    rotate: { x: 0, y: 0, z: 0 },
    scale: 1,
  },
}

/**
 * Apply a modifier to the whole body (wrapping the current root).
 * Fillet/Chamfer convert an existing boolean root into a smooth blend of the
 * same children; otherwise the modifier simply wraps the root as its child.
 */
export function applyModifier(tree: FeatureTreeDict, type: ModifierType): FeatureTreeDict {
  const clone = structuredClone(tree)
  const root = clone.root
  const base = MODIFIER_DEFAULTS[type]
  const label = type.replace('Feature', '')

  if ((type === 'FilletFeature' || type === 'ChamferFeature') && isBoolean(root) && childrenOf(root).length >= 2) {
    clone.root = {
      type,
      id: genId(),
      name: label,
      params: { ...base, operation: root.params.operation ?? 'union', children: childrenOf(root) },
    }
    return clone
  }

  clone.root = {
    type,
    id: genId(),
    name: label,
    params: { ...structuredClone(base), children: [root] },
  }
  return clone
}

export function isSuppressed(node: FeatureNode): boolean {
  return node.suppressed === true
}

/** Toggle a feature's suppressed flag (skip it during regeneration). */
export function setSuppressed(tree: FeatureTreeDict, id: string, suppressed: boolean): FeatureTreeDict {
  const clone = structuredClone(tree)
  const walk = (node: FeatureNode): boolean => {
    if (node.id === id) {
      if (suppressed) node.suppressed = true
      else delete node.suppressed
      return true
    }
    return childrenOf(node).some(walk)
  }
  if (clone.root) walk(clone.root)
  return clone
}

/** Move a feature up/down within its parent's children. */
export function reorderNode(tree: FeatureTreeDict, id: string, dir: 'up' | 'down'): FeatureTreeDict {
  const clone = structuredClone(tree)
  const delta = dir === 'up' ? -1 : 1
  const walk = (node: FeatureNode): boolean => {
    const kids = childrenOf(node)
    for (let i = 0; i < kids.length; i++) {
      if (kids[i].id === id) {
        const j = i + delta
        if (j >= 0 && j < kids.length) {
          ;[kids[i], kids[j]] = [kids[j], kids[i]]
        }
        return true
      }
    }
    return kids.some(walk)
  }
  if (clone.root) walk(clone.root)
  return clone
}

/** Remove a node by id. Collapses a boolean that drops to a single child. */
export function removeNode(tree: FeatureTreeDict, id: string): FeatureTreeDict {
  const clone = structuredClone(tree)
  const prune = (node: FeatureNode): FeatureNode => {
    if (!isBoolean(node)) return node
    let kids = (node.params.children as FeatureNode[]).filter((c) => c.id !== id).map(prune)
    node.params.children = kids
    if (kids.length === 1) return kids[0]
    return node
  }
  if (clone.root) {
    if (clone.root.id === id) {
      // deleting the whole root — fall back to an empty box so eval stays valid
      clone.root = makePrimitive('BoxFeature', 'Base Box')
    } else {
      clone.root = prune(clone.root)
    }
  }
  return clone
}

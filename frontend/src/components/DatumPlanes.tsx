import { useMemo } from 'react'
import { Line } from '@react-three/drei'
import { flatten } from '../util/featureTree'
import type { FeatureNode, FeatureTreeDict } from '../../../shared/types'

/**
 * The document's datum planes, drawn.
 *
 * A new document is three reference planes and no body, which is what every CAD
 * package opens on and what replaced the 100 mm placeholder box. Without
 * drawing them the new-document viewport is an empty grid, which is worse than
 * the block it replaced: the block at least told you the app had loaded.
 *
 * Three decisions:
 *
 * - **The outline carries them, not the fill.** A plane is a reference, not a
 *   face, and a solid translucent quad reads as a thin sheet of material — the
 *   exact confusion the feature exists to avoid. The fill is barely there and
 *   the border is what you see.
 * - **They are pickable and they are the sketch-plane picker.** Clicking one
 *   while a sketch is being placed chooses it, which is the same call the
 *   XY/XZ/YZ buttons make; otherwise it selects the feature in the tree, so a
 *   plane can be deleted from the thing you are looking at.
 * - **They scale with the part when there is one.** `fit` normalises the body
 *   to ~1.5 display units; a plane drawn at a fixed size would swallow a small
 *   part and vanish inside a large one. With no body there is no `fit`, so the
 *   plane's own `size` sets the scale — that is the blank-document case and the
 *   only time the number matters.
 */

type Vec3 = [number, number, number]

// A `planeGeometry` lies in XY with its normal along +Z, so the XY datum needs
// no rotation and the other two are quarter turns onto their own normals.
const ROTATION: Record<string, Vec3> = {
  xy: [0, 0, 0],
  xz: [-Math.PI / 2, 0, 0],
  yz: [0, Math.PI / 2, 0],
}

// Distinct enough to name a plane at a glance without competing with the
// accent colour the selection already owns.
const COLOUR: Record<string, string> = {
  xy: '#5cc9a7',
  xz: '#e8a63c',
  yz: '#6b8fd4',
}

/** The offset moves the plane along its own normal, in part space. */
function offsetPoint(plane: string, offset: number): Vec3 {
  if (plane === 'xz') return [0, offset, 0]
  if (plane === 'yz') return [offset, 0, 0]
  return [0, 0, offset]
}

export type PlaneFit = {
  s: number
  toDisplay: (p: { x: number; y: number; z: number }) => Vec3
}

export function planesOf(tree: FeatureTreeDict | undefined): FeatureNode[] {
  if (!tree) return []
  return flatten(tree)
    .map(({ node }) => node)
    .filter((n) => n.type === 'DatumPlaneFeature')
}

function DatumPlane({
  node, scale, toDisplay, selected, onClick,
}: {
  node: FeatureNode
  scale: number
  toDisplay: (p: { x: number; y: number; z: number }) => Vec3
  selected: boolean
  onClick: () => void
}) {
  const plane = String(node.params.plane ?? 'xy')
  const offset = Number(node.params.offset ?? 0)
  const size = Number(node.params.size ?? 0.12) * scale
  const [x, y, z] = offsetPoint(plane, offset)
  const position = toDisplay({ x, y, z })
  const colour = selected ? '#ffb86b' : (COLOUR[plane] ?? '#6b8fd4')
  const half = size / 2

  // The border, in the plane's own local frame — the group's rotation puts it
  // where the quad is, so the two can never disagree.
  const border: Vec3[] = [
    [-half, -half, 0], [half, -half, 0], [half, half, 0], [-half, half, 0], [-half, -half, 0],
  ]

  return (
    <group position={position} rotation={ROTATION[plane] ?? ROTATION.xy}>
      <mesh
        onClick={(e) => { e.stopPropagation(); onClick() }}
        onPointerOver={(e) => { e.stopPropagation() }}
      >
        <planeGeometry args={[size, size]} />
        <meshBasicMaterial
          color={colour}
          transparent
          opacity={selected ? 0.1 : 0.045}
          side={2}          // THREE.DoubleSide — a plane has no back
          depthWrite={false}
        />
      </mesh>
      <Line points={border} color={colour} lineWidth={selected ? 1.6 : 1} transparent opacity={0.65} />
    </group>
  )
}

export function DatumPlanes({
  tree, fit, selectedId, onSelect,
}: {
  tree: FeatureTreeDict | undefined
  fit: PlaneFit | null
  selectedId: string | null
  onSelect: (id: string) => void
}) {
  const planes = useMemo(() => planesOf(tree), [tree])
  if (!planes.length) return null

  // With no body there is no fit, so the largest plane sets the scale and the
  // origin is the origin. `1.5` is the same display size `useFit` normalises a
  // part to, so a blank document and a modelled one frame identically.
  const fallbackSize = Math.max(...planes.map((p) => Number(p.params.size ?? 0.12)), 1e-6)
  const scale = fit ? fit.s : 1.5 / fallbackSize
  const toDisplay = fit
    ? fit.toDisplay
    : (p: { x: number; y: number; z: number }): Vec3 => [p.x * scale, p.y * scale, p.z * scale]

  return (
    <>
      {planes.map((node) => (
        <DatumPlane
          key={node.id}
          node={node}
          scale={scale}
          toDisplay={toDisplay}
          selected={node.id === selectedId}
          onClick={() => node.id && onSelect(node.id)}
        />
      ))}
    </>
  )
}

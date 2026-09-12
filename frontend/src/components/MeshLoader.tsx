import { useRef, useEffect, useMemo } from 'react'
import { useLoader } from '@react-three/fiber'
import * as THREE from 'three'
import { CadMaterial } from './CadAppearance'
import { ForgeMeshLoader, faceOfTriangle, facesSubGeometry, ownerOfTriangle } from '../util/forgeMesh'

/**
 * A highlighted face, drawn over the body.
 *
 * `polygonOffset` rather than `depthTest={false}`: turning the depth test off
 * was the first attempt and it draws the selected face *through* the part, so
 * selecting the back face of a box lights up the front of it. Nudging the
 * overlay a fraction toward the camera keeps the depth test — so a hidden face
 * stays hidden — while still winning the z-fight against the surface it sits on.
 *
 * `FrontSide`, for the same reason: the inside of the far wall is not the face
 * you picked.
 */
function FaceOverlay({ geometry, colour, opacity }: {
  geometry: THREE.BufferGeometry; colour: string; opacity: number
}) {
  return (
    <mesh geometry={geometry} renderOrder={1}>
      <meshBasicMaterial
        color={colour}
        transparent
        opacity={opacity}
        side={THREE.FrontSide}
        polygonOffset
        polygonOffsetFactor={-2}
        polygonOffsetUnits={-2}
      />
    </mesh>
  )
}

// Map a world-space face normal to one of the six datum faces. Mirrors the
// backend datum FACE_MAP: +z top / -z bottom, +x right / -x left, +y back / -y front.
function normalToFace(n: THREE.Vector3): string {
  const ax = Math.abs(n.x), ay = Math.abs(n.y), az = Math.abs(n.z)
  if (az >= ax && az >= ay) return n.z >= 0 ? 'top' : 'bottom'
  if (ax >= ay) return n.x >= 0 ? 'right' : 'left'
  return n.y >= 0 ? 'back' : 'front'
}

export function MeshLoader({
  url,
  onPick,
  onPickFace,
  onSelect,
  onPickTopology,
  onHoverTopology,
  highlight,
  dim,
  fit,
}: {
  url: string
  onPick?: (point: [number, number, number]) => void
  onPickFace?: (face: string, point: [number, number, number]) => void
  /** Plain click on the body (no measure/sketch active) — for feature selection.
   * `feature` is the id the triangle came from when the mesh carries provenance;
   * null means the caller has to fall back to its own guess. */
  onSelect?: (point: [number, number, number], feature: string | null) => void
  /** Topology picking is live: click a face to select it for fillet/chamfer/
   * shell. `additive` is a shift-click. A null id means this body has no
   * nameable faces — the caller falls back to the list rather than guessing. */
  onPickTopology?: (faceId: string | null, additive: boolean,
                    point: [number, number, number]) => void
  /** The face under the cursor, as it moves. Separate from the click on
   * purpose: a hover that selected would make the picker unusable. */
  onHoverTopology?: (faceId: string | null) => void
  /** Face ids to draw as selected, plus the one under the cursor. */
  highlight?: { selected: string[]; hovered?: string | null }
  dim?: boolean
  /** Frozen display transform (centre in part metres + scale). When given, the
   * mesh uses it instead of self-fitting, so edits never rescale the view. */
  fit?: { c: [number, number, number]; s: number }
}) {
  const loadedGeometry = useLoader(ForgeMeshLoader, url)
  const meshRef = useRef<THREE.Mesh>(null)
  const geometry = useMemo(() => {
    const g = loadedGeometry.clone()
    if (fit) g.translate(-fit.c[0], -fit.c[1], -fit.c[2])
    return g
    // eslint-disable-next-line react-hooks/exhaustive-deps -- fit identity churn
  }, [loadedGeometry, fit?.c[0], fit?.c[1], fit?.c[2]])

  // `useLoader`'s suspense cache is keyed by URL and never evicts, so without
  // this every mesh version of a session stays resident. Less dramatic now the
  // URL is a hash rather than megabytes of base64 — but the parsed geometry and
  // its GPU buffers are still real, so free them when the url changes.
  useEffect(() => {
    return () => {
      useLoader.clear(ForgeMeshLoader, url)
      geometry.dispose()
    }
  }, [url, geometry])

  useEffect(() => {
    if (!geometry || !meshRef.current) return
    if (fit) {
      // Frozen document fit: geometry is pre-translated; just apply the scale.
      meshRef.current.scale.setScalar(fit.s)
      return
    }
    // Legacy self-fit (candidate/assembly viewers without a document fit).
    geometry.center()
    const box = new THREE.Box3().setFromObject(meshRef.current)
    const size = box.getSize(new THREE.Vector3())
    const maxDim = Math.max(size.x, size.y, size.z)
    if (maxDim > 0) {
      const scale = 1.5 / maxDim
      meshRef.current.scale.setScalar(scale)
    }
  }, [geometry, fit])

  // The overlay geometries: what is selected, and what the cursor is over.
  // Rebuilt only when the ids change — a hover that lands on the same face is
  // the common case and must not allocate.
  const selectedKey = (highlight?.selected ?? []).join(',')
  const hovered = highlight?.hovered ?? null
  const selectedGeometry = useMemo(
    () => facesSubGeometry(geometry, selectedKey ? selectedKey.split(',') : []),
    [geometry, selectedKey])
  const hoveredGeometry = useMemo(
    // A hover over something already selected draws nothing: the selection
    // colour is the stronger one and flickering between them reads as a bug.
    () => (hovered && !selectedKey.split(',').includes(hovered)
      ? facesSubGeometry(geometry, [hovered]) : null),
    [geometry, hovered, selectedKey])

  return (
    <mesh
      ref={meshRef}
      onPointerMove={
        onHoverTopology
          ? (e) => {
              e.stopPropagation()
              onHoverTopology(faceOfTriangle(geometry, e.faceIndex))
            }
          : undefined
      }
      onPointerOut={onHoverTopology ? () => onHoverTopology(null) : undefined}
      onClick={
        onPick || onPickFace || onSelect || onPickTopology
          ? (e) => {
              e.stopPropagation()
              const pt: [number, number, number] = [e.point.x, e.point.y, e.point.z]
              if (onPickTopology) {
                const native = e.nativeEvent as MouseEvent
                onPickTopology(faceOfTriangle(geometry, e.faceIndex),
                               native.shiftKey || native.metaKey || native.ctrlKey, pt)
              } else if (onPickFace && e.face) {
                // World-space normal (uniform scale + no rotation → direction preserved).
                const n = e.face.normal.clone().transformDirection(e.object.matrixWorld).normalize()
                onPickFace(normalToFace(n), pt)
              } else if (onPick) {
                onPick(pt)
              } else if (onSelect) {
                onSelect(pt, ownerOfTriangle(geometry, e.faceIndex))
              }
            }
          : undefined
      }
    >
      <primitive object={geometry} attach="geometry" />
      <CadMaterial dim={dim} />
      {/* Drawn as children so they inherit the mesh's scale and translation:
          the overlay shares the parent's position buffer, so any transform
          applied to one has to apply to the other or they separate. */}
      {hoveredGeometry && <FaceOverlay geometry={hoveredGeometry} colour="#ffb86b" opacity={0.4} />}
      {selectedGeometry && <FaceOverlay geometry={selectedGeometry} colour="#e86a3c" opacity={0.6} />}
    </mesh>
  )
}

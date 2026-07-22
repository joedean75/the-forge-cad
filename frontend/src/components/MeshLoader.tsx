import { useRef, useEffect, useMemo } from 'react'
import { useLoader } from '@react-three/fiber'
import * as THREE from 'three'
import { STLLoader } from 'three/examples/jsm/loaders/STLLoader.js'

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
  dim,
  fit,
}: {
  url: string
  onPick?: (point: [number, number, number]) => void
  onPickFace?: (face: string, point: [number, number, number]) => void
  /** Plain click on the body (no measure/sketch active) — for feature selection. */
  onSelect?: (point: [number, number, number]) => void
  dim?: boolean
  /** Frozen display transform (centre in part metres + scale). When given, the
   * mesh uses it instead of self-fitting, so edits never rescale the view. */
  fit?: { c: [number, number, number]; s: number }
}) {
  const loadedGeometry = useLoader(STLLoader, url)
  const meshRef = useRef<THREE.Mesh>(null)
  const geometry = useMemo(() => {
    const g = loadedGeometry.clone()
    if (fit) g.translate(-fit.c[0], -fit.c[1], -fit.c[2])
    return g
    // eslint-disable-next-line react-hooks/exhaustive-deps -- fit identity churn
  }, [loadedGeometry, fit?.c[0], fit?.c[1], fit?.c[2]])

  // Every edit produces a new multi-MB data-URI, and useLoader's suspense
  // cache is keyed by that string — without eviction, every mesh version of a
  // session (URI + parsed geometry) stays resident. Evict the entry and free
  // the GPU buffers as soon as the url changes or the component unmounts.
  useEffect(() => {
    return () => {
      useLoader.clear(STLLoader, url)
      geometry.dispose()
    }
  }, [url, geometry])

  useEffect(() => {
    if (!geometry || !meshRef.current) return
    geometry.computeVertexNormals()
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

  return (
    <mesh
      ref={meshRef}
      castShadow
      receiveShadow
      onClick={
        onPick || onPickFace || onSelect
          ? (e) => {
              e.stopPropagation()
              const pt: [number, number, number] = [e.point.x, e.point.y, e.point.z]
              if (onPickFace && e.face) {
                // World-space normal (uniform scale + no rotation → direction preserved).
                const n = e.face.normal.clone().transformDirection(e.object.matrixWorld).normalize()
                onPickFace(normalToFace(n), pt)
              } else if (onPick) {
                onPick(pt)
              } else if (onSelect) {
                onSelect(pt)
              }
            }
          : undefined
      }
    >
      <primitive object={geometry} attach="geometry" />
      <meshStandardMaterial
        color="#c2ccd8"
        metalness={0.25}
        roughness={0.55}
        emissive="#e8683c"
        emissiveIntensity={dim ? 0 : 0.05}
        transparent={dim}
        opacity={dim ? 0.16 : 1}
        flatShading={false}
      />
    </mesh>
  )
}

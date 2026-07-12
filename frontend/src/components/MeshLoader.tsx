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
  dim,
}: {
  url: string
  onPick?: (point: [number, number, number]) => void
  onPickFace?: (face: string, point: [number, number, number]) => void
  dim?: boolean
}) {
  const loadedGeometry = useLoader(STLLoader, url)
  const meshRef = useRef<THREE.Mesh>(null)
  const geometry = useMemo(() => loadedGeometry.clone(), [loadedGeometry])

  useEffect(() => {
    if (geometry && meshRef.current) {
      geometry.computeVertexNormals()
      geometry.center()
      const box = new THREE.Box3().setFromObject(meshRef.current)
      const size = box.getSize(new THREE.Vector3())
      const maxDim = Math.max(size.x, size.y, size.z)
      if (maxDim > 0) {
        const scale = 1.5 / maxDim
        meshRef.current.scale.setScalar(scale)
      }
    }
  }, [geometry])

  return (
    <mesh
      ref={meshRef}
      castShadow
      receiveShadow
      onClick={
        onPick || onPickFace
          ? (e) => {
              e.stopPropagation()
              const pt: [number, number, number] = [e.point.x, e.point.y, e.point.z]
              if (onPickFace && e.face) {
                // World-space normal (uniform scale + no rotation → direction preserved).
                const n = e.face.normal.clone().transformDirection(e.object.matrixWorld).normalize()
                onPickFace(normalToFace(n), pt)
              } else if (onPick) {
                onPick(pt)
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

import { useRef, useEffect, useMemo } from 'react'
import { useLoader } from '@react-three/fiber'
import * as THREE from 'three'
import { STLLoader } from 'three/examples/jsm/loaders/STLLoader.js'

export function MeshLoader({
  url,
  onPick,
  dim,
}: {
  url: string
  onPick?: (point: [number, number, number]) => void
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
        onPick
          ? (e) => {
              e.stopPropagation()
              onPick([e.point.x, e.point.y, e.point.z])
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

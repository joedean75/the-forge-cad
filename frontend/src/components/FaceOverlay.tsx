import { useEffect } from 'react'
import * as THREE from 'three'

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
export function FaceOverlay({ geometry, colour, opacity }: {
  geometry: THREE.BufferGeometry; colour: string; opacity: number
}) {
  useEffect(() => () => geometry.dispose(), [geometry])
  return (
    <mesh raycast={() => null} renderOrder={1}>
      <primitive object={geometry} attach="geometry" />
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


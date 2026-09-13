import * as THREE from 'three'

/** Prepare a private display clone using local bounds, independent of any
 * previous mesh transform. A supplied centre keeps document framing stable. */
export function prepareDisplayGeometry(
  source: THREE.BufferGeometry,
  centre?: readonly [number, number, number],
): { geometry: THREE.BufferGeometry; scale: number } {
  const geometry = source.clone()
  geometry.computeBoundingBox()
  const bounds = geometry.boundingBox!
  const size = bounds.getSize(new THREE.Vector3())
  const maxDimension = Math.max(size.x, size.y, size.z)
  const c = centre ? new THREE.Vector3(...centre) : bounds.getCenter(new THREE.Vector3())
  geometry.translate(-c.x, -c.y, -c.z)
  return { geometry, scale: maxDimension > 0 ? 1.5 / maxDimension : 1 }
}

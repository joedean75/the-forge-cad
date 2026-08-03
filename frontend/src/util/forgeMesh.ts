import * as THREE from 'three'
import { authHeaders } from '../api/client'

/**
 * The mesh transport, matching `cad_kernel/io/mesh_binary.py`.
 *
 *     magic   "FMSH"   4 bytes
 *     version uint32   1
 *     counts  uint32 x 2
 *     coords  float32 x 3V
 *     faces   uint32  x 3F
 *
 * Indexed, and with no normals — the mesher already knows which vertices are
 * shared, and three.js recomputes normals on load regardless. STL threw both
 * away: 872 KB of triangle soup where these are 314 KB.
 */

const MAGIC = 0x48534d46 // "FMSH" read little-endian as a uint32
const HEADER = 16

export type ForgeMesh = { positions: Float32Array; indices: Uint32Array }

export function parseForgeMesh(buffer: ArrayBuffer): ForgeMesh {
  const view = new DataView(buffer)
  if (buffer.byteLength < HEADER || view.getUint32(0, true) !== MAGIC) {
    throw new Error('not a Forge mesh')
  }
  const version = view.getUint32(4, true)
  if (version !== 1) throw new Error(`unsupported Forge mesh version ${version}`)
  const vertexCount = view.getUint32(8, true)
  const faceCount = view.getUint32(12, true)
  const coordsEnd = HEADER + vertexCount * 12
  return {
    positions: new Float32Array(buffer, HEADER, vertexCount * 3),
    indices: new Uint32Array(buffer, coordsEnd, faceCount * 3),
  }
}

/**
 * Fetch a mesh URL, re-reading the document once if the hash has moved on.
 *
 * A mesh URL names a *snapshot*: `/api/documents/{id}/mesh/{hash}`. That is what
 * makes it cacheable forever, and it also means a URL can stop being current
 * while it is in flight — two edits landing close together, or another session
 * editing the same document. The server answers 404 rather than serving
 * different triangles under a hash it has promised is immutable, so recovering
 * is the client's job: ask the document where its geometry is now, and try that.
 * Once, not in a loop — a second 404 is a real failure, not a race.
 */
export async function fetchForgeMesh(url: string): Promise<ForgeMesh> {
  const res = await fetch(url, { headers: authHeaders() })
  if (res.ok) return parseForgeMesh(await res.arrayBuffer())
  if (res.status !== 404) throw new Error(`mesh ${res.status}`)

  const docUrl = url.replace(/\/mesh\/[^/]+$/, '')
  const doc = await fetch(docUrl, { headers: authHeaders() })
  if (!doc.ok) throw new Error(`mesh 404, and the document is ${doc.status}`)
  const current: string | undefined = (await doc.json())?.derived?.mesh_url
  if (!current || current === url) throw new Error('mesh 404')

  const retry = await fetch(current, { headers: authHeaders() })
  if (!retry.ok) throw new Error(`mesh ${retry.status}`)
  return parseForgeMesh(await retry.arrayBuffer())
}

export function toGeometry(mesh: ForgeMesh): THREE.BufferGeometry {
  const geometry = new THREE.BufferGeometry()
  geometry.setAttribute('position', new THREE.BufferAttribute(mesh.positions, 3))
  geometry.setIndex(new THREE.BufferAttribute(mesh.indices, 1))
  geometry.computeVertexNormals()
  return geometry
}

/**
 * A three.js `Loader` over the format, so `useLoader` keeps working.
 *
 * `useLoader` caches by URL and suspends on the promise; reimplementing that
 * with an effect would mean re-solving suspense, eviction and the race between
 * a new URL arriving and the old parse finishing.
 */
export class ForgeMeshLoader extends THREE.Loader<THREE.BufferGeometry> {
  load(
    url: string,
    onLoad: (geometry: THREE.BufferGeometry) => void,
    _onProgress?: (event: ProgressEvent) => void,
    onError?: (err: unknown) => void,
  ) {
    fetchForgeMesh(url)
      .then((mesh) => onLoad(toGeometry(mesh)))
      .catch((err) => (onError ? onError(err) : console.error(err)))
  }
}

/** Iterate a mesh's triangles as flat vertex coordinates, without expanding it. */
export function forEachTriangle(
  mesh: ForgeMesh,
  visit: (ax: number, ay: number, az: number,
          bx: number, by: number, bz: number,
          cx: number, cy: number, cz: number) => void,
  stride = 1,
): void {
  const { positions, indices } = mesh
  for (let f = 0; f + 2 < indices.length; f += 3 * stride) {
    const a = indices[f] * 3, b = indices[f + 1] * 3, c = indices[f + 2] * 3
    visit(positions[a], positions[a + 1], positions[a + 2],
          positions[b], positions[b + 1], positions[b + 2],
          positions[c], positions[c + 1], positions[c + 2])
  }
}

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
const NO_LABEL = 0xffffffff

export type ForgeMesh = {
  positions: Float32Array
  indices: Uint32Array
  /** One feature id per triangle, or null where the body cannot be attributed. */
  owners: (string | null)[] | null
  /** One face id per triangle — the same id `list_faces` hands out and
   *  `fillet_edges` / `shell_faces` / `on_face` take. Null on a marching-cubes
   *  body, which has exactly one face and so cannot name any of them. */
  faceIds: (string | null)[] | null
}

/** One label block: `(count, tableBytes, indices, table)`. Returns the labels
 *  and where the next block starts, or nulls when the block is absent. */
function readLabels(
  buffer: ArrayBuffer, view: DataView, at: number,
): { labels: (string | null)[] | null; next: number } {
  if (buffer.byteLength < at + 8) return { labels: null, next: at }
  const count = view.getUint32(at, true)
  const tableBytes = view.getUint32(at + 4, true)
  const body = at + 8
  if (!count) return { labels: null, next: body }
  const end = body + count * 4
  if (buffer.byteLength < end + tableBytes) return { labels: null, next: at }
  const packed = new Uint32Array(buffer, body, count)
  const table: string[] = JSON.parse(
    new TextDecoder().decode(new Uint8Array(buffer, end, tableBytes)),
  )
  return {
    labels: Array.from(packed, (slot) =>
      slot === NO_LABEL || slot >= table.length ? null : table[slot]),
    next: end + tableBytes,
  }
}

export function parseForgeMesh(buffer: ArrayBuffer): ForgeMesh {
  const view = new DataView(buffer)
  if (buffer.byteLength < HEADER || view.getUint32(0, true) !== MAGIC) {
    throw new Error('not a Forge mesh')
  }
  const version = view.getUint32(4, true)
  if (version < 1 || version > 3) {
    throw new Error(`unsupported Forge mesh version ${version}`)
  }
  const vertexCount = view.getUint32(8, true)
  const faceCount = view.getUint32(12, true)
  const coordsEnd = HEADER + vertexCount * 12
  const facesEnd = coordsEnd + faceCount * 12
  const mesh: ForgeMesh = {
    positions: new Float32Array(buffer, HEADER, vertexCount * 3),
    indices: new Uint32Array(buffer, coordsEnd, faceCount * 3),
    owners: null,
    faceIds: null,
  }
  // v1 had no labels at all; v2 had owners only; a body the kernel could not
  // attribute writes a zero-length block. Every one of those means "select in
  // the tree, or from the list" rather than an error — a label is a
  // convenience, geometry is not.
  if (version < 2) return mesh

  const owners = readLabels(buffer, view, facesEnd)
  mesh.owners = owners.labels
  if (version < 3) return mesh
  mesh.faceIds = readLabels(buffer, view, owners.next).labels
  return mesh
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
  // The transport is welded for topology/analysis. Rendering must split a
  // shared vertex at CAD face boundaries, otherwise a box's corner averages
  // three perpendicular normals and puts curved shading on its flat faces.
  // Within each face vertices remain shared, so cylindrical surfaces stay
  // smooth. Triangle order and the original mesh are never changed.
  let positions = mesh.positions
  let indices = mesh.indices
  const labels = mesh.faceIds
  if (labels?.length === indices.length / 3 && labels.some((id) => id !== null)) {
    const vertices: number[] = []
    const splitIndices = new Uint32Array(indices.length)
    const byFace = new Map<string | number, Map<number, number>>()
    for (let t = 0; t < labels.length; t++) {
      // Unlabelled triangles cannot safely be assigned to a neighbouring face.
      const key = labels[t] ?? t
      let face = byFace.get(key)
      if (!face) { face = new Map(); byFace.set(key, face) }
      for (let corner = 0; corner < 3; corner++) {
        const source = indices[t * 3 + corner]
        let target = face.get(source)
        if (target === undefined) {
          target = vertices.length / 3
          face.set(source, target)
          vertices.push(positions[source * 3], positions[source * 3 + 1], positions[source * 3 + 2])
        }
        splitIndices[t * 3 + corner] = target
      }
    }
    positions = new Float32Array(vertices)
    indices = splitIndices
  }
  geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3))
  geometry.setIndex(new THREE.BufferAttribute(indices, 1))
  geometry.computeVertexNormals()
  // Carried on the geometry so a raycast hit — which reports a triangle index
  // against this exact buffer — can be turned into a feature id without the
  // click handler having to hold the parse result separately. `clone()` copies
  // userData, so the per-mesh translate in `MeshLoader` keeps it.
  geometry.userData.owners = mesh.owners
  geometry.userData.faceIds = mesh.faceIds
  return geometry
}

/** The face a raycast-hit triangle belongs to, if the body has nameable faces.
 *
 *  Null is a real answer and the caller must treat it as one: a marching-cubes
 *  body has exactly one face and cannot name it, so the picker list is the only
 *  way in. A guess here would not merely highlight the wrong patch — the id
 *  feeds `fillet_edges`, so it would cut the wrong geometry. */
export function faceOfTriangle(
  geometry: THREE.BufferGeometry | undefined,
  faceIndex: number | null | undefined,
): string | null {
  const ids = geometry?.userData?.faceIds as (string | null)[] | null | undefined
  if (!ids || faceIndex == null) return null
  return ids[faceIndex] ?? null
}

/** A geometry of only the triangles belonging to `ids` — the highlight overlay.
 *
 *  Built from the index buffer of the mesh already on the GPU rather than
 *  fetched: the labels are in the browser, so a hover costs a filter over the
 *  triangle array and no network at all. */
export function facesSubGeometry(
  geometry: THREE.BufferGeometry | undefined,
  ids: Iterable<string>,
): THREE.BufferGeometry | null {
  const labels = geometry?.userData?.faceIds as (string | null)[] | null | undefined
  const index = geometry?.getIndex()
  if (!geometry || !labels || !index) return null
  const wanted = new Set(ids)
  if (!wanted.size) return null

  const kept: number[] = []
  const positions: number[] = []
  const remap = new Map<number, number>()
  const source = geometry.getAttribute('position')
  for (let t = 0; t < labels.length; t++) {
    const id = labels[t]
    if (!id || !wanted.has(id)) continue
    for (let corner = 0; corner < 3; corner++) {
      const original = index.getX(t * 3 + corner)
      let local = remap.get(original)
      if (local === undefined) {
        local = positions.length / 3
        remap.set(original, local)
        positions.push(source.getX(original), source.getY(original), source.getZ(original))
      }
      kept.push(local)
    }
  }
  if (!kept.length) return null
  const out = new THREE.BufferGeometry()
  // Own only the selected vertices. Disposing a highlight must not release
  // the body's shared GPU position buffer.
  out.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3))
  out.setIndex(kept)
  // Highlight overlays use an unlit material and do not need normals.
  return out
}

/** The feature that made a raycast-hit triangle, if the body could be attributed. */
export function ownerOfTriangle(
  geometry: THREE.BufferGeometry | undefined,
  faceIndex: number | null | undefined,
): string | null {
  const owners = geometry?.userData?.owners as (string | null)[] | null | undefined
  if (!owners || faceIndex == null) return null
  return owners[faceIndex] ?? null
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

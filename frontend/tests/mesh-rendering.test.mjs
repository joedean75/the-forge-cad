import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import ts from 'typescript'
import * as THREE from 'three'

const require = createRequire(import.meta.url)
const source = readFileSync(new URL('../src/util/forgeMesh.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText
const loaded = { exports: {} }
new Function('require', 'module', 'exports', compiled)(
  (id) => id === '../api/client' ? { authHeaders: () => ({}) } : require(id),
  loaded, loaded.exports,
)
const { toGeometry, faceOfTriangle, ownerOfTriangle, facesSubGeometry } = loaded.exports

// Mimic the backend's welded transport, retaining CAD face groups.
function welded(geometry) {
  const points = geometry.getAttribute('position')
  const index = geometry.getIndex()
  const vertices = [], indices = [], faceIds = []
  const known = new Map()
  for (let i = 0; i < index.count; i++) {
    const point = new THREE.Vector3().fromBufferAttribute(points, index.getX(i))
    const coords = point.toArray()
    const key = coords.map((n) => Math.round(n * 1e8)).join(',')
    if (!known.has(key)) {
      known.set(key, vertices.length / 3)
      vertices.push(...coords)
    }
    indices.push(known.get(key))
    if (i % 3 === 0) {
      const group = geometry.groups.find((g) => i >= g.start && i < g.start + g.count)
      faceIds.push(String(group.materialIndex))
    }
  }
  geometry.dispose()
  return {
    positions: new Float32Array(vertices), indices: new Uint32Array(indices),
    faceIds, owners: faceIds.map((id) => 'feature-' + id),
  }
}

function corner(geometry, triangle, offset, attribute = 'position') {
  return new THREE.Vector3().fromBufferAttribute(
    geometry.getAttribute(attribute), geometry.getIndex().getX(triangle * 3 + offset),
  )
}

describe('CAD rendering normals', () => {
  it('keeps welded box faces planar up to every sharp edge', () => {
    const mesh = welded(new THREE.BoxGeometry(2, 2, 2))
    assert.equal(mesh.positions.length / 3, 8)
    const geometry = toGeometry(mesh)
    assert.equal(geometry.getAttribute('position').count, 24)
    for (let t = 0; t < mesh.indices.length / 3; t++) {
      const a = corner(geometry, t, 0), b = corner(geometry, t, 1), c = corner(geometry, t, 2)
      const expected = b.sub(a).cross(c.sub(a)).normalize()
      for (let k = 0; k < 3; k++) {
        assert.ok(corner(geometry, t, k, 'normal').dot(expected) > 0.99999)
      }
    }
    geometry.dispose()
  })

  it('smooths cylindrical sides while keeping the end caps flat', () => {
    const mesh = welded(new THREE.CylinderGeometry(1, 1, 2, 32))
    const geometry = toGeometry(mesh)
    for (let t = 0; t < mesh.faceIds.length; t++) {
      for (let k = 0; k < 3; k++) {
        const normal = corner(geometry, t, k, 'normal')
        if (mesh.faceIds[t] === '0') {
          const radial = corner(geometry, t, k)
          radial.y = 0
          assert.ok(normal.dot(radial.normalize()) > 0.99)
          assert.ok(Math.abs(normal.y) < 1e-6)
        } else {
          assert.ok(normal.dot(new THREE.Vector3(0, mesh.faceIds[t] === '1' ? 1 : -1, 0)) > 0.99999)
        }
      }
    }
    geometry.dispose()
  })

  it('preserves source coordinates, winding, picking IDs and face highlights', () => {
    const mesh = welded(new THREE.BoxGeometry(2, 3, 4))
    const positions = mesh.positions.slice(), indices = mesh.indices.slice()
    const geometry = toGeometry(mesh)
    assert.deepEqual(mesh.positions, positions)
    assert.deepEqual(mesh.indices, indices)
    for (let t = 0; t < mesh.faceIds.length; t++) {
      assert.equal(faceOfTriangle(geometry, t), mesh.faceIds[t])
      assert.equal(ownerOfTriangle(geometry, t), mesh.owners[t])
      for (let k = 0; k < 3; k++) {
        const i = indices[t * 3 + k] * 3
        assert.deepEqual(corner(geometry, t, k).toArray(), Array.from(positions.slice(i, i + 3)))
      }
    }
    const highlight = facesSubGeometry(geometry, ['0'])
    assert.equal(highlight.getIndex().count, 6)
    highlight.dispose()
    geometry.dispose()
  })

  it('retains shared smoothing for meshes without CAD face labels', () => {
    const mesh = welded(new THREE.BoxGeometry())
    mesh.faceIds = null
    const geometry = toGeometry(mesh)
    assert.equal(geometry.getAttribute('position').count, 8)
    assert.deepEqual(geometry.getIndex().array, mesh.indices)
    assert.ok(Array.from(geometry.getAttribute('normal').array).every(Number.isFinite))
    assert.equal(faceOfTriangle(geometry, 0), null)
    geometry.dispose()
  })

  it('does not assign an unknown triangle to a named face', () => {
    const mesh = welded(new THREE.BoxGeometry())
    mesh.faceIds[0] = null
    const geometry = toGeometry(mesh)
    assert.equal(faceOfTriangle(geometry, 0), null)
    assert.equal(faceOfTriangle(geometry, 1), '0')
    const highlight = facesSubGeometry(geometry, ['0'])
    assert.equal(highlight.getIndex().count, 3)
    highlight.dispose()
    geometry.dispose()
  })
})

import assert from 'node:assert/strict'
import { it } from 'node:test'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import ts from 'typescript'
import * as THREE from 'three'

const require = createRequire(import.meta.url)
const compiled = ts.transpileModule(
  readFileSync(new URL('../src/util/forgeMesh.ts', import.meta.url), 'utf8'),
  { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } },
).outputText
const loaded = { exports: {} }
new Function('require', 'module', 'exports', compiled)(
  (id) => id === '../api/client' ? { authHeaders: () => ({}) } : require(id),
  loaded, loaded.exports,
)
const { facesSubGeometry } = loaded.exports

function box() {
  const geometry = new THREE.BoxGeometry()
  geometry.userData.faceIds = Array.from({ length: 12 }, (_, i) => String(Math.floor(i / 2)))
  return geometry
}

function corners(geometry) {
  return Array.from(geometry.getIndex().array, (i) =>
    new THREE.Vector3().fromBufferAttribute(geometry.getAttribute('position'), i).toArray())
}

it('preserves body triangle order for duplicate and out-of-order face selections', () => {
  const geometry = box()
  const overlay = facesSubGeometry(geometry, ['4', '0', '4', 'missing'])
  const all = corners(geometry)
  assert.deepEqual(corners(overlay), [...all.slice(0, 6), ...all.slice(24, 30)])
  overlay.dispose()
  geometry.dispose()
})

it('does not scan all triangle labels on subsequent face hovers', () => {
  const geometry = box()
  let reads = 0
  geometry.userData.faceIds = new Proxy(geometry.userData.faceIds, {
    get(target, key, receiver) {
      if (typeof key === 'string' && /^\d+$/.test(key)) reads++
      return Reflect.get(target, key, receiver)
    },
  })
  facesSubGeometry(geometry, ['0']).dispose()
  assert.ok(reads >= 12)
  reads = 0
  const overlay = facesSubGeometry(geometry, ['4'])
  assert.equal(overlay.getIndex().count, 6)
  assert.equal(reads, 0)
  overlay.dispose()
  geometry.dispose()
})

it('uses replacement labels and explicitly independent clone metadata', () => {
  const geometry = box()
  facesSubGeometry(geometry, ['0']).dispose()
  const clone = geometry.clone()
  clone.userData = { ...clone.userData, faceIds: Array(12).fill('clone') }
  const selected = facesSubGeometry(clone, ['clone'])
  assert.equal(selected.getIndex().count, 36)
  assert.equal(facesSubGeometry(geometry, ['clone']), null)
  geometry.userData.faceIds = Array(12).fill('updated')
  assert.equal(facesSubGeometry(geometry, ['0']), null)
  const updated = facesSubGeometry(geometry, ['updated'])
  assert.equal(updated.getIndex().count, 36)
  selected.dispose()
  updated.dispose()
  geometry.dispose()
  clone.dispose()
})

it('ignores unlabelled faces and returns no geometry for empty or missing selections', () => {
  const geometry = box()
  geometry.userData.faceIds = [null, null, ...geometry.userData.faceIds.slice(2)]
  assert.equal(facesSubGeometry(geometry, ['0']), null)
  assert.equal(facesSubGeometry(geometry, []), null)
  assert.equal(facesSubGeometry(geometry, ['absent']), null)
  const overlay = facesSubGeometry(geometry, ['1'])
  assert.equal(overlay.getIndex().count, 6)
  overlay.dispose()
  geometry.dispose()
})

import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import ts from 'typescript'
import * as THREE from 'three'

const require = createRequire(import.meta.url)
function load(path, overrides = {}) {
  const compiled = ts.transpileModule(
    readFileSync(new URL(path, import.meta.url), 'utf8'),
    { compilerOptions: {
      module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022,
      jsx: ts.JsxEmit.ReactJSX,
    } },
  ).outputText
  const loaded = { exports: {} }
  new Function('require', 'module', 'exports', compiled)(
    (id) => overrides[id] ?? require(id), loaded, loaded.exports,
  )
  return loaded.exports
}
const { prepareDisplayGeometry } = load('../src/util/meshDisplay.ts')
const { facesSubGeometry } = load('../src/util/forgeMesh.ts', {
  '../api/client': { authHeaders: () => ({}) },
})

describe('display geometry ownership and fitting', () => {
  it('fits successive previews from local bounds regardless of previous scale', () => {
    const mesh = new THREE.Mesh()
    for (const width of [0.01, 20, 0.4, 3]) {
      const source = new THREE.BoxGeometry(width, width / 2, width / 4)
      source.translate(5, 6, 7)
      const original = source.getAttribute('position').array.slice()
      const display = prepareDisplayGeometry(source)
      mesh.geometry = display.geometry
      mesh.scale.setScalar(display.scale)
      mesh.updateMatrixWorld(true)
      const bounds = new THREE.Box3().setFromObject(mesh)
      assert.ok(Math.abs(bounds.getSize(new THREE.Vector3()).x - 1.5) < 1e-5)
      assert.ok(bounds.getCenter(new THREE.Vector3()).length() < 1e-5)
      assert.deepEqual(source.getAttribute('position').array, original)
      display.geometry.dispose()
      source.dispose()
    }
    mesh.material.dispose()
  })

  it('preserves a supplied frozen centre and leaves cached geometry untouched', () => {
    const source = new THREE.BoxGeometry(2, 4, 6)
    source.translate(5, 6, 7)
    source.userData.faceIds = ['face']
    const display = prepareDisplayGeometry(source, [1, 2, 3])
    assert.deepEqual(display.geometry.boundingBox.getCenter(new THREE.Vector3()).toArray(), [4, 4, 4])
    assert.deepEqual(display.geometry.userData.faceIds, ['face'])
    assert.notEqual(display.geometry.getAttribute('normal'), source.getAttribute('normal'))
    source.computeBoundingBox()
    assert.deepEqual(source.boundingBox.getCenter(new THREE.Vector3()).toArray(), [5, 6, 7])
    display.geometry.dispose()
    source.dispose()
  })

  it('keeps empty previews finite', () => {
    const source = new THREE.BufferGeometry()
    source.setAttribute('position', new THREE.Float32BufferAttribute([], 3))
    const display = prepareDisplayGeometry(source)
    assert.equal(display.scale, 1)
    assert.equal(display.geometry.getAttribute('position').count, 0)
    display.geometry.dispose()
    source.dispose()
  })

  it('allocates only a selected face and owns its position buffer', () => {
    const source = new THREE.BoxGeometry()
    source.userData.faceIds = Array.from({ length: 12 }, (_, i) => String(Math.floor(i / 2)))
    const overlay = facesSubGeometry(source, ['4'])
    assert.equal(overlay.getIndex().count, 6)
    assert.equal(overlay.getAttribute('position').count, 4)
    assert.notEqual(overlay.getAttribute('position'), source.getAttribute('position'))
    assert.notEqual(overlay.getAttribute('position').array.buffer, source.getAttribute('position').array.buffer)
    for (let i = 0; i < 6; i++) {
      const actual = new THREE.Vector3().fromBufferAttribute(overlay.getAttribute('position'), overlay.getIndex().getX(i))
      const expected = new THREE.Vector3().fromBufferAttribute(source.getAttribute('position'), source.getIndex().getX(24 + i))
      assert.deepEqual(actual.toArray(), expected.toArray())
    }
    overlay.dispose()
    source.dispose()
  })

  it('lets clicks reach the body through a highlight and disposes the overlay on cleanup', () => {
    let cleanup
    const { FaceOverlay } = load('../src/components/FaceOverlay.tsx', {
      react: { useEffect: (effect) => { cleanup = effect() } },
    })
    const source = new THREE.BoxGeometry()
    source.userData.faceIds = Array.from({ length: 12 }, (_, i) => String(Math.floor(i / 2)))
    const geometry = facesSubGeometry(source, ['4'])
    let disposed = false
    geometry.addEventListener('dispose', () => { disposed = true })
    const element = FaceOverlay({ geometry, colour: '#ffffff', opacity: 0.5 })
    const body = new THREE.Mesh(source)
    const overlay = new THREE.Mesh(geometry)
    overlay.raycast = element.props.raycast
    body.add(overlay)
    body.updateMatrixWorld(true)
    const ray = new THREE.Raycaster(new THREE.Vector3(0.1, 0.2, 2), new THREE.Vector3(0, 0, -1))
    const hits = ray.intersectObject(body, true)
    assert.ok(hits.length > 0)
    assert.ok(hits.every((hit) => hit.object === body))
    assert.ok(hits.every((hit) => source.userData.faceIds[hit.faceIndex] === '4'))
    cleanup()
    assert.equal(disposed, true)
    source.dispose()
    body.material.dispose()
    overlay.material.dispose()
  })
})

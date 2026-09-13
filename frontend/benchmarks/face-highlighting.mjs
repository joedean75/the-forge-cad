// Run with: node --expose-gc benchmarks/face-highlighting.mjs [baseline-source.ts]
// Reports observations only; CI timing is not a stable pass/fail threshold.
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { performance } from 'node:perf_hooks'
import ts from 'typescript'
import * as THREE from 'three'

const require = createRequire(import.meta.url)
function implementation(path) {
  const compiled = ts.transpileModule(readFileSync(path, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText
  const loaded = { exports: {} }
  new Function('require', 'module', 'exports', compiled)(
    (id) => id === '../api/client' ? { authHeaders: () => ({}) } : require(id),
    loaded, loaded.exports,
  )
  return loaded.exports.facesSubGeometry
}

const triangleCount = 200_000, faceCount = 2_000, iterations = 500
function fixture() {
  const geometry = new THREE.BufferGeometry()
  const positions = new Float32Array(triangleCount * 9)
  const indices = new Uint32Array(triangleCount * 3)
  for (let t = 0; t < triangleCount; t++) {
    positions.set([t, 0, 0, t + 1, 0, 0, t, 1, 0], t * 9)
    indices.set([t * 3, t * 3 + 1, t * 3 + 2], t * 3)
  }
  geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3))
  geometry.setIndex(new THREE.BufferAttribute(indices, 1))
  geometry.userData.faceIds = Array.from({ length: triangleCount }, (_, t) =>
    String(Math.floor(t / (triangleCount / faceCount))))
  return geometry
}

function measure(name, path) {
  const select = implementation(path)
  const geometry = fixture()
  global.gc?.()
  const heapBefore = process.memoryUsage().heapUsed
  const firstStart = performance.now()
  select(geometry, ['0']).dispose()
  const firstMs = performance.now() - firstStart
  global.gc?.()
  const retainedHeapBytes = process.memoryUsage().heapUsed - heapBefore
  for (let i = 0; i < 50; i++) select(geometry, [String(i)]).dispose()
  const times = []
  for (let i = 0; i < iterations; i++) {
    const start = performance.now()
    select(geometry, [String((i * 37) % faceCount)]).dispose()
    times.push(performance.now() - start)
  }
  times.sort((a, b) => a - b)
  geometry.dispose()
  return {
    name, firstMs, medianMs: times[Math.floor(iterations / 2)],
    p95Ms: times[Math.floor(iterations * 0.95)], retainedHeapBytes,
  }
}

const results = []
if (process.argv[2]) results.push(measure('baseline', process.argv[2]))
results.push(measure('current', new URL('../src/util/forgeMesh.ts', import.meta.url)))
console.log(JSON.stringify({
  node: process.version, triangleCount, faceCount, iterations,
  gcEnabled: typeof global.gc === 'function', results,
}, null, 2))

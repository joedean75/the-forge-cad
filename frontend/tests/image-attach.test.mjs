import assert from 'node:assert/strict'
import { it } from 'node:test'
import { readFileSync } from 'node:fs'
import ts from 'typescript'

const compiled = ts.transpileModule(
  readFileSync(new URL('../src/util/imageAttach.ts', import.meta.url), 'utf8'),
  { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } },
).outputText
const loaded = { exports: {} }
new Function('require', 'module', 'exports', compiled)(() => ({}), loaded, loaded.exports)
const { fitWithin, dataUriBytes, MAX_IMAGE_EDGE } = loaded.exports

it('shrinks a large photo to the longest-edge limit and keeps its aspect', () => {
  assert.deepEqual(fitWithin(4032, 3024), { width: MAX_IMAGE_EDGE, height: 1176 })
  assert.deepEqual(fitWithin(3024, 4032), { width: 1176, height: MAX_IMAGE_EDGE })
})

it('never enlarges a small image', () => {
  assert.deepEqual(fitWithin(640, 480), { width: 640, height: 480 })
})

it('counts decoded bytes from the base64 length, padding included', () => {
  const uri = (bytes) => `data:image/png;base64,${Buffer.from(bytes).toString('base64')}`
  for (const n of [1, 2, 3, 4, 5, 1000]) assert.equal(dataUriBytes(uri(new Uint8Array(n))), n)
  assert.equal(dataUriBytes('not a data uri'), 0)
})

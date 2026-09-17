import assert from 'node:assert/strict'
import { it } from 'node:test'
import { readFileSync } from 'node:fs'
import ts from 'typescript'

const compiled = ts.transpileModule(
  readFileSync(new URL('../src/util/nearBottom.ts', import.meta.url), 'utf8'),
  { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } },
).outputText
const loaded = { exports: {} }
new Function('require', 'module', 'exports', compiled)(() => ({}), loaded, loaded.exports)
const { isNearBottom, NEAR_BOTTOM_PX } = loaded.exports

const box = (scrollTop) => ({ scrollTop, scrollHeight: 1000, clientHeight: 400 })

it('follows new content while the reader is at the end', () => {
  assert.equal(isNearBottom(box(600)), true)
  assert.equal(isNearBottom(box(600 - NEAR_BOTTOM_PX)), true)
})

it('leaves the reader alone once they scroll up', () => {
  assert.equal(isNearBottom(box(600 - NEAR_BOTTOM_PX - 1)), false)
  assert.equal(isNearBottom(box(0)), false)
})

it('treats a transcript shorter than its box as at the end', () => {
  assert.equal(isNearBottom({ scrollTop: 0, scrollHeight: 200, clientHeight: 400 }), true)
})

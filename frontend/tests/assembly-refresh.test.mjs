import assert from 'node:assert/strict'
import { beforeEach, describe, it } from 'node:test'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import ts from 'typescript'

// Exercise the real store with only its HTTP boundary replaced. TypeScript is
// already a build dependency; Node's test runner needs no extra test packages.
const require = createRequire(import.meta.url)
const api = { getAssemblyDoc: async () => { throw new Error('unexpected fetch') } }
const source = readFileSync(new URL('../src/store/useAssemblyStore.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText
const loaded = { exports: {} }
new Function('require', 'module', 'exports', compiled)(
  (id) => id === '../api/client' ? { api } : require(id), loaded, loaded.exports,
)
const { useAssemblyStore } = loaded.exports
const assembly = (id) => ({
  id, name: id, components: [], mates: [], created_at: '', updated_at: '',
})

function deferred() {
  let resolve
  const promise = new Promise((done) => { resolve = done })
  return { promise, resolve }
}

beforeEach(() => {
  useAssemblyStore.getState().close()
  api.getAssemblyDoc = async () => { throw new Error('unexpected fetch') }
  useAssemblyStore.setState({ assembly: assembly('a'), stale: true })
})

describe('assembly refresh recovery', () => {
  it('keeps old geometry visibly stale on failure, then permits retry', async () => {
    const original = useAssemblyStore.getState().assembly
    api.getAssemblyDoc = async () => { throw new Error('offline') }
    await useAssemblyStore.getState().refresh()
    let state = useAssemblyStore.getState()
    assert.equal(state.assembly, original)
    assert.equal(state.stale, true)
    assert.equal(state.refreshing, false)
    assert.match(state.error, /offline/)
    const updated = { ...assembly('a'), name: 'updated' }
    api.getAssemblyDoc = async () => updated
    await state.refresh()
    state = useAssemblyStore.getState()
    assert.equal(state.assembly, updated)
    assert.equal(state.stale, false)
    assert.equal(state.refreshing, false)
    assert.equal(state.error, null)
    assert.equal(state.clash, null)
    assert.equal(state.bom, null)
  })

  it('does not publish an old response into another assembly', async () => {
    const oldRequest = deferred()
    api.getAssemblyDoc = () => oldRequest.promise
    const refreshing = useAssemblyStore.getState().refresh()
    api.getAssemblyDoc = async () => assembly('b')
    await useAssemblyStore.getState().open('b')
    oldRequest.resolve(assembly('a'))
    await refreshing
    assert.equal(useAssemblyStore.getState().assembly.id, 'b')
    assert.equal(useAssemblyStore.getState().refreshing, false)
  })

  it('does not overwrite edits made while a refresh is in flight', async () => {
    const request = deferred()
    api.getAssemblyDoc = () => request.promise
    const refreshing = useAssemblyStore.getState().refresh()
    const newer = { ...assembly('a'), name: 'newer edit' }
    useAssemblyStore.setState({ assembly: newer })
    request.resolve(assembly('a'))
    await refreshing
    assert.equal(useAssemblyStore.getState().assembly, newer)
    assert.equal(useAssemblyStore.getState().stale, true)
    assert.equal(useAssemblyStore.getState().refreshing, false)
  })

  it('coalesces concurrent retry clicks', async () => {
    const request = deferred()
    let calls = 0
    api.getAssemblyDoc = () => { calls++; return request.promise }
    const first = useAssemblyStore.getState().refresh()
    await useAssemblyStore.getState().refresh()
    assert.equal(calls, 1)
    request.resolve(assembly('a'))
    await first
  })

  it('keeps a newly invalidated source stale when an older refresh finishes', async () => {
    const request = deferred()
    api.getAssemblyDoc = () => request.promise
    const refreshing = useAssemblyStore.getState().refresh()
    useAssemblyStore.getState().markStale()
    request.resolve(assembly('a'))
    await refreshing
    assert.equal(useAssemblyStore.getState().stale, true)
    assert.equal(useAssemblyStore.getState().refreshing, false)
  })

  it('drops selection of a component removed by the refreshed assembly', async () => {
    useAssemblyStore.setState({ selectedComponentId: 'removed' })
    api.getAssemblyDoc = async () => assembly('a')
    await useAssemblyStore.getState().refresh()
    assert.equal(useAssemblyStore.getState().selectedComponentId, null)
  })
})

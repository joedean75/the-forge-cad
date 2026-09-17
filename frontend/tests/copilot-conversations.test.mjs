import assert from 'node:assert/strict'
import { it } from 'node:test'
import { readFileSync } from 'node:fs'
import ts from 'typescript'

function load(storage = new Map()) {
  const compiled = ts.transpileModule(
    readFileSync(new URL('../src/util/copilotConversations.ts', import.meta.url), 'utf8'),
    { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } },
  ).outputText
  const loaded = { exports: {} }
  new Function('module', 'exports', 'localStorage', compiled)(loaded, loaded.exports, {
    getItem: (key) => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, value),
  })
  return loaded.exports
}

const result = (summary = 'Budget exhausted; motor and mates remain.') => ({
  job_id: 'job-1', status: 'done', ok: false, finished: false,
  stop_reason: 'budget', summary,
  steps: [{ kind: 'op', op: 'build_part', args: { goal: 'Base plate, 100 mm wide' },
    ok: true, result: { component_id: 'base-123' } }],
})
const turn = (goal, overrides = {}) => ({
  id: crypto.randomUUID(), subjectId: 'assembly-a', goal, running: false, result: result(), ...overrides,
})

it('sends the original brief, actions, returned IDs and budget outcome with a continuation', () => {
  const { conversationHistory } = load()
  const history = conversationHistory([turn('Build a NEMA 17 motor mount with a 100 mm base')])
  for (const text of ['NEMA 17', '100 mm', 'build_part', 'base-123', 'budget', 'motor and mates remain']) {
    assert.ok(history.includes(text), text)
  }
  assert.equal(conversationHistory([]), '')
})

it('preserves what an earlier "this" referred to and keeps its work plan across reloads', () => {
  const storage = new Map()
  const m = load(storage)
  const scope = m.conversationScope('alice', 'assembly', 'a')
  const plan = { objective: 'Mount the motor', requirements: ['M4 holes'],
    completed: ['Plate built'], remaining: ['Fit motor'], decisions: ['0.25 mm clearance'] }
  m.updateConversations(scope, (s) => ({ ...s, conversations: [{ ...s.conversations[0], turns: [
    turn('Move this', { context: { selection: { component_id: 'motor-1' } },
      result: { ...result(), task_state: plan } }),
  ] }] }))
  const restored = load(storage).readConversations(scope).conversations[0].turns
  assert.deepEqual(restored[0].result.task_state, plan)
  assert.match(m.conversationHistory(restored), /motor-1/)
})

it('bounds long history while retaining the first brief and the latest correction and actions', () => {
  const { conversationHistory, MAX_HISTORY_CHARS } = load()
  const turns = Array.from({ length: 140 }, (_, i) => turn(`Goal ${i}: ${'x'.repeat(2000)}`, {
    result: { ...result(), steps: Array.from({ length: 120 }, (_, j) => ({
      kind: 'op', op: `operation-${i}-${j}`, args: { description: 'y'.repeat(5000) }, ok: true,
    })) },
  }))
  const history = conversationHistory(turns)
  assert.ok(history.length <= MAX_HISTORY_CHARS, history.length)
  assert.match(history, /Goal 0:/)
  assert.match(history, /Goal 139:/)
  assert.match(history, /operation-139-119/)
  assert.match(history, /abbreviated/)
})

it('separates accounts, subject types, assemblies and selected conversations', () => {
  const m = load()
  const a = m.conversationScope('alice', 'assembly', 'a')
  const scopes = [m.conversationScope('bob', 'assembly', 'a'),
    m.conversationScope('alice', 'document', 'a'), m.conversationScope('alice', 'assembly', 'b')]
  const first = m.readConversations(a).selected
  const second = m.newConversation()
  m.updateConversations(a, (s) => ({ ...s, selected: second.id, conversations: [
    { ...s.conversations[0], turns: [turn('Original assembly brief')] }, second,
  ] }))
  const state = m.readConversations(a)
  assert.equal(m.conversationHistory(state.conversations.find((c) => c.id === state.selected).turns), '')
  assert.match(m.conversationHistory(state.conversations.find((c) => c.id === first).turns), /Original assembly brief/)
  for (const scope of scopes) assert.equal(m.readConversations(scope).conversations[0].turns.length, 0)
})

it('saves budget-stopped and running turns across reloads without geometry or image bytes', () => {
  const storage = new Map()
  const m = load(storage)
  const scope = m.conversationScope('alice', 'assembly', 'a')
  const state = m.readConversations(scope)
  m.updateConversations(scope, (s) => ({ ...s, conversations: [{ ...s.conversations[0], turns: [
    turn('Build the rig', { images: ['data:image/png;base64,secret-image'], result: {
      ...result(), document: { huge: 'mesh' }, assembly: { huge: 'geometry' },
    } }), turn('Continue', { running: true, result: { ...result(), status: 'running' } }),
  ] }] }))
  const serialized = [...storage.values()][0]
  assert.ok(!serialized.includes('secret-image'))
  assert.ok(!serialized.includes('geometry'))
  const restored = load(storage).readConversations(scope)
  assert.equal(restored.selected, state.selected)
  assert.equal(restored.conversations[0].turns[0].imageCount, 1)
  assert.equal(restored.conversations[0].turns[1].running, true)
  assert.equal(restored.conversations[0].turns[1].result.job_id, 'job-1')
  assert.match(m.conversationHistory(restored.conversations[0].turns), /Build the rig/)
})

it('routes late progress to the originating conversation after selection changes', () => {
  const m = load()
  const scope = m.conversationScope('alice', 'assembly', 'a')
  const first = m.readConversations(scope).conversations[0]
  const pending = turn('First goal', { running: true })
  const second = m.newConversation()
  m.updateConversations(scope, (s) => ({ ...s, selected: second.id,
    conversations: [{ ...first, turns: [pending] }, second] }))
  m.updateTurn(scope, first.id, pending.id, { running: false, result: result('First goal finished') })
  const state = m.readConversations(scope)
  assert.equal(state.selected, second.id)
  assert.equal(state.conversations[1].turns.length, 0)
  assert.equal(state.conversations[0].turns[0].result.summary, 'First goal finished')
})

it('keeps working in memory and reports when browser persistence fails', () => {
  const storage = new Map()
  storage.set = () => { throw new Error('quota exceeded') }
  const m = load(storage)
  const scope = 'blocked-storage'
  m.updateConversations(scope, (s) => ({ ...s, conversations: [{
    ...s.conversations[0], turns: [turn('Keep this goal')],
  }] }))
  const state = m.readConversations(scope)
  assert.match(state.storageError, /could not save/)
  assert.equal(state.conversations[0].turns[0].goal, 'Keep this goal')
})

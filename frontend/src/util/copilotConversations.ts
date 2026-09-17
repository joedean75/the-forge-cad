import type { CopilotContext, CopilotResult, CopilotStep } from '../../../shared/types'

export type Turn = {
  id: string; goal: string; images?: string[]; imageCount?: number
  running: boolean; subjectId: string; stopping?: boolean; stopError?: string
  result?: CopilotResult
  context?: Omit<CopilotContext, 'task_state'>
}
export type Conversation = { id: string; title: string; turns: Turn[] }
export type Conversations = {
  selected: string; conversations: Conversation[]; storageError?: string
}

export const MAX_HISTORY_CHARS = 60000
const cache = new Map<string, Conversations>()
const listeners = new Set<() => void>()
// Requests outlive the panel: switching subjects must not misroute a late poll.
export const trackedRuns = new Set<string>()

export function conversationScope(account: string, subject: string, id: string): string {
  return JSON.stringify([account, subject, id])
}

export function newConversation(): Conversation {
  return { id: crypto.randomUUID(), title: 'New conversation', turns: [] }
}

export function subscribeConversations(listener: () => void): () => void {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

export function readConversations(scope: string): Conversations {
  const cached = cache.get(scope)
  if (cached) return cached
  let value: Conversations | undefined
  try {
    const raw = JSON.parse(localStorage.getItem(`forge.copilot.conversations.v1:${scope}`) || 'null')
    if (raw && typeof raw.selected === 'string' && Array.isArray(raw.conversations)
        && raw.conversations.length && raw.conversations.every((c: Conversation) =>
          typeof c.id === 'string' && typeof c.title === 'string' && Array.isArray(c.turns)
          && c.turns.every((t: Turn) => typeof t.id === 'string' && typeof t.goal === 'string'
            && (!t.result || Array.isArray(t.result.steps))))) {
      value = raw
    }
  } catch { /* Storage may be blocked or a saved value damaged. */ }
  if (!value) {
    const first = newConversation()
    value = { selected: first.id, conversations: [first] }
  }
  if (!value.conversations.some((c) => c.id === value.selected)) value.selected = value.conversations[0].id
  cache.set(scope, value)
  return value
}

/** Never keep returned geometry or image bytes in the saved chat. */
export function transcriptResult(result: CopilotResult): CopilotResult {
  const { document: _document, assembly: _assembly, ...transcript } = result
  return transcript
}

function clip(text: string, limit: number): string {
  return text.length <= limit ? text : `${text.slice(0, Math.max(0, limit - 15))}\n[… abbreviated]`
}

function savedStep(step: CopilotStep): CopilotStep {
  return {
    ...step,
    result: step.result && JSON.stringify(step.result).length > 2000
      ? { preview: clip(JSON.stringify(step.result), 2000) } : step.result,
  }
}

export function updateConversations(scope: string, update: (state: Conversations) => Conversations): void {
  const next = update(readConversations(scope))
  const saved = {
    selected: next.selected,
    conversations: next.conversations.map((c) => ({ ...c, turns: c.turns.map((t) => ({
      ...t, images: undefined, imageCount: t.images?.length || t.imageCount,
      result: t.result && { ...transcriptResult(t.result), steps: t.result.steps.map(savedStep) },
    })) })),
  }
  try {
    localStorage.setItem(`forge.copilot.conversations.v1:${scope}`, JSON.stringify(saved))
    next.storageError = undefined
  } catch {
    next.storageError = 'Conversation is available for this session, but browser storage could not save it.'
  }
  cache.set(scope, next)
  listeners.forEach((listener) => listener())
}

export function updateTurn(scope: string, conversationId: string, turnId: string, patch: Partial<Turn>): void {
  updateConversations(scope, (state) => ({ ...state, conversations: state.conversations.map((c) =>
    c.id !== conversationId ? c : { ...c, turns: c.turns.map((t) => t.id === turnId ? { ...t, ...patch } : t) }),
  }))
}

/** Preserve the original brief and recent goals/outcomes before spending room
 * on operation detail. A long build must not exhaust the next model context.
 * Older detail is explicitly abbreviated; nothing here consumes action steps.
 */
export function conversationHistory(turns: Turn[]): string {
  if (!turns.length) return ''
  const headers = turns.map((turn, i) =>
    `Turn ${i + 1}\nUser: ${clip(turn.goal, 12000)}\nCopilot: ${clip(turn.result?.summary || 'No final response received.', 2500)}`
    + `\nStop reason: ${turn.result?.stop_reason || turn.result?.status || 'unknown'}`
    + (turn.context?.selection ? `\nSelection for that request: ${clip(JSON.stringify(turn.context.selection), 1500)}` : '')
    + (turn.stopError ? `\nConnection/stop error: ${clip(turn.stopError, 500)}` : '')
    + ((turn.images?.length || turn.imageCount) ? '\nReference images were attached; their pixels are not included in this text history.' : ''))
  const included = new Set([0, turns.length - 1])
  let used = [...included].reduce((n, i) => n + headers[i].length, 0)
  for (let i = turns.length - 2; i > 0; i--) {
    if (used + headers[i].length > 40000) break
    included.add(i)
    used += headers[i].length
  }
  const details = new Map<number, string>()
  let remaining = MAX_HISTORY_CHARS - used - included.size * 100 - 200
  for (const i of [...included].sort((a, b) => b - a)) {
    const rows: string[] = []
    const steps = turns[i].result?.steps || []
    for (let j = steps.length - 1; j >= 0; j--) {
      const row = clip(JSON.stringify(steps[j]), 2500)
      if (row.length + 1 > remaining) break
      rows.unshift(row)
      remaining -= row.length + 1
    }
    details.set(i, (rows.length < steps.length ? '\n[Older operation details abbreviated.]' : '')
      + (rows.length ? `\nOperations (already attempted):\n${rows.join('\n')}` : ''))
  }
  return [...included].sort((a, b) => a - b).map((i, n, indices) =>
    (n > 0 && i !== indices[n - 1] + 1 ? '[Older conversation turns abbreviated.]\n' : '')
    + headers[i] + details.get(i)).join('\n\n')
}

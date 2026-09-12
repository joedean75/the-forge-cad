import { useEffect, useRef, useState } from 'react'
import { Bot, Send, X, Check, AlertTriangle, Loader2, Wrench } from 'lucide-react'
import { useDocumentStore } from '../store/useDocumentStore'
import { useAssemblyStore } from '../store/useAssemblyStore'
import { api } from '../api/client'
import type { CopilotResult, CopilotStep } from '../../../shared/types'

type Turn = { goal: string; running: boolean; subjectId: string; stopping?: boolean; stopError?: string; result?: CopilotResult }

/** Which thing the panel is driving. One panel rather than two, because the
 *  loop, the transcript and the stop button are identical — what differs is
 *  the id in the path and therefore which half of the catalog the planner is
 *  offered, which is a decision the server makes. */
type Subject = 'document' | 'assembly'

const EXAMPLES: Record<Subject, string[]> = {
  document: [
    'Add four M4 mounting holes near the corners',
    'Shell the body to a 2 mm wall',
    'Round the top edges with a 3 mm fillet',
    'Cut a 20 mm slot down the centre',
  ],
  assembly: [
    'Build a NEMA 17 motor mount: a base plate, an upright, and the motor bolted to it',
    'Insert a NEMA 17 stepper and mate it to the plate',
    'Check for interference and tell me what clashes',
    'Report the all-up mass and where the CG sits',
  ],
}

const BLURB: Record<Subject, string> = {
  document: 'The copilot edits this part step by step through the operation API.',
  assembly: 'The copilot designs the parts an assembly needs, places them, mates them, and checks for interference — one part at a time until it holds together.',
}

/** Undo is a document-store feature; an assembly has no local history, so
 *  promising one here would be a lie in the one place a user checks. */
const FOOTNOTE: Record<Subject, string> = {
  document: 'Enter to send · Shift+Enter for a newline · every step is undoable',
  assembly: 'Enter to send · Shift+Enter for a newline · each step runs through the operation API',
}

function StepRow({ step }: { step: CopilotStep }) {
  if (step.kind !== 'op') {
    return (
      <div className="flex items-start gap-1.5 text-[11px] text-cad-text/70 pl-1">
        {step.kind === 'error' ? <AlertTriangle size={11} className="text-cad-danger mt-0.5 shrink-0" /> : null}
        <span>{step.error ?? (step.result?.summary as string | undefined) ?? 'done'}</span>
      </div>
    )
  }
  const ok = step.ok
  return (
    <div className="flex items-start gap-1.5 text-[11px] pl-1">
      {ok ? <Check size={11} className="text-cad-success mt-0.5 shrink-0" /> : <AlertTriangle size={11} className="text-cad-danger mt-0.5 shrink-0" />}
      <div className="min-w-0">
        <span className="tabular text-cad-text-heading">{step.op}</span>
        {step.rationale && <span className="text-cad-text/70"> · {step.rationale}</span>}
        {!ok && step.error && <div className="text-cad-danger/90 truncate">{step.error}</div>}
      </div>
    </div>
  )
}

export function CopilotPanel({ open, onClose, subject = 'document' }: {
  open: boolean; onClose: () => void; subject?: Subject
}) {
  const docStore = useDocumentStore()
  const asmStore = useAssemblyStore()
  const onAssembly = subject === 'assembly'
  const busy = onAssembly ? asmStore.busy : docStore.busy
  const subjectId = (onAssembly ? asmStore.assembly?.id : docStore.doc?.id) ?? null
  const runCopilot = onAssembly ? asmStore.runCopilot : docStore.runCopilot
  const noun = onAssembly ? 'assembly' : 'part'
  const [goal, setGoal] = useState('')
  const [turns, setTurns] = useState<Turn[]>([])
  const scrollRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' })
  }, [turns])

  if (!open) return null

  const submit = async (g: string) => {
    const text = g.trim()
    if (!text || busy || !subjectId) return
    setGoal('')
    const idx = turns.length
    setTurns((t) => [...t, { goal: text, running: true, subjectId }])
    // Live progress: the copilot runs as a server-side job; each poll delivers
    // the transcript so far.
    const result = await runCopilot(text, (state) =>
      setTurns((t) => t.map((x, i) => (i === idx ? { ...x, result: state } : x))),
    )
    setTurns((t) => t.map((x, i) => (i === idx ? { ...x, running: false, result } : x)))
  }

  const stop = async (index: number, turn: Turn) => {
    if (!turn.result?.job_id) return
    setTurns((t) => t.map((x, i) => i === index ? { ...x, stopping: true, stopError: undefined } : x))
    try {
      await api.cancelCopilot(turn.subjectId, turn.result.job_id,
                              onAssembly ? 'assemblies' : 'documents')
    }
    catch (e) {
      setTurns((t) => t.map((x, i) => i === index ? { ...x, stopping: false,
        stopError: e instanceof Error ? e.message : 'Could not stop the task' } : x))
    }
  }

  return (
    // `bottom-8` clears the editor's status bar. The assembly screen has no
    // status bar, so the same offset would leave the panel floating two rems
    // above the bottom edge of its own screen.
    <aside className={`fixed top-14 ${onAssembly ? 'bottom-0' : 'bottom-8'} right-0 w-[380px] max-w-full bg-cad-panel border-l border-cad-border flex flex-col z-30 shadow-2xl`}>
      <div className="h-11 border-b border-cad-border flex items-center justify-between px-4 shrink-0">
        <div className="flex items-center gap-2">
          <Bot size={16} className="text-cad-accent" />
          <div className="leading-none">
            <span className="eyebrow text-cad-text/70">Forge</span>
            <h2 className="font-display text-sm font-semibold text-cad-text-heading leading-none mt-1">Copilot</h2>
          </div>
        </div>
        <button onClick={onClose} className="p-1.5 rounded-sm text-cad-text hover:text-cad-text-heading hover:bg-cad-raised"><X size={16} /></button>
      </div>

      <div ref={scrollRef} className="flex-1 overflow-y-auto px-4 py-3 space-y-4">
        {turns.length === 0 && (
          <div className="text-center pt-6">
            <Bot size={28} className="text-cad-border mx-auto" />
            <p className="text-xs text-cad-text-heading mt-3">Describe a goal — I'll plan and build it</p>
            <p className="text-[11px] text-cad-text/60 mt-1">{BLURB[subject]}</p>
            <div className="flex flex-col gap-1.5 mt-4">
              {EXAMPLES[subject].map((e) => (
                <button
                  key={e}
                  onClick={() => submit(e)}
                  disabled={busy}
                  className="text-left text-[11px] px-2.5 py-1.5 rounded-sm border border-cad-border text-cad-text hover:text-cad-text-heading hover:border-cad-accent/50 hover:bg-cad-accent/5 transition-colors disabled:opacity-40"
                >
                  {e}
                </button>
              ))}
            </div>
          </div>
        )}

        {turns.map((turn, i) => (
          <div key={i} className="space-y-2">
            {/* user goal */}
            <div className="flex justify-end">
              <div className="max-w-[85%] text-[12px] bg-cad-accent/12 border border-cad-accent/30 text-cad-text-heading rounded-md rounded-tr-sm px-2.5 py-1.5">
                {turn.goal}
              </div>
            </div>
            {/* agent activity */}
            <div className="bg-cad-bg/50 border border-cad-border rounded-md px-2.5 py-2 space-y-1.5">
              {turn.result && turn.result.steps.length > 0 && (
                <>
                  <div className="flex items-center gap-1.5 text-[11px] text-cad-text/60">
                    <Wrench size={11} /> {turn.result.steps.filter((s) => s.kind === 'op').length} step(s)
                  </div>
                  {turn.result.steps.map((s, j) => <StepRow key={j} step={s} />)}
                </>
              )}
              {turn.running ? (
                <div className="flex items-center gap-2 text-[11px] text-cad-accent-hot">
                  <Loader2 size={12} className="animate-spin" /> {turn.stopping ? 'Stopping after current operation...' : 'Planning & building...'}
                  {turn.result?.job_id && <button onClick={() => stop(i, turn)} disabled={turn.stopping}
                    className="ml-auto px-2 py-1 border border-cad-border rounded-sm disabled:opacity-40">Stop</button>}
                </div>
              ) : turn.result ? (
                <div className="text-[12px] text-cad-text-heading pt-1 border-t border-cad-border/60 mt-1">{turn.result.summary}</div>
              ) : (
                <div className="text-[11px] text-cad-danger">No response.</div>
              )}
              {turn.stopError && <p role="alert" className="text-[11px] text-cad-danger">{turn.stopError}</p>}
            </div>
          </div>
        ))}
      </div>

      <div className="border-t border-cad-border p-2.5 shrink-0">
        <div className="flex items-end gap-2 rounded-sm bg-cad-bg border border-cad-border focus-within:border-cad-accent transition-colors px-2.5 py-1.5">
          <textarea
            value={goal}
            onChange={(e) => setGoal(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit(goal) } }}
            rows={1}
            disabled={!subjectId}
            placeholder={subjectId ? `Ask the copilot to change the ${noun}…`
                                   : `Open ${onAssembly ? 'an assembly' : 'a part'} first`}
            className="flex-1 bg-transparent text-xs text-cad-text-heading placeholder:text-cad-text/50 outline-none resize-none max-h-24"
          />
          <button
            onClick={() => submit(goal)}
            disabled={!goal.trim() || busy || !subjectId}
            className="flex items-center justify-center h-7 w-7 rounded-sm bg-cad-accent text-cad-bg hover:bg-cad-accent-hot transition-colors disabled:opacity-40 shrink-0"
          >
            {busy ? <Loader2 size={13} className="animate-spin" /> : <Send size={13} />}
          </button>
        </div>
        <p className="text-[9px] text-cad-text/40 mt-1 px-1">{FOOTNOTE[subject]}</p>
      </div>
    </aside>
  )
}

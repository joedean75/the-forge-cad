import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { Bot, Send, X, Check, AlertTriangle, Loader2, Wrench, ImagePlus } from 'lucide-react'
import { useDocumentStore } from '../store/useDocumentStore'
import { useAuthStore } from '../store/useAuthStore'
import { useAssemblyStore } from '../store/useAssemblyStore'
import { api } from '../api/client'
import type { CopilotStep } from '../../../shared/types'
import { prepareImage, dataUriBytes, MAX_IMAGES, MAX_TOTAL_IMAGE_BYTES } from '../util/imageAttach'

import {
  conversationScope, conversationHistory, newConversation, readConversations,
  subscribeConversations, updateConversations, updateTurn, transcriptResult, trackedRuns,
  type Turn,
} from '../util/copilotConversations'

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

const MODEL_KEY = 'forge.copilot.model'

/** The picked model, remembered per browser.
 *
 * Wrapped because a browser with site data blocked *throws* on the accessor
 * rather than returning null, and a thrown read here would take the whole panel
 * down with it. An empty answer is a real answer: use the server's default.
 */
function rememberedModel(): string {
  try { return localStorage.getItem(MODEL_KEY) || '' } catch { return '' }
}

function rememberModel(model: string): void {
  try {
    if (model) localStorage.setItem(MODEL_KEY, model)
    else localStorage.removeItem(MODEL_KEY)
  } catch { /* a blocked store is not a reason to refuse the run */ }
}

/** `anthropic/claude-sonnet-5` -> `claude-sonnet-5`: the vendor prefix is the
 *  same for every row of a provider's list and costs the width that tells two
 *  models apart. */
function shortModel(id: string): string {
  return id.includes('/') ? id.slice(id.indexOf('/') + 1) : id
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
  const accountId = useAuthStore((s) => s.account?.id ?? 'local')
  const docId = useDocumentStore((s) => s.doc?.id)
  const asmId = useAssemblyStore((s) => s.assembly?.id)
  const scope = conversationScope(accountId, subject, (subject === 'assembly' ? asmId : docId) ?? '')
  return <CopilotConversationPanel key={scope} scope={scope} open={open} onClose={onClose} subject={subject} />
}

function CopilotConversationPanel({ open, onClose, subject, scope }: {
  open: boolean; onClose: () => void; subject: Subject; scope: string
}) {
  const docStore = useDocumentStore()
  const asmStore = useAssemblyStore()
  const onAssembly = subject === 'assembly'
  const storeBusy = onAssembly ? asmStore.busy : docStore.busy
  const subjectId = (onAssembly ? asmStore.assembly?.id : docStore.doc?.id) ?? null
  const runCopilot = onAssembly ? asmStore.runCopilot : docStore.runCopilot
  const noun = onAssembly ? 'assembly' : 'part'
  const [goal, setGoal] = useState('')
  const conversations = useSyncExternalStore(subscribeConversations, () => readConversations(scope))
  const conversation = conversations.conversations.find((c) => c.id === conversations.selected)!
  const turns = conversation.turns
  const conversationBusy = conversations.conversations.some((c) => c.turns.some((t) => t.running))
  const submitting = useRef(false)
  const busy = storeBusy || conversationBusy
  const [models, setModels] = useState<string[]>([])
  const [defaultModel, setDefaultModel] = useState('')
  const [model, setModel] = useState(rememberedModel)
  // `null` means the provider publishes no modalities: unknown, so allowed.
  const [visionModels, setVisionModels] = useState<string[] | null>(null)
  const [images, setImages] = useState<string[]>([])
  const [imageError, setImageError] = useState<string | null>(null)
  const [preparing, setPreparing] = useState(false)
  const scrollRef = useRef<HTMLDivElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' })
  }, [turns])

  // Only once the panel is open: the list costs a request, and a closed panel
  // is the common case on both screens.
  useEffect(() => {
    if (!open || models.length) return
    let live = true
    api.getLlmConfig()
      .then((cfg) => {
        if (!live) return
        setModels(cfg.available_models || [])
        setDefaultModel(cfg.model || '')
        setVisionModels(cfg.vision_models ?? null)
      })
      // A provider that will not list its models is not a reason to hide the
      // panel — the run still works on the server's default.
      .catch(() => undefined)
    return () => { live = false }
  }, [open, models.length])

  // A saved job id lets a reload reconnect without starting the goal again.
  useEffect(() => {
    for (const c of conversations.conversations) {
      for (const turn of c.turns) {
        if (!turn.running || trackedRuns.has(turn.id)) continue
        const patch = (value: Partial<Turn>) => updateTurn(scope, c.id, turn.id, value)
        if (!turn.result?.job_id) {
          patch({ running: false, stopError: 'The page closed before a job ID was received. Check the current model before retrying.' })
          continue
        }
        trackedRuns.add(turn.id)
        api.waitForCopilot(turn.subjectId, turn.result.job_id,
          (state) => patch({ result: transcriptResult(state) }),
          onAssembly ? 'assemblies' : 'documents')
          .then(async (result) => {
            patch({ running: false, result: transcriptResult(result) })
            if (onAssembly && useAssemblyStore.getState().assembly?.id === turn.subjectId) {
              useAssemblyStore.getState().markStale()
              await useAssemblyStore.getState().refresh()
            } else if (!onAssembly && useDocumentStore.getState().doc?.id === turn.subjectId) {
              await useDocumentStore.getState().refreshDoc()
            }
          })
          .catch((e) => patch({ running: false, stopError: e instanceof Error ? e.message : 'Could not reconnect to the task' }))
          .finally(() => trackedRuns.delete(turn.id))
      }
    }
  }, [conversations, scope, onAssembly])

  if (!open) return null

  const effectiveModel = model || defaultModel
  const textOnly = (m: string) => visionModels !== null && !visionModels.includes(m)
  // Refused here rather than sent: the server would answer 422, and switching
  // the model silently would run the comparison the user did not ask for.
  const imageBlocked = images.length > 0 && !!effectiveModel && textOnly(effectiveModel)

  const attach = async (files: File[]) => {
    // Cleared so choosing the same files again still fires `change`. The File
    // objects were already copied out of the input before this ran.
    if (fileRef.current) fileRef.current.value = ''
    const picked = files.filter((f) => f.type.startsWith('image/'))
    if (!picked.length || busy || !subjectId) return
    const room = MAX_IMAGES - images.length
    if (room <= 0) { setImageError(`Up to ${MAX_IMAGES} images per goal`); return }
    setImageError(null)
    setPreparing(true)
    let problem: string | null = picked.length > room
      ? `Only ${room} more image${room === 1 ? '' : 's'} fit. A goal takes up to ${MAX_IMAGES}.` : null
    let total = images.reduce((n, uri) => n + dataUriBytes(uri), 0)
    const added: string[] = []
    // One at a time: each is decoded onto a canvas, and six full-size photos
    // decoded at once is several hundred megabytes of bitmap.
    for (const file of picked.slice(0, room)) {
      try {
        const uri = await prepareImage(file)
        if (total + dataUriBytes(uri) > MAX_TOTAL_IMAGE_BYTES) {
          problem = 'Those images are too large to send together. Remove one and try again.'
          break
        }
        total += dataUriBytes(uri)
        added.push(uri)
      } catch (e) {
        problem = e instanceof Error ? e.message : 'Could not read that image'
      }
    }
    setImages((cur) => [...cur, ...added].slice(0, MAX_IMAGES))
    setImageError(problem)
    setPreparing(false)
  }

  const submit = async (g: string) => {
    const text = g.trim()
    if (!text || busy || submitting.current || !subjectId || preparing || imageBlocked) return
    const sentImages = images
    setGoal('')
    setImages([])
    setImageError(null)
    submitting.current = true
    const turnId = crypto.randomUUID()
    const conversationId = conversation.id
    trackedRuns.add(turnId)
    const history = conversationHistory(turns)
    updateConversations(scope, (state) => ({ ...state, conversations: state.conversations.map((c) =>
      c.id !== conversationId ? c : {
        ...c, title: c.turns.length ? c.title : text.slice(0, 70),
        turns: [...c.turns, { id: turnId, goal: text, images: sentImages.length ? sentImages : undefined,
          running: true, subjectId }],
      }),
    }))
    const patch = (value: Partial<Turn>) => updateTurn(scope, conversationId, turnId, value)
    try {
      const result = await runCopilot(
        text, (state) => patch({ result: transcriptResult(state) }),
        model || undefined, sentImages, history,
      )
      patch({ running: false, ...(result ? { result: transcriptResult(result) } : {
        stopError: (onAssembly ? useAssemblyStore.getState().error : useDocumentStore.getState().error) || 'No response received.',
      }) })
    } catch (e) {
      patch({ running: false, stopError: e instanceof Error ? e.message : 'Copilot failed' })
    } finally {
      trackedRuns.delete(turnId)
      submitting.current = false
    }
  }

  const stop = async (turn: Turn) => {
    if (!turn.result?.job_id) return
    const patch = (value: Partial<Turn>) => updateTurn(scope, conversation.id, turn.id, value)
    patch({ stopping: true, stopError: undefined })
    try {
      await api.cancelCopilot(turn.subjectId, turn.result.job_id,
                              onAssembly ? 'assemblies' : 'documents')
    } catch (e) {
      patch({ stopping: false, stopError: e instanceof Error ? e.message : 'Could not stop the task' })
    }
  }

  const selectConversation = (id: string) => {
    setGoal('')
    setImages([])
    setImageError(null)
    updateConversations(scope, (state) => ({ ...state, selected: id }))
  }

  const startConversation = () => {
    const next = newConversation()
    updateConversations(scope, (state) => ({ ...state, selected: next.id,
      conversations: [...state.conversations, next] }))
    setGoal('')
    setImages([])
    setImageError(null)
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

      <div className="border-b border-cad-border px-4 py-2 space-y-1.5 shrink-0">
        <div className="flex items-center gap-2">
          <label htmlFor="copilot-conversation" className="eyebrow text-cad-text/55">Chat</label>
          <select id="copilot-conversation" value={conversation.id}
            disabled={preparing} onChange={(e) => selectConversation(e.target.value)}
            className="flex-1 min-w-0 bg-cad-bg border border-cad-border rounded-sm px-1.5 py-1 text-[11px] text-cad-text-heading">
            {conversations.conversations.map((c) => <option key={c.id} value={c.id}>{c.title}</option>)}
          </select>
          <button onClick={startConversation} disabled={!subjectId || busy || preparing}
            className="text-[11px] text-cad-text border border-cad-border rounded-sm px-2 py-1 disabled:opacity-40">New chat</button>
        </div>
        <p className="text-[10px] text-cad-text/55">
          {turns.length ? `${turns.length} previous prompt(s) included as context. ` : ''}
          Chats are saved in this browser for this {noun}; long histories are abbreviated.
          {' '}Reattach reference images when needed.
        </p>
        {conversations.storageError && <p role="alert" className="text-[11px] text-cad-danger">{conversations.storageError}</p>}
      </div>

      {/* Which model plans the run, for this run only — the process-wide default
          is left alone, so comparing two models here does not change what every
          other session gets. Disabled mid-run: the model is fixed when the job
          starts and a control that looks live but changes nothing is worse than
          one that is visibly unavailable. */}
      <div className="border-b border-cad-border px-4 py-2 flex items-center gap-2 shrink-0">
        <label htmlFor="copilot-model" className="eyebrow text-cad-text/55 shrink-0">Model</label>
        <select
          id="copilot-model"
          value={model}
          disabled={busy || models.length === 0}
          onChange={(e) => { setModel(e.target.value); rememberModel(e.target.value) }}
          className="flex-1 min-w-0 bg-cad-bg border border-cad-border rounded-sm px-1.5 py-1 text-[11px] text-cad-text-heading outline-none focus:border-cad-accent disabled:opacity-50"
        >
          <option value="">
            {defaultModel ? `Default · ${shortModel(defaultModel)}` : 'Default'}
          </option>
          {models.filter((m) => m !== defaultModel).map((m) => (
            <option key={m} value={m}>{shortModel(m)}{textOnly(m) ? ' · text only' : ''}</option>
          ))}
        </select>
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

        {turns.map((turn) => (
          <div key={turn.id} className="space-y-2">
            {/* user goal */}
            <div className="flex justify-end">
              <div className="max-w-[85%] text-[12px] bg-cad-accent/12 border border-cad-accent/30 text-cad-text-heading rounded-md rounded-tr-sm px-2.5 py-1.5">
                {turn.images && (
                  <div className={`mb-1.5 grid gap-1 ${turn.images.length === 1 ? 'grid-cols-1' : 'grid-cols-3'}`}>
                    {turn.images.map((uri, j) => (
                      <img key={j} src={uri} alt={`Reference ${j + 1} sent with this goal`}
                           className={`block rounded-sm border border-cad-border ${turn.images!.length === 1 ? 'max-h-32 max-w-full' : 'h-16 w-full object-cover'}`} />
                    ))}
                  </div>
                )}
                {!turn.images?.length && !!turn.imageCount && <div className="text-[10px] text-cad-text/55">{turn.imageCount} reference image(s) attached previously</div>}
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
                  {turn.result?.job_id && <button onClick={() => stop(turn)} disabled={turn.stopping}
                    className="ml-auto px-2 py-1 border border-cad-border rounded-sm disabled:opacity-40">Stop</button>}
                </div>
              ) : turn.result ? (
                <div className="pt-1 border-t border-cad-border/60 mt-1">
                  <div className="text-[12px] text-cad-text-heading">{turn.result.summary}</div>
                  {/* On the turn rather than only in the picker: the picker says
                      what the *next* run will use, which is the wrong question
                      when reading back a transcript of several. */}
                  {turn.result.model && (
                    <div className="eyebrow text-cad-text/40 mt-1">{shortModel(turn.result.model)}</div>
                  )}
                </div>
              ) : (
                <div className="text-[11px] text-cad-danger">No response.</div>
              )}
              {turn.stopError && <p role="alert" className="text-[11px] text-cad-danger">{turn.stopError}</p>}
            </div>
          </div>
        ))}
      </div>

      <div className="border-t border-cad-border p-2.5 shrink-0">
        {(images.length > 0 || preparing) && (
          <div className="mb-2 space-y-1.5">
            <div className="flex flex-wrap items-start gap-2">
              {images.map((uri, i) => (
                <div key={i} className="relative">
                  <img src={uri} alt={`Attached reference ${i + 1}`} className="h-14 w-14 object-cover rounded-sm border border-cad-border bg-cad-bg" />
                  <button onClick={() => setImages((cur) => cur.filter((_, j) => j !== i))} aria-label={`Remove image ${i + 1}`}
                    className="absolute -top-1.5 -right-1.5 h-5 w-5 flex items-center justify-center rounded-full bg-cad-panel border border-cad-border text-cad-text hover:text-cad-text-heading">
                    <X size={11} />
                  </button>
                </div>
              ))}
              {preparing && (
                <div className="h-14 w-14 flex items-center justify-center rounded-sm border border-cad-border"><Loader2 size={14} className="animate-spin text-cad-text/60" /></div>
              )}
            </div>
            {images.length > 0 && (
              <p className="text-[10px] text-cad-text/50">{images.length} of {MAX_IMAGES} images · each is re-sent on every step</p>
            )}
            {imageBlocked && (
              <p role="alert" className="text-[11px] text-cad-danger">
                {shortModel(effectiveModel)} does not accept images. Pick another model or remove the images.
              </p>
            )}
          </div>
        )}
        {imageError && <p role="alert" className="mb-2 text-[11px] text-cad-danger">{imageError}</p>}
        <input ref={fileRef} type="file" accept="image/png,image/jpeg,image/webp,image/gif" multiple className="hidden"
               onChange={(e) => attach(Array.from(e.target.files ?? []))} />
        <div
          className="flex items-end gap-2 rounded-sm bg-cad-bg border border-cad-border focus-within:border-cad-accent transition-colors px-2.5 py-1.5"
          onDragOver={(e) => { if (Array.from(e.dataTransfer.items).some((it) => it.kind === 'file')) e.preventDefault() }}
          onDrop={(e) => {
            const files = Array.from(e.dataTransfer.files)
            if (files.length) { e.preventDefault(); attach(files) }
          }}
        >
          <textarea
            value={goal}
            onChange={(e) => setGoal(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit(goal) } }}
            onPaste={(e) => {
              const files = Array.from(e.clipboardData.files).filter((f) => f.type.startsWith('image/'))
              if (files.length) { e.preventDefault(); attach(files) }
            }}
            rows={1}
            disabled={!subjectId}
            placeholder={subjectId ? `Ask the copilot to change the ${noun}…`
                                   : `Open ${onAssembly ? 'an assembly' : 'a part'} first`}
            className="flex-1 bg-transparent text-xs text-cad-text-heading placeholder:text-cad-text/50 outline-none resize-none max-h-24"
          />
          <button
            onClick={() => fileRef.current?.click()}
            disabled={busy || !subjectId || preparing || images.length >= MAX_IMAGES}
            aria-label="Attach images"
            title={`Attach up to ${MAX_IMAGES} reference images (or paste or drop them)`}
            className="flex items-center justify-center h-7 w-7 rounded-sm text-cad-text hover:text-cad-text-heading hover:bg-cad-raised transition-colors disabled:opacity-40 shrink-0"
          >
            <ImagePlus size={14} />
          </button>
          <button
            onClick={() => submit(goal)}
            disabled={!goal.trim() || busy || !subjectId || preparing || imageBlocked}
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

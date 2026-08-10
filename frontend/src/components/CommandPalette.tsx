import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Search, Box, Circle, Cylinder, Cone, Rotate3d, FlipHorizontal2, Spline, Layers,
  Blend, Triangle, Shell, CircleDot, Grid3x3, Move3d, PencilRuler, Boxes, Waypoints,
  Activity, Wrench, Printer, Undo2, Redo2, Save, Maximize, FileText, Scissors, Bot,
  PlayCircle, Terminal, ArrowLeft, Loader2, CornerDownLeft,
} from 'lucide-react'
import { api } from '../api/client'
import { useDocumentStore } from '../store/useDocumentStore'
import type { EditorMode, OperationParam, OperationSpec } from '../../../shared/types'
import type { OperationFailure } from '../store/useDocumentStore'

type Cmd = { id: string; label: string; group: string; keywords?: string; icon: typeof Box; run: () => void }

/**
 * Operations console (Cmd/Ctrl-K).
 *
 * Two tiers, deliberately. The top tier is a short list of task-shaped
 * shortcuts — "Add box" should not make anyone fill in a form. Below it is the
 * **live operation catalog**, fetched from the same `GET /api/operations` an
 * agent reads, so every operation the backend can run is reachable here.
 *
 * That second tier is the point. The old palette listed commands by hand, so
 * each new operation shipped unreachable until someone remembered to add it —
 * and this catalog grew from 55 to 76 in a fortnight. Reading the catalog means
 * an operation is in the console the day it exists, with its real arguments.
 *
 * Argument fields are generated from each operation's own schema: enums become
 * choices, numbers carry their unit, defaults arrive filled in. Queries print
 * their answer here rather than closing, because for the 34 of them the answer
 * *is* the point.
 */
export function CommandPalette({ onOpenCopilot }: { onOpenCopilot: () => void }) {
  const [open, setOpen] = useState(false)
  const [q, setQ] = useState('')
  const [active, setActive] = useState(0)
  const [catalog, setCatalog] = useState<OperationSpec[]>([])
  const [picked, setPicked] = useState<OperationSpec | null>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const s = useDocumentStore()

  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        setOpen((v) => !v)
        setQ(''); setActive(0); setPicked(null)
      } else if (e.key === 'Escape') {
        // Escape steps back out of an operation before it closes the console.
        setPicked((p) => (p ? null : p))
        setOpen((wasOpen) => (picked ? wasOpen : false))
      }
    }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [picked])

  useEffect(() => { if (open && !picked) setTimeout(() => inputRef.current?.focus(), 0) }, [open, picked])

  // Fetched once per session, the first time the console is opened.
  useEffect(() => {
    if (!open || catalog.length) return
    api.listOperations().then((r) => setCatalog(r.operations)).catch(() => setCatalog([]))
  }, [open, catalog.length])

  const shortcuts = useMemo<Cmd[]>(() => {
    const mode = (m: EditorMode, label: string, icon: typeof Box): Cmd =>
      ({ id: `mode-${m}`, label: `Go to ${label}`, group: 'Mode', icon, run: () => s.setMode(m) })
    const prim = (shape: string, label: string, icon: typeof Box): Cmd =>
      ({ id: `add-${shape}`, label: `Add ${label}`, group: 'Create', keywords: 'primitive body', icon,
         run: () => s.runOp('add_primitive', { shape, operation: 'union' }) })
    const mod = (modifier: string, label: string, icon: typeof Box): Cmd =>
      ({ id: `mod-${modifier}`, label, group: 'Modify', keywords: 'modifier', icon,
         run: () => s.runOp('add_modifier', { modifier }) })

    return [
      mode('model', 'Model', Boxes), mode('optimize', 'Optimize', Waypoints),
      mode('analysis', 'Analysis', Activity), mode('make', 'Make', Wrench),
      mode('validate', 'Validate', PlayCircle), mode('machine', 'Machine', Printer),

      prim('box', 'box', Box), prim('cylinder', 'cylinder', Cylinder),
      prim('sphere', 'sphere', Circle), prim('cone', 'cone', Cone),

      { id: 'revolve', label: 'Revolve profile', group: 'Forms', keywords: 'shaft wheel pulley turned',
        icon: Rotate3d, run: () => s.runOp('add_revolve', { operation: 'union' }) },
      { id: 'sweep', label: 'Sweep along path', group: 'Forms', keywords: 'pipe tube wire',
        icon: Spline, run: () => s.runOp('add_sweep', { operation: 'union' }) },
      { id: 'loft', label: 'Loft between profiles', group: 'Forms', keywords: 'adapter taper funnel',
        icon: Layers, run: () => s.runOp('add_loft', { operation: 'union' }) },
      { id: 'mirror', label: 'Mirror body', group: 'Forms', keywords: 'symmetry reflect', icon: FlipHorizontal2,
        run: () => s.runOp('add_mirror', s.selectedFeatureId ? { target: s.selectedFeatureId } : {}) },

      mod('fillet', 'Fillet edges', Blend), mod('chamfer', 'Chamfer edges', Triangle),
      mod('shell', 'Shell (hollow)', Shell), mod('hole', 'Hole', CircleDot),
      mod('pattern', 'Pattern selected', Grid3x3), mod('transform', 'Move / rotate / scale', Move3d),

      { id: 'sketch', label: 'New sketch', group: 'Sketch', icon: PencilRuler, run: () => s.startSketchPick() },
      { id: 'drawing', label: 'Generate 2D drawing', group: 'Make', keywords: 'print blueprint', icon: FileText,
        run: () => { s.setMode('make') } },
      { id: 'cut', label: 'Laser / waterjet cut', group: 'Make', keywords: 'sheet plasma', icon: Scissors,
        run: () => { s.setMode('make') } },
      { id: 'copilot', label: 'Ask Forge Copilot', group: 'AI', keywords: 'agent generate', icon: Bot, run: onOpenCopilot },

      { id: 'undo', label: 'Undo', group: 'Edit', icon: Undo2, run: () => s.undo() },
      { id: 'redo', label: 'Redo', group: 'Edit', icon: Redo2, run: () => s.redo() },
      { id: 'save', label: 'Save checkpoint', group: 'Edit', icon: Save, run: () => s.saveCheckpoint() },
      { id: 'fit', label: 'Fit view', group: 'View', keywords: 'zoom center', icon: Maximize,
        run: () => window.dispatchEvent(new KeyboardEvent('keydown', { key: 'f' })) },
    ]
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [s.selectedFeatureId])

  // Only operations this screen can actually run. Assembly operations post to a
  // different endpoint and belong to the assembly screen; offering them here
  // would be a button that always fails.
  const runnable = useMemo(
    () => catalog.filter((o) => (o.subject ?? 'document') === 'document'),
    [catalog],
  )

  const query = q.trim().toLowerCase()
  const filteredShortcuts = useMemo(
    () => (!query ? shortcuts
      : shortcuts.filter((c) => `${c.label} ${c.group} ${c.keywords ?? ''}`.toLowerCase().includes(query))),
    [query, shortcuts],
  )
  const filteredOps = useMemo(
    () => (!query ? runnable
      : runnable.filter((o) => `${o.name} ${o.description}`.toLowerCase().includes(query))),
    [query, runnable],
  )

  const rows = useMemo(
    () => [...filteredShortcuts.map((c) => ({ kind: 'cmd' as const, cmd: c })),
           ...filteredOps.map((o) => ({ kind: 'op' as const, op: o }))],
    [filteredShortcuts, filteredOps],
  )

  useEffect(() => { setActive(0) }, [q])

  if (!open) return null

  const choose = (i: number) => {
    const row = rows[i]
    if (!row) return
    if (row.kind === 'cmd') { row.cmd.run(); setOpen(false) }
    else setPicked(row.op)
  }

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center pt-[12vh] bg-black/40 backdrop-blur-sm"
      onClick={() => setOpen(false)}>
      <div className="w-[620px] max-w-[94vw] bg-cad-panel border border-cad-border rounded-md shadow-2xl overflow-hidden"
        onClick={(e) => e.stopPropagation()}>

        {picked ? (
          <OperationForm spec={picked} onBack={() => setPicked(null)} onDone={() => { setPicked(null); setOpen(false) }} />
        ) : (
          <>
            <div className="flex items-center gap-2 px-3 h-11 border-b border-cad-border">
              <Search size={15} className="text-cad-text/60" />
              <input
                ref={inputRef}
                value={q}
                onChange={(e) => setQ(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'ArrowDown') { e.preventDefault(); setActive((a) => Math.min(a + 1, rows.length - 1)) }
                  else if (e.key === 'ArrowUp') { e.preventDefault(); setActive((a) => Math.max(a - 1, 0)) }
                  else if (e.key === 'Enter') { e.preventDefault(); choose(active) }
                }}
                placeholder="Search tools and operations…"
                className="flex-1 bg-transparent text-sm text-cad-text-heading placeholder:text-cad-text/40 outline-none"
              />
              <kbd className="text-[9px] text-cad-text/40 border border-cad-border rounded px-1 py-0.5">esc</kbd>
            </div>

            <div className="max-h-[54vh] overflow-y-auto py-1">
              {rows.length === 0 && (
                <div className="px-3 py-5 text-xs text-cad-text/60">
                  Nothing matches “{q.trim()}”. Try an operation name like <span className="tabular">fillet</span> or <span className="tabular">tolerance</span>.
                </div>
              )}

              {filteredShortcuts.map((c, i) => (
                <button
                  key={c.id}
                  onMouseEnter={() => setActive(i)}
                  onClick={() => choose(i)}
                  className={`w-full flex items-center gap-2.5 px-3 py-1.5 text-left transition-colors ${
                    i === active ? 'bg-cad-accent/12 text-cad-text-heading' : 'text-cad-text hover:bg-cad-raised'}`}
                >
                  <c.icon size={14} className={i === active ? 'text-cad-accent-hot' : 'text-cad-text/60'} />
                  <span className="text-xs flex-1">{c.label}</span>
                  <span className="eyebrow text-cad-text/35">{c.group}</span>
                </button>
              ))}

              {filteredOps.length > 0 && (
                <div className="flex items-center gap-2 px-3 pt-3 pb-1.5">
                  <Terminal size={11} className="text-cad-text/40" />
                  <span className="eyebrow text-cad-text/40">
                    All operations · {runnable.length}
                  </span>
                  <span className="h-px flex-1 bg-cad-border" />
                </div>
              )}

              {filteredOps.map((o, j) => {
                const i = filteredShortcuts.length + j
                return (
                  <button
                    key={o.name}
                    onMouseEnter={() => setActive(i)}
                    onClick={() => choose(i)}
                    className={`w-full flex items-center gap-2.5 px-3 py-1.5 text-left transition-colors ${
                      i === active ? 'bg-cad-accent/12' : 'hover:bg-cad-raised'}`}
                  >
                    <KindDot kind={o.kind} />
                    <span className="tabular text-xs text-cad-text-heading">{o.name}</span>
                    <span className="text-[11px] text-cad-text/55 truncate flex-1">{firstSentence(o.description)}</span>
                    {Object.keys(o.params ?? {}).length > 0 && (
                      <span className="eyebrow text-cad-text/30">{Object.keys(o.params).length} args</span>
                    )}
                  </button>
                )
              })}
            </div>
          </>
        )}
      </div>
    </div>
  )
}

/** mutate changes the body, query only answers — a real distinction, so it shows. */
function KindDot({ kind }: { kind: OperationSpec['kind'] }) {
  const mutate = kind === 'mutate'
  return (
    <span
      title={mutate ? 'Changes the part' : 'Answers a question'}
      className={`w-1.5 h-1.5 rounded-full shrink-0 ${mutate ? 'bg-cad-accent' : 'bg-stress-void'}`}
    />
  )
}

function firstSentence(text: string): string {
  const cut = text.indexOf('. ')
  return cut > 0 ? text.slice(0, cut + 1) : text
}

/** Seed a form from the schema's own defaults so running it is one keystroke. */
function initialArgs(spec: OperationSpec): Record<string, string> {
  const out: Record<string, string> = {}
  for (const [name, p] of Object.entries(spec.params ?? {})) {
    if (p.default !== undefined && p.default !== null) {
      out[name] = typeof p.default === 'object' ? JSON.stringify(p.default) : String(p.default)
    } else if (p.enum?.length) {
      out[name] = ''
    } else {
      out[name] = ''
    }
  }
  return out
}

/** Turn the typed strings back into what the API expects. */
function coerce(params: Record<string, OperationParam>, raw: Record<string, string>) {
  const args: Record<string, unknown> = {}
  for (const [name, p] of Object.entries(params ?? {})) {
    const v = (raw[name] ?? '').trim()
    if (!v) continue
    if (p.type === 'number' || p.type === 'integer') {
      const n = Number(v)
      if (!Number.isNaN(n)) args[name] = n
    } else if (p.type === 'boolean') {
      args[name] = v === 'true'
    } else if (p.type === 'object' || p.type === 'array') {
      try { args[name] = JSON.parse(v) } catch { throw new Error(`${name} must be valid JSON`) }
    } else {
      args[name] = v
    }
  }
  return args
}

/**
 * The argument form, generated from the operation's schema.
 *
 * Structured arguments (object/array) get a JSON field rather than a invented
 * widget — guessing a shape the schema only describes in prose would be worse
 * than showing the shape and letting someone type it.
 */
function OperationForm({ spec, onBack, onDone }: {
  spec: OperationSpec
  onBack: () => void
  onDone: () => void
}) {
  const s = useDocumentStore()
  const [raw, setRaw] = useState<Record<string, string>>(() => initialArgs(spec))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [failure, setFailure] = useState<OperationFailure | null>(null)
  const [result, setResult] = useState<unknown>(null)
  const first = useRef<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>(null)

  useEffect(() => { setTimeout(() => first.current?.focus(), 0) }, [])

  const entries = Object.entries(spec.params ?? {})

  const run = async () => {
    // `runOp` returns early with no failure when nothing is open, which this
    // form would otherwise read as success and close on — a no-op reporting
    // that it worked. The editor is reachable with no part (a bare `#editor`
    // link), so say so instead.
    if (!useDocumentStore.getState().doc) {
      setError('No part is open. Open one from Files first.')
      return
    }
    setBusy(true); setError(null); setFailure(null); setResult(null)
    let out: Record<string, unknown> | undefined
    try {
      out = await s.runOp(spec.name, coerce(spec.params, raw))
    } catch (e) {
      // Only argument coercion throws — runOp itself reports through the store.
      setError(e instanceof Error ? e.message : 'Check the arguments and try again')
      setBusy(false)
      return
    }
    setBusy(false)

    // `runOp` catches its own errors and records them on the store rather than
    // throwing, so a try/catch here would let a failed operation close the
    // console as though it had worked. Read the failure channel instead — it
    // already carries the backend's message, the offending argument, and a
    // remedy written for the error code.
    const failed = useDocumentStore.getState().failure
    if (failed && failed.op === spec.name) { setFailure(failed); return }

    // A mutate has already changed the part behind the console; a query's
    // answer is the whole reason it was run, so it stays and shows it.
    if (spec.kind === 'query') setResult(out ?? {})
    else onDone()
  }

  return (
    <div>
      <div className="flex items-center gap-2 px-3 h-11 border-b border-cad-border">
        <button onClick={onBack} title="Back to search" className="text-cad-text hover:text-cad-text-heading">
          <ArrowLeft size={15} />
        </button>
        <KindDot kind={spec.kind} />
        <span className="tabular text-sm text-cad-text-heading">{spec.name}</span>
        <span className="eyebrow text-cad-text/40 ml-auto">
          {spec.kind === 'mutate' ? 'changes the part' : 'answers'}
        </span>
      </div>

      <div className="px-4 pt-3 pb-1">
        <p className="text-[11.5px] text-cad-text leading-relaxed">{spec.description}</p>
      </div>

      <div className="px-4 py-3 space-y-3 max-h-[42vh] overflow-y-auto">
        {entries.length === 0 && (
          <p className="text-[11px] text-cad-text/55">Takes no arguments — press Run.</p>
        )}
        {entries.map(([name, p], i) => (
          <Field
            key={name}
            name={name}
            param={p}
            value={raw[name] ?? ''}
            onChange={(v) => setRaw((r) => ({ ...r, [name]: v }))}
            inputRef={i === 0 ? first : undefined}
            flagged={failure?.arg === name}
          />
        ))}
      </div>

      {(error || failure) && (
        <div className="mx-4 mb-3 px-3 py-2 rounded-sm border border-cad-danger/40 bg-cad-danger/10 space-y-1">
          <p className="text-[11px] text-cad-danger">
            {failure?.arg && <span className="tabular">{failure.arg}: </span>}
            {failure?.message ?? error}
          </p>
          {failure?.remedy && (
            <p className="text-[10.5px] text-cad-text leading-snug">{failure.remedy}</p>
          )}
        </div>
      )}

      {result !== null && (
        <div className="mx-4 mb-3 rounded-sm border border-cad-border bg-cad-bg overflow-hidden">
          <div className="px-3 py-1.5 border-b border-cad-border eyebrow text-cad-text/50">Result</div>
          <pre className="px-3 py-2 text-[11px] text-cad-text-heading tabular overflow-x-auto max-h-40">
{JSON.stringify(result, null, 2)}
          </pre>
        </div>
      )}

      <div className="flex items-center gap-2 px-4 py-3 border-t border-cad-border">
        <button
          onClick={run}
          disabled={busy}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-sm bg-cad-accent text-cad-bg text-xs font-semibold hover:bg-cad-accent-hot transition-colors disabled:opacity-50"
        >
          {busy ? <Loader2 size={12} className="animate-spin" /> : <CornerDownLeft size={12} />}
          {busy ? 'Running…' : 'Run'}
        </button>
        <span className="text-[10px] text-cad-text/40">
          {spec.kind === 'query' ? 'The answer appears here.' : 'Applies to the open part.'}
        </span>
      </div>
    </div>
  )
}

/**
 * One argument, drawn from its own schema entry.
 *
 * `flagged` marks the argument the backend blamed. The field list scrolls, so a
 * message naming `screw` is not much use when `screw` is above the fold — the
 * field marks itself and scrolls into view.
 */
function Field({ name, param, value, onChange, inputRef, flagged }: {
  name: string
  param: OperationParam
  value: string
  onChange: (v: string) => void
  inputRef?: React.RefObject<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement | null>
  flagged?: boolean
}) {
  const label = name.replace(/_/g, ' ')
  const structured = param.type === 'object' || param.type === 'array'
  const row = useRef<HTMLLabelElement>(null)

  useEffect(() => {
    if (flagged) row.current?.scrollIntoView({ block: 'nearest' })
  }, [flagged])

  const box = flagged ? 'border-cad-danger' : 'border-cad-border'

  return (
    <label ref={row} className="block">
      <div className="flex items-baseline gap-2 mb-1">
        <span className="tabular text-[11px] text-cad-text-heading">{label}</span>
        {param.required && <span className="eyebrow text-cad-accent">required</span>}
        {param.unit && <span className="eyebrow text-cad-text/40">{param.unit}</span>}
      </div>

      {param.enum?.length ? (
        <select
          ref={inputRef as React.RefObject<HTMLSelectElement>}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className={`w-full px-2 py-1.5 rounded-sm bg-cad-bg border ${box} text-cad-text-heading text-xs focus:border-cad-accent outline-none`}
        >
          <option value="">— choose —</option>
          {param.enum.map((opt) => <option key={opt} value={opt}>{opt}</option>)}
        </select>
      ) : param.type === 'boolean' ? (
        <select
          ref={inputRef as React.RefObject<HTMLSelectElement>}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className={`w-full px-2 py-1.5 rounded-sm bg-cad-bg border ${box} text-cad-text-heading text-xs focus:border-cad-accent outline-none`}
        >
          <option value="">— unset —</option>
          <option value="true">yes</option>
          <option value="false">no</option>
        </select>
      ) : structured ? (
        <textarea
          ref={inputRef as React.RefObject<HTMLTextAreaElement>}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          rows={2}
          spellCheck={false}
          placeholder={param.description ?? 'JSON'}
          className={`w-full px-2 py-1.5 rounded-sm bg-cad-bg border ${box} text-cad-text-heading text-[11px] tabular focus:border-cad-accent outline-none resize-y`}
        />
      ) : (
        <input
          ref={inputRef as React.RefObject<HTMLInputElement>}
          type={param.type === 'number' || param.type === 'integer' ? 'number' : 'text'}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={param.default !== undefined ? String(param.default) : ''}
          className={`w-full px-2 py-1.5 rounded-sm bg-cad-bg border ${box} text-cad-text-heading text-xs tabular focus:border-cad-accent outline-none`}
        />
      )}

      {param.description && !structured && (
        <p className="text-[10px] text-cad-text/50 mt-1 leading-snug">{param.description}</p>
      )}
    </label>
  )
}

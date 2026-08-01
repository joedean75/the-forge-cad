import { useEffect, useState } from 'react'
import { Check, ClipboardCheck, Loader2, Plus, Trash2, X, AlertTriangle } from 'lucide-react'
import { useDocumentStore } from '../../store/useDocumentStore'
import { api } from '../../api/client'
import type { DfmCheck, SpecRequirement } from '../../../../shared/types'

/**
 * The design spec — what the part is *supposed* to be.
 *
 * Everything else in this app checks that the body is well formed: watertight,
 * manufacturable, strong enough. None of that can tell you the bracket came out
 * 80 mm long when it was meant to be 100. That is what a spec is for, and the
 * score it returns is the one number that says whether the part is *right*.
 *
 * Every requirement kind here is something the kernel can actually measure —
 * there is no "looks like" — and an unmeasurable one fails loudly rather than
 * being skipped, because a spec scoring 1.0 because half of it was ignored is
 * the worst thing a score can do.
 */

const KINDS: Array<{ id: SpecRequirement['kind']; label: string; hint: string }> = [
  { id: 'bounding_box', label: 'Overall size', hint: 'an axis extent, mm' },
  { id: 'mass', label: 'Mass', hint: 'grams' },
  { id: 'volume', label: 'Volume', hint: 'cm³' },
  { id: 'hole', label: 'Holes', hint: 'bore ⌀ mm, and how many' },
  { id: 'material', label: 'Material', hint: 'a catalog material id' },
  { id: 'feature', label: 'Feature present', hint: 'a feature type, e.g. EdgeFilletFeature' },
  { id: 'watertight', label: 'Watertight solid', hint: 'no bound needed' },
  { id: 'bodies', label: 'Body count', hint: 'usually 1' },
]

const NEEDS_NO_BOUND = new Set<SpecRequirement['kind']>(['watertight'])

function blank(kind: SpecRequirement['kind']): SpecRequirement {
  if (kind === 'bounding_box') return { kind, axis: 'x', value: 100, tolerance: 1 }
  if (kind === 'hole') return { kind, diameter: 6, count: 4, diameter_tolerance: 0.2 }
  if (kind === 'bodies') return { kind, value: 1 }
  if (kind === 'material') return { kind, value: 'al-6061' }
  if (kind === 'feature') return { kind, value: 'EdgeFilletFeature' }
  if (kind === 'watertight') return { kind }
  return { kind, value: 100, tolerance: 5 }
}

export function SpecPanel() {
  const { doc, busy, specReport, saveSpec, verifySpec } = useDocumentStore()
  const [goal, setGoal] = useState('')
  const [rows, setRows] = useState<SpecRequirement[]>([])
  const [loaded, setLoaded] = useState(false)

  // Load whatever spec the document already carries; an agent or an earlier
  // session may have set one.
  useEffect(() => {
    if (!doc) return
    setLoaded(false)
    api.getSpec(doc.id)
      .then((env) => {
        if (!env.ok) return
        setRows(env.result.requirements ?? [])
        setGoal(env.result.goal ?? '')
        if ((env.result.requirements ?? []).length) verifySpec()
      })
      .catch(() => {})
      .finally(() => setLoaded(true))
  }, [doc?.id, doc, verifySpec])

  if (!doc) return null

  const edit = (i: number, patch: Partial<SpecRequirement>) =>
    setRows(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)))

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="sec-head">Design spec</h3>
        {specReport && <Score report={specReport} />}
      </div>
      <p className="text-[10px] text-cad-text/60 leading-snug">
        What the part is supposed to be. Every requirement is measured on the body, so the score says
        whether it is the <span className="text-cad-text-heading">right</span> part — not merely a valid one.
      </p>

      <input
        value={goal}
        onChange={(e) => setGoal(e.target.value)}
        placeholder="The request in words (optional)"
        className="w-full px-2 py-1.5 rounded-sm bg-cad-bg border border-cad-border text-cad-text-heading text-[11px] outline-none focus:border-cad-accent"
      />

      {rows.map((r, i) => (
        <RequirementRow key={i} row={r} onEdit={(patch) => edit(i, patch)} onRemove={() => setRows(rows.filter((_, j) => j !== i))} />
      ))}

      <div className="flex items-center gap-2">
        <select
          value=""
          onChange={(e) => e.target.value && setRows([...rows, blank(e.target.value as SpecRequirement['kind'])])}
          className="flex-1 px-2 py-1.5 rounded-sm bg-cad-bg border border-cad-border text-cad-text text-[11px] focus:border-cad-accent outline-none"
        >
          <option value="">Add a requirement…</option>
          {KINDS.map((k) => (
            <option key={k.id} value={k.id}>{k.label} — {k.hint}</option>
          ))}
        </select>
        <button
          onClick={() => setRows([...rows, blank('bounding_box')])}
          title="Add a size requirement"
          className="flex items-center justify-center h-7 w-7 rounded-sm border border-cad-border text-cad-text hover:text-cad-accent-hot transition"
        >
          <Plus size={13} />
        </button>
      </div>

      <div className="flex gap-2">
        <button
          onClick={() => saveSpec(rows, goal.trim() || undefined)}
          disabled={busy || !loaded}
          className="flex-1 flex items-center justify-center gap-1.5 py-2 rounded-sm bg-cad-accent text-cad-bg text-xs font-semibold hover:bg-cad-accent-hot transition-colors disabled:opacity-50"
        >
          {busy ? <Loader2 size={13} className="animate-spin" /> : <ClipboardCheck size={13} />}
          {rows.length ? `Save & verify ${rows.length}` : 'Clear the spec'}
        </button>
        <button
          onClick={verifySpec}
          disabled={busy || rows.length === 0}
          className="px-3 rounded-sm border border-cad-border text-cad-text hover:text-cad-text-heading transition text-[11px] disabled:opacity-40"
        >
          Re-verify
        </button>
      </div>

      {specReport && <SpecChecks checks={specReport.checks} />}
    </div>
  )
}

function Score({ report }: { report: { score: number; passed: number; failed: number; met: boolean } }) {
  const pct = Math.round(report.score * 100)
  const tone = report.met ? 'text-cad-success' : pct >= 50 ? 'text-cad-warning' : 'text-cad-danger'
  return (
    <div className="flex items-baseline gap-1.5">
      <span className={`font-display text-lg font-semibold ${tone}`}>{pct}%</span>
      <span className="tabular text-[10px] text-cad-text/60">
        {report.passed}/{report.passed + report.failed} met
      </span>
    </div>
  )
}

const STATUS_ICON = { pass: Check, warn: AlertTriangle, fail: X }
const STATUS_TONE = { pass: 'text-cad-success', warn: 'text-cad-warning', fail: 'text-cad-danger' }

function SpecChecks({ checks }: { checks: DfmCheck[] }) {
  if (checks.length === 0) return null
  return (
    <ul className="space-y-1.5 pt-2 border-t border-cad-border">
      {checks.map((c) => {
        const Icon = STATUS_ICON[c.status]
        return (
          <li key={c.id} className="flex items-start gap-2 text-[11px]">
            <Icon size={13} className={`${STATUS_TONE[c.status]} mt-px shrink-0`} />
            <span className="text-cad-text-heading shrink-0">{c.label}</span>
            <span className="tabular text-cad-text/70 flex-1 text-right leading-snug">{c.detail}</span>
          </li>
        )
      })}
    </ul>
  )
}

function RequirementRow({
  row,
  onEdit,
  onRemove,
}: {
  row: SpecRequirement
  onEdit: (patch: Partial<SpecRequirement>) => void
  onRemove: () => void
}) {
  const kind = KINDS.find((k) => k.id === row.kind)
  return (
    <div className="p-2 rounded-sm border border-cad-border bg-cad-bg/40 space-y-1.5">
      <div className="flex items-center justify-between">
        <span className="text-[10px] text-cad-text-heading">{kind?.label ?? row.kind}</span>
        <button onClick={onRemove} className="text-cad-text/60 hover:text-cad-danger"><Trash2 size={11} /></button>
      </div>

      <div className="flex flex-wrap items-center gap-1.5">
        {row.kind === 'bounding_box' && (
          <select
            value={row.axis ?? 'x'}
            onChange={(e) => onEdit({ axis: e.target.value as 'x' | 'y' | 'z' })}
            className="px-1.5 py-1 rounded-sm bg-cad-bg border border-cad-border text-cad-text-heading text-[11px] outline-none"
          >
            <option value="x">X</option><option value="y">Y</option><option value="z">Z</option>
          </select>
        )}

        {row.kind === 'hole' ? (
          <>
            <Field label="⌀" value={row.diameter ?? 6} onChange={(v) => onEdit({ diameter: v })} suffix="mm" />
            <Field label="±" value={row.diameter_tolerance ?? 0.2} onChange={(v) => onEdit({ diameter_tolerance: v })} suffix="mm" />
            <Field label="count" value={row.count ?? 1} onChange={(v) => onEdit({ count: Math.max(0, Math.round(v)) })} suffix="" />
          </>
        ) : row.kind === 'material' || row.kind === 'feature' ? (
          <input
            value={String(row.value ?? '')}
            onChange={(e) => onEdit({ value: e.target.value })}
            className="tabular flex-1 min-w-0 px-1.5 py-1 rounded-sm bg-cad-bg border border-cad-border text-cad-text-heading text-[11px] outline-none focus:border-cad-accent"
          />
        ) : NEEDS_NO_BOUND.has(row.kind) ? (
          <span className="text-[10px] text-cad-text/50">Nothing to set — the body either is or is not.</span>
        ) : (
          <>
            <Field label="=" value={Number(row.value ?? 0)} onChange={(v) => onEdit({ value: v })} suffix="" />
            <Field label="±" value={row.tolerance ?? 0} onChange={(v) => onEdit({ tolerance: v })} suffix="" />
          </>
        )}
      </div>
      <p className="text-[9px] text-cad-text/40">{kind?.hint}</p>
    </div>
  )
}

function Field({
  label, value, onChange, suffix,
}: { label: string; value: number; onChange: (v: number) => void; suffix: string }) {
  return (
    <label className="flex items-center gap-1 text-[10px] text-cad-text">
      <span className="tabular">{label}</span>
      <div className="flex items-center gap-1 px-1.5 py-1 rounded-sm bg-cad-bg border border-cad-border focus-within:border-cad-accent transition-colors">
        <input
          type="number"
          value={value}
          onChange={(e) => {
            const n = parseFloat(e.target.value)
            if (!Number.isNaN(n)) onChange(n)
          }}
          className="tabular w-14 bg-transparent text-[11px] text-cad-text-heading outline-none"
        />
        {suffix && <span className="text-[9px] text-cad-text/60">{suffix}</span>}
      </div>
    </label>
  )
}

import { useEffect, useState } from 'react'
import { Loader2, Factory, Download, Check, AlertTriangle, X, Route, FileText, GitBranch, Tag, Scissors, Grid2x2, FileCog, Layers } from 'lucide-react'
import { useDocumentStore } from '../../store/useDocumentStore'
import { api } from '../../api/client'
import type { DfmCheck, DfmProcess, DrawingResult, DocumentRevisionMeta, MachineSpec, NestResult, SliceResponse } from '../../../../shared/types'

const LIFECYCLE_LABEL: Record<string, string> = {
  draft: 'Draft', in_review: 'In review', released: 'Released', obsolete: 'Obsolete',
}

const ALL_VIEWS = ['front', 'top', 'right', 'left', 'back', 'bottom'] as const
type ViewName = (typeof ALL_VIEWS)[number]
const SECTION_PLANES = ['xz', 'xy', 'yz'] as const
const SHEETS = ['auto', 'a4', 'a3', 'a2', 'a1', 'a0'] as const

function Documentation({ docId, docName }: { docId: string; docName: string }) {
  const { doc } = useDocumentStore()
  const [drawing, setDrawing] = useState<DrawingResult | null>(null)
  const [loading, setLoading] = useState(false)
  const [revs, setRevs] = useState<DocumentRevisionMeta[]>([])
  const [notes, setNotes] = useState('')
  const [err, setErr] = useState<string | null>(null)
  // The drawing spec: every field here is a drafting decision, which is exactly
  // why the engine takes them from the caller rather than hardcoding front/top/
  // right and a section pinned to mid-Y. The UI used to send one boolean.
  const [views, setViews] = useState<ViewName[]>(['front', 'top', 'right'])
  const [section, setSection] = useState<'none' | (typeof SECTION_PLANES)[number]>('xz')
  const [projection, setProjection] = useState<'first' | 'third'>('third')
  const [sheet, setSheet] = useState<(typeof SHEETS)[number]>('auto')
  const [hidden, setHidden] = useState(true)
  const state = doc?.lifecycle_state ?? 'draft'

  useEffect(() => { api.listRevisions(docId).then(setRevs).catch(() => {}) }, [docId])

  const toggleView = (v: ViewName) =>
    setViews((cur) => (cur.includes(v) ? cur.filter((x) => x !== v) : [...cur, v]))

  const gen = async () => {
    setLoading(true); setErr(null)
    try {
      setDrawing(await api.drawingFromSpec(docId, {
        views: views.length ? views : ['front'],
        sections: section === 'none' ? [] : [{ plane: section, label: 'A-A' }],
        projection,
        sheet,
        show_hidden: hidden,
      }))
    }
    catch (e) { setErr(e instanceof Error ? e.message : 'Drawing failed') }
    finally { setLoading(false) }
  }
  const release = async () => {
    try {
      await api.releaseRevision(docId, notes)
      setNotes('')
      setRevs(await api.listRevisions(docId))
      await useDocumentStore.getState().refreshDoc()
    } catch (e) { setErr(e instanceof Error ? e.message : 'Release failed') }
  }
  const setState = async (s: string) => {
    try { await api.setLifecycleState(docId, s); await useDocumentStore.getState().refreshDoc() }
    catch { /* ignore */ }
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="sec-head">Drawing &amp; release</h3>
        <span className={`text-[9px] px-1.5 py-0.5 rounded-sm border ${
          state === 'released' ? 'border-cad-success/50 text-cad-success'
          : state === 'obsolete' ? 'border-cad-danger/50 text-cad-danger'
          : 'border-cad-border text-cad-text'}`}>
          {LIFECYCLE_LABEL[state]}
        </span>
      </div>

      <div>
        <label className="eyebrow text-cad-text/60 block mb-1">Views</label>
        <div className="grid grid-cols-3 gap-px rounded-sm overflow-hidden border border-cad-border">
          {ALL_VIEWS.map((v) => (
            <button key={v} onClick={() => toggleView(v)}
              className={`py-1 text-[10px] capitalize transition-colors ${
                views.includes(v) ? 'bg-cad-accent text-cad-bg' : 'bg-cad-bg text-cad-text hover:text-cad-text-heading'}`}>
              {v}
            </button>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-3 gap-2">
        <Choice label="Section" value={section} options={['none', ...SECTION_PLANES]}
          onChange={(v) => setSection(v as typeof section)} />
        <Choice label="Projection" value={projection} options={['third', 'first']}
          onChange={(v) => setProjection(v as typeof projection)} />
        <Choice label="Sheet" value={sheet} options={[...SHEETS]}
          onChange={(v) => setSheet(v as typeof sheet)} />
      </div>

      <label className="flex items-center gap-1.5 text-[10px] text-cad-text cursor-pointer">
        <input type="checkbox" checked={hidden} onChange={(e) => setHidden(e.target.checked)}
          className="accent-[var(--color-cad-accent)]" />
        Hidden lines
      </label>

      <div className="flex gap-2">
        <button onClick={gen} disabled={loading}
          className="flex-1 flex items-center justify-center gap-1.5 py-2 rounded-sm bg-cad-accent text-cad-bg text-xs font-semibold hover:bg-cad-accent-hot transition-colors disabled:opacity-50">
          {loading ? <Loader2 size={13} className="animate-spin" /> : <FileText size={13} />} 2D drawing
        </button>
        {drawing && (
          <>
            <button onClick={() => api.downloadDrawing(docId, docName).catch(() => {})}
              title="Download SVG — the readable sheet"
              className="flex items-center justify-center px-2.5 rounded-sm border border-cad-border text-cad-text hover:text-cad-text-heading transition text-[10px]">
              SVG
            </button>
            <button onClick={() => api.downloadDrawingDxf(docId, docName, {
                views: views.length ? views : ['front'],
                sections: section === 'none' ? [] : [{ plane: section, label: 'A-A' }],
                projection, sheet, show_hidden: hidden,
              }).catch((e) => setErr(e instanceof Error ? e.message : 'DXF failed'))}
              title="Download DXF — layered geometry for CAM"
              className="flex items-center justify-center px-2.5 rounded-sm border border-cad-border text-cad-text hover:text-cad-text-heading transition text-[10px]">
              <Download size={12} className="mr-1" /> DXF
            </button>
          </>
        )}
      </div>
      {err && <p className="text-[10px] text-cad-danger">{err}</p>}
      {drawing && (
        <>
          <div className="rounded-sm border border-cad-border bg-white p-1 overflow-hidden"
            dangerouslySetInnerHTML={{ __html: drawing.svg }} />
          <div className="tabular text-[10px] text-cad-text/60 flex flex-wrap gap-x-3">
            <span>{drawing.drawn_views.join(', ')}</span>
            <span>{drawing.scale_text}</span>
            <span>{drawing.sheet_mm.width}×{drawing.sheet_mm.height} mm</span>
            <span>{drawing.projection} angle</span>
            {drawing.sections.map((s) => (
              <span key={s.label}>{s.label} at {s.cut_at_mm.toFixed(1)} mm</span>
            ))}
          </div>
        </>
      )}

      {/* lifecycle + revisions */}
      <div className="flex items-center gap-1">
        {(['draft', 'in_review', 'released', 'obsolete'] as const).map((s) => (
          <button key={s} onClick={() => setState(s)}
            className={`flex-1 text-[9px] py-1 rounded-sm border transition-colors ${
              state === s ? 'border-cad-accent/60 text-cad-accent-hot bg-cad-accent/10' : 'border-cad-border text-cad-text hover:text-cad-text-heading'}`}>
            {LIFECYCLE_LABEL[s]}
          </button>
        ))}
      </div>
      <div className="flex gap-2">
        <input value={notes} onChange={(e) => setNotes(e.target.value)} placeholder="Change notes for this revision"
          className="flex-1 min-w-0 px-2 py-1.5 rounded-sm bg-cad-bg border border-cad-border text-cad-text-heading text-[11px] outline-none focus:border-cad-accent" />
        <button onClick={release} title="Cut a released revision"
          className="flex items-center gap-1.5 px-2.5 rounded-sm border border-cad-accent/50 text-cad-accent-hot hover:bg-cad-accent/10 text-[11px] font-medium transition">
          <Tag size={12} /> Release
        </button>
      </div>
      {revs.length > 0 && (
        <div className="space-y-1">
          {revs.map((r) => (
            <div key={r.letter} className="flex items-center gap-2 text-[10px] text-cad-text">
              <GitBranch size={11} className="text-cad-accent/70 shrink-0" />
              <span className="tabular font-semibold text-cad-text-heading">Rev {r.letter}</span>
              <span className="truncate flex-1">{r.notes || r.label}</span>
              <span className="tabular text-cad-text/40">{r.released_at.slice(0, 10)}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}


const STATUS_ICON = { pass: Check, warn: AlertTriangle, fail: X }
const STATUS_TONE = { pass: 'text-cad-success', warn: 'text-cad-warning', fail: 'text-cad-danger' }

const PROCESSES: Array<{ id: DfmProcess; label: string; blurb: string }> = [
  { id: 'additive', label: 'Print', blurb: 'LPBF metal — overhang, supports, printability and a quote.' },
  { id: 'milling', label: 'Mill', blurb: '3-axis tool access, wall thickness, smallest bore, trapped voids.' },
  { id: 'molding', label: 'Mould', blurb: 'Draft, undercuts against the pull direction, wall thickness.' },
  { id: 'casting', label: 'Cast', blurb: 'Draft, undercuts, wall thickness and trapped voids.' },
]

const AXES = ['auto', 'x', 'y', 'z'] as const

/**
 * Manufacturability, per process.
 *
 * The choice of process is not cosmetic: `additive` reports overhang, support
 * volume and a quote, and the other three report what needs real topology —
 * wall thickness, draft, undercuts, 3-axis tool access — measured on the exact
 * B-rep, so those figures do not move with meshing resolution.
 *
 * The missing quote on the non-additive processes is deliberate and is said so
 * in words. There is no cost model for milling, moulding or casting here, and an
 * invented number is worse than none: it is a signal that reads as real and
 * would be optimised against.
 */
function DfmSection() {
  const { busy, dfm, dfmProcess, runDfm } = useDocumentStore()
  const [process, setProcess] = useState<DfmProcess>('additive')
  const [axis, setAxis] = useState<(typeof AXES)[number]>('auto')
  const chosen = PROCESSES.find((p) => p.id === process)!
  // Matched on what was *asked for*, not on the report's `process` field —
  // that is a display label ('LPBF' for additive), so comparing it to the
  // selection would never match and a milling report would read as current
  // whichever tab you were on.
  const current = dfm && dfmProcess === process ? dfm : null

  return (
    <div className="space-y-3">
      <h3 className="sec-head">Manufacturability</h3>

      <div className="flex gap-px rounded-sm overflow-hidden border border-cad-border">
        {PROCESSES.map((p) => (
          <button
            key={p.id}
            onClick={() => setProcess(p.id)}
            className={`flex-1 py-1 text-[10px] transition-colors ${
              process === p.id ? 'bg-cad-accent text-cad-bg' : 'bg-cad-bg text-cad-text hover:text-cad-text-heading'
            }`}
          >
            {p.label}
          </button>
        ))}
      </div>
      <p className="text-[10px] text-cad-text/60 leading-snug">{chosen.blurb}</p>

      <div className="flex items-center gap-2">
        <label className="text-[10px] text-cad-text flex items-center gap-1">
          {process === 'additive' ? 'build' : process === 'milling' ? 'tool' : 'pull'}
          <select
            value={axis}
            onChange={(e) => setAxis(e.target.value as (typeof AXES)[number])}
            className="px-1.5 py-1 rounded-sm bg-cad-bg border border-cad-border text-cad-text-heading text-[11px] outline-none focus:border-cad-accent"
          >
            {AXES.map((a) => <option key={a} value={a}>{a === 'auto' ? 'auto' : `+${a.toUpperCase()}`}</option>)}
          </select>
        </label>
        <button
          onClick={() => runDfm(axis, process)}
          disabled={busy}
          className="flex-1 flex items-center justify-center gap-2 py-2 rounded-sm bg-cad-accent text-cad-bg text-xs font-semibold hover:bg-cad-accent-hot transition-colors disabled:opacity-50"
        >
          {busy ? <Loader2 size={14} className="animate-spin" /> : <Factory size={14} />}
          {busy ? 'Analyzing…' : `Check for ${chosen.label.toLowerCase()}`}
        </button>
      </div>

      {dfm && !current && (
        <p className="text-[10px] text-cad-text/50">
          Showing nothing — the last report was for{' '}
          <span className="text-cad-text">{PROCESSES.find((p) => p.id === dfmProcess)?.label.toLowerCase() ?? dfm.process}</span>.
          Run the check again for {chosen.label.toLowerCase()}.
        </p>
      )}

      {current && (
        <>
          <div className="flex items-center justify-between p-2.5 rounded-sm border border-cad-border bg-cad-bg/50">
            <div>
              {/* Keyed on the request, not on `current.process` — that reads
                  'LPBF' for additive, so comparing it to 'additive' is always
                  false and every report claimed to be about an axis. */}
              <div className="eyebrow text-cad-text/60">
                {dfmProcess === 'additive' ? 'Printability' : 'Manufacturability'}
              </div>
              <div className="tabular text-[10px] text-cad-text mt-1">
                {current.process}
                {current.build_orientation
                  ? ` · ${dfmProcess === 'additive' ? 'build' : 'pull'} ${current.build_orientation}`
                  : ''}
              </div>
            </div>
            <span className="font-display text-2xl font-semibold text-cad-accent-hot">
              {current.printability_score}
            </span>
          </div>

          <ul className="space-y-1.5">
            {current.checks.map((c: DfmCheck) => {
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

          {current.quote ? (
            <div className="pt-3 border-t border-cad-border">
              <h3 className="sec-head mb-2">Quote</h3>
              <dl className="space-y-1 text-[11px] text-cad-text">
                <Row label="Material" value={`$${current.quote.material_cost.toFixed(2)}`} />
                <Row label="Machine time" value={`$${current.quote.machine_cost.toFixed(2)}`} />
                <Row label="Setup" value={`$${current.quote.setup_cost.toFixed(2)}`} />
                <div className="flex items-center justify-between pt-1.5 mt-1 border-t border-cad-border">
                  <dt className="text-cad-text-heading font-medium">Total</dt>
                  <dd className="tabular text-cad-accent-hot font-semibold">${current.quote.total.toFixed(2)}</dd>
                </div>
                {current.quote.lead_time_days != null && (
                  <Row label="Lead time" value={`${current.quote.lead_time_days} days`} />
                )}
              </dl>
            </div>
          ) : (
            <p className="pt-3 border-t border-cad-border text-[10px] leading-snug text-cad-text/60">
              <span className="text-cad-text-heading">No quote for {chosen.label.toLowerCase()}.</span>{' '}
              There is no cost model for this process here, and a made-up figure would look exactly like
              a real one. The checks above are measured on the exact body.
            </p>
          )}
        </>
      )}
    </div>
  )
}

function LaserCut({ docId, docName }: { docId: string; docName: string }) {
  const [kerf, setKerf] = useState(0.2)
  const [res, setRes] = useState<{ path_count: number; cut_length_mm: number; estimated_minutes: number } | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const run = async () => {
    setBusy(true); setErr(null)
    try { setRes(await api.profileCut(docId, kerf)) }
    catch (e) { setErr(e instanceof Error ? e.message : 'Cut failed') }
    finally { setBusy(false) }
  }
  return (
    <div className="space-y-2">
      <h3 className="sec-head">Laser / waterjet cut</h3>
      <p className="text-[10px] text-cad-text/60">Cut the part outline from sheet — kerf-compensated G-code for laser, plasma or waterjet.</p>
      <div className="flex items-center gap-2">
        <label className="text-[10px] text-cad-text flex items-center gap-1">kerf
          <input type="number" value={kerf} step={0.05} min={0} onChange={(e) => setKerf(parseFloat(e.target.value) || 0)}
            className="tabular w-14 px-1.5 py-1 rounded-sm bg-cad-bg border border-cad-border text-cad-text-heading text-[11px] outline-none" />mm
        </label>
        <button onClick={run} disabled={busy}
          className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-sm bg-cad-accent text-cad-bg text-[11px] font-semibold hover:bg-cad-accent-hot transition-colors disabled:opacity-50">
          {busy ? <Loader2 size={12} className="animate-spin" /> : <Scissors size={12} />} Cut
        </button>
        {res && (
          <button onClick={() => api.downloadProfileCut(docId, docName, kerf).catch(() => {})}
            title="Download cut G-code" className="flex items-center justify-center px-2.5 py-1.5 rounded-sm border border-cad-border text-cad-text hover:text-cad-text-heading transition">
            <Download size={13} />
          </button>
        )}
      </div>
      {err && <p className="text-[10px] text-cad-danger">{err}</p>}
      {res && (
        <div className="tabular text-[10px] text-cad-text flex gap-3">
          <span>{res.path_count} path(s)</span>
          <span>{res.cut_length_mm.toFixed(0)} mm cut</span>
          <span>~{res.estimated_minutes} min</span>
        </div>
      )}
    </div>
  )
}

/**
 * Job prep for the selected machine: nest copies onto its sheet, then post the
 * program to its dialect. This is the last shaping step before Validate
 * simulates the job and Machine sends it.
 */
function JobPrep({ docId, docName }: { docId: string; docName: string }) {
  const { machineId, setMachineId } = useDocumentStore()
  const [machines, setMachines] = useState<MachineSpec[]>([])
  const [qty, setQty] = useState(4)
  const [nest, setNest] = useState<NestResult | null>(null)
  const [posted, setPosted] = useState<{ machine: { name: string; dialect: string }; lines: number; stripped: number; bytes: number; gcode: string } | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => { api.listMachines().then((r) => setMachines(r.machines)).catch(() => {}) }, [])
  const machine = machines.find((m) => m.id === machineId)
  const sheetCapable = machine && machine.bed[1] > 0 && machine.process !== 'fdm'

  const runNest = async () => {
    setBusy('nest'); setErr(null)
    try { setNest(await api.nestSheet(docId, machineId, qty)) }
    catch (e) { setErr(e instanceof Error ? e.message : 'Nesting failed') }
    finally { setBusy(null) }
  }
  const runPost = async () => {
    setBusy('post'); setErr(null)
    try { setPosted(await api.postGcode(docId, machineId, machine?.process === 'fdm' ? 'print' : 'cut')) }
    catch (e) { setErr(e instanceof Error ? e.message : 'Post failed') }
    finally { setBusy(null) }
  }
  const download = () => {
    if (!posted) return
    const blob = new Blob([posted.gcode], { type: 'text/plain' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `${docName.slice(0, 40).replace(/\s+/g, '_') || 'part'}_${machineId}.gcode`
    a.click()
    URL.revokeObjectURL(a.href)
  }

  return (
    <div className="pt-4 border-t border-cad-border space-y-3">
      <h3 className="sec-head">Job prep</h3>
      <div>
        <label className="eyebrow text-cad-text/60 block mb-1">Target machine</label>
        <select value={machineId} onChange={(e) => { setMachineId(e.target.value); setNest(null); setPosted(null) }}
          className="w-full px-2 py-1.5 rounded-sm bg-cad-bg border border-cad-border text-cad-text-heading text-xs focus:border-cad-accent outline-none">
          {machines.map((m) => (
            <option key={m.id} value={m.id}>{m.vendor} {m.name} · {m.process}</option>
          ))}
        </select>
        {machine && (
          <p className="text-[10px] text-cad-text/50 mt-1">
            {machine.bed[1] > 0 ? `${machine.bed[0]}×${machine.bed[1]} mm` : `${machine.bed[0]} mm`} · {machine.dialect}
          </p>
        )}
      </div>

      {sheetCapable && (
        <>
          <div className="flex items-center gap-2">
            <label className="text-[10px] text-cad-text flex items-center gap-1">qty
              <input type="number" min={1} value={qty} onChange={(e) => setQty(Math.max(1, parseInt(e.target.value, 10) || 1))}
                className="tabular w-14 px-1.5 py-1 rounded-sm bg-cad-bg border border-cad-border text-cad-text-heading text-[11px] outline-none" />
            </label>
            <button onClick={runNest} disabled={busy !== null}
              className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-sm border border-cad-accent/50 text-cad-accent-hot hover:bg-cad-accent/10 text-[11px] font-medium transition disabled:opacity-50">
              {busy === 'nest' ? <Loader2 size={12} className="animate-spin" /> : <Grid2x2 size={12} />} Nest sheet
            </button>
          </div>
          {nest && <NestView nest={nest} />}
        </>
      )}

      <button onClick={runPost} disabled={busy !== null}
        className="w-full flex items-center justify-center gap-1.5 py-2 rounded-sm border border-cad-border text-cad-text hover:text-cad-text-heading transition text-[11px] disabled:opacity-50">
        {busy === 'post' ? <Loader2 size={12} className="animate-spin" /> : <FileCog size={12} />}
        Post to {machine ? machine.dialect : 'machine'} dialect
      </button>
      {posted && (
        <div className="flex items-center justify-between text-[10px] text-cad-text">
          <span className="tabular">{posted.lines} lines · {(posted.bytes / 1024).toFixed(0)} KB{posted.stripped ? ` · ${posted.stripped} stripped` : ''}</span>
          <button onClick={download} className="flex items-center gap-1 text-cad-accent-hot hover:underline">
            <Download size={11} /> Download
          </button>
        </div>
      )}
      {err && <p className="text-[10px] text-cad-danger">{err}</p>}
    </div>
  )
}

/** The nested sheet, to scale. */
function NestView({ nest }: { nest: NestResult }) {
  const [w, h] = nest.machine.sheet_mm
  const pts = (poly: number[][]) => poly.map((p) => `${p[0]},${-p[1]}`).join(' ')
  return (
    <div className="space-y-1.5">
      <svg viewBox={`0 ${-h} ${w} ${h}`} className="w-full rounded-sm border border-cad-border bg-cad-bg"
        style={{ aspectRatio: `${w} / ${h}` }}>
        <rect x={0} y={-h} width={w} height={h} fill="none" stroke="#2c333e" strokeWidth={Math.max(w, h) / 400} />
        {nest.paths.map((p, i) => (
          <polyline key={i} points={pts(p)} fill="none" stroke="var(--color-cad-accent)" strokeWidth={Math.max(w, h) / 500} />
        ))}
      </svg>
      <div className="tabular text-[10px] text-cad-text flex flex-wrap gap-x-3">
        <span>{nest.placed}/{nest.requested} on sheet</span>
        <span>{nest.grid.cols}×{nest.grid.rows}</span>
        <span>{nest.utilisation_pct}% used</span>
        {nest.sheets_needed > 1 && <span>{nest.sheets_needed} sheets</span>}
      </div>
    </div>
  )
}

function Choice({ label, value, options, onChange }: {
  label: string; value: string; options: readonly string[]; onChange: (v: string) => void
}) {
  return (
    <label className="text-[10px] text-cad-text flex flex-col gap-1">
      <span className="eyebrow text-cad-text/60">{label}</span>
      <select value={value} onChange={(e) => onChange(e.target.value)}
        className="px-1.5 py-1 rounded-sm bg-cad-bg border border-cad-border text-cad-text-heading text-[11px] outline-none focus:border-cad-accent">
        {options.map((o) => <option key={o} value={o}>{o}</option>)}
      </select>
    </label>
  )
}

function Num({ label, value, step, min, suffix, onChange }: {
  label: string; value: number; step: number; min: number; suffix: string
  onChange: (v: number) => void
}) {
  return (
    <label className="text-[10px] text-cad-text flex flex-col gap-1">
      <span>{label}</span>
      <div className="flex items-center gap-1 px-2 py-1 rounded-sm bg-cad-bg border border-cad-border focus-within:border-cad-accent transition-colors">
        <input
          type="number"
          value={value}
          step={step}
          min={min}
          onChange={(e) => {
            const v = parseFloat(e.target.value)
            if (!Number.isNaN(v)) onChange(v)
          }}
          className="tabular w-full bg-transparent text-xs text-cad-text-heading outline-none"
        />
        <span className="text-[9px] text-cad-text/60">{suffix}</span>
      </div>
    </label>
  )
}

/** One sliced layer drawn to scale: walls in ember, infill in steel. */
function LayerView({ slice, index }: { slice: SliceResponse; index: number }) {
  const layer = slice.preview[index]
  if (!layer) return null
  const all = [...layer.walls, ...layer.skin, ...layer.infill].flat()
  if (all.length === 0) return <p className="text-[10px] text-cad-text/50">Empty layer.</p>
  const xs = all.map((p) => p[0])
  const ys = all.map((p) => p[1])
  const minX = Math.min(...xs), maxX = Math.max(...xs)
  const minY = Math.min(...ys), maxY = Math.max(...ys)
  const pad = 2
  const toPts = (poly: number[][]) => poly.map((p) => `${p[0]},${-p[1]}`).join(' ')
  return (
    <svg
      viewBox={`${minX - pad} ${-maxY - pad} ${maxX - minX + 2 * pad} ${maxY - minY + 2 * pad}`}
      className="w-full aspect-square rounded-sm border border-cad-border bg-cad-bg"
    >
      {layer.infill.map((poly, i) => (
        <polyline key={`i${i}`} points={toPts(poly)} fill="none"
          stroke="var(--color-stress-void)" strokeWidth={0.25} strokeLinecap="round" />
      ))}
      {layer.skin.map((poly, i) => (
        <polyline key={`s${i}`} points={toPts(poly)} fill="none"
          stroke="var(--color-cad-accent-hot)" strokeWidth={0.25} strokeLinecap="round" opacity={0.75} />
      ))}
      {layer.walls.map((poly, i) => (
        <polygon key={`w${i}`} points={toPts(poly)} fill="none"
          stroke="var(--color-cad-accent)" strokeWidth={0.35} strokeLinejoin="round" />
      ))}
    </svg>
  )
}

function SlicerSection() {
  const { doc, refreshDoc } = useDocumentStore()
  const [layerHeight, setLayerHeight] = useState(0.2)
  const [infill, setInfill] = useState(15)
  const [walls, setWalls] = useState(2)
  const [skins, setSkins] = useState(3)
  const [tempNozzle, setTempNozzle] = useState(210)
  const [tempBed, setTempBed] = useState(60)
  const [slicing, setSlicing] = useState(false)
  const [result, setResult] = useState<SliceResponse | null>(null)
  const [layerIdx, setLayerIdx] = useState(0)
  const [err, setErr] = useState<string | null>(null)

  const slice = async () => {
    if (!doc || slicing) return
    setSlicing(true)
    setErr(null)
    try {
      const res = await api.sliceDocument(doc.id, {
        layer_height: layerHeight,
        infill: infill / 100,
        wall_count: walls,
        skin_layers: skins,
        temp_nozzle: tempNozzle,
        temp_bed: tempBed,
      })
      setResult(res)
      setLayerIdx(Math.max(0, res.preview.length - 1))
      refreshDoc() // pick up derived.cam so Send job unlocks
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'Slicing failed')
    } finally {
      setSlicing(false)
    }
  }

  if (!doc) return null
  const s = result?.stats
  return (
    <div className="space-y-3">
      <h3 className="sec-head">Slice for printing</h3>
      <div className="grid grid-cols-2 gap-2">
        <Num label="Layer height" value={layerHeight} step={0.05} min={0.05} suffix="mm" onChange={setLayerHeight} />
        <Num label="Infill" value={infill} step={5} min={0} suffix="%" onChange={setInfill} />
        <Num label="Walls" value={walls} step={1} min={1} suffix="" onChange={setWalls} />
        <Num label="Top/bottom layers" value={skins} step={1} min={0} suffix="" onChange={setSkins} />
        <Num label="Nozzle" value={tempNozzle} step={5} min={150} suffix="°C" onChange={setTempNozzle} />
        <Num label="Bed" value={tempBed} step={5} min={0} suffix="°C" onChange={setTempBed} />
      </div>
      <button
        onClick={slice}
        disabled={slicing}
        className="w-full flex items-center justify-center gap-1.5 py-2 rounded-sm bg-cad-accent text-cad-bg font-semibold hover:bg-cad-accent-hot transition-colors text-xs disabled:opacity-50"
      >
        {slicing ? <Loader2 size={13} className="animate-spin" /> : <Layers size={13} />}
        {slicing ? 'Slicing…' : 'Slice'}
      </button>
      {err && <p className="text-[10px] text-cad-danger">{err}</p>}

      {result && s && (
        <div className="space-y-2.5">
          <div className="grid grid-cols-3 gap-2 text-center">
            {[
              [`${s.layers}`, 'layers'],
              [`${s.estimated_minutes < 90 ? `${Math.round(s.estimated_minutes)} min` : `${(s.estimated_minutes / 60).toFixed(1)} h`}`, 'print time'],
              [`${s.filament_g} g`, 'filament'],
            ].map(([v, l]) => (
              <div key={l} className="p-2 rounded-sm border border-cad-border bg-cad-bg/50">
                <div className="font-display text-sm font-semibold text-cad-text-heading">{v}</div>
                <div className="eyebrow text-cad-text/50 mt-0.5">{l}</div>
              </div>
            ))}
          </div>

          <LayerView slice={result} index={layerIdx} />
          <div className="flex items-center gap-2">
            <input
              type="range"
              min={0}
              max={result.preview.length - 1}
              value={layerIdx}
              onChange={(e) => setLayerIdx(parseInt(e.target.value, 10))}
              className="flex-1 accent-[var(--color-cad-accent)]"
            />
            <span className="tabular text-[10px] text-cad-text w-20 text-right">
              layer {result.preview[layerIdx]?.layer} · z{result.preview[layerIdx]?.z}
            </span>
          </div>

          <button
            onClick={() => api.downloadGcode(doc.id, doc.name).catch((e) => setErr(String(e)))}
            className="w-full flex items-center justify-center gap-1.5 py-1.5 rounded-sm border border-cad-border text-cad-text hover:text-cad-text-heading transition text-[11px]"
          >
            <Download size={12} /> Download G-code ({(s.gcode_bytes / 1e6).toFixed(1)} MB)
          </button>
        </div>
      )}
    </div>
  )
}

export function MakePanel() {
  const { doc, busy, cam, runCam, exportStep, downloadGcode } = useDocumentStore()
  const [diameter, setDiameter] = useState(3)
  const [stepdown, setStepdown] = useState(1)
  const [stepover, setStepover] = useState(1.2)
  if (!doc) return <p className="text-[11px] text-cad-text/60">Open a part to prepare it for manufacture.</p>

  return (
    <div className="space-y-5">
      <Documentation docId={doc.id} docName={doc.name} />

      <div className="pt-3 border-t border-cad-border" />

      <SlicerSection />

      <div className="pt-3 border-t border-cad-border" />

      <LaserCut docId={doc.id} docName={doc.name} />

      <div className="pt-3 border-t border-cad-border" />

      <DfmSection />

      <div className="flex justify-end">
        <button
          onClick={exportStep}
          title="Export STEP"
          className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-sm border border-cad-border text-cad-text hover:text-cad-text-heading hover:border-stress-void transition text-[11px]"
        >
          <Download size={13} /> STEP
        </button>
      </div>

      <JobPrep docId={doc.id} docName={doc.name} />

      {/* CAM — 2.5D profiling */}
      <div className="pt-4 border-t border-cad-border space-y-3">
        <h3 className="sec-head">CAM · 3-axis mill</h3>
        <div className="grid grid-cols-3 gap-2">
          <ToolField label="Tool Ø" value={diameter} unit="mm" onChange={setDiameter} />
          <ToolField label="Stepdown" value={stepdown} unit="mm" onChange={setStepdown} />
          <ToolField label="Stepover" value={stepover} unit="mm" onChange={setStepover} />
        </div>
        <button
          onClick={() => runCam({ diameter_mm: diameter, stepdown_mm: stepdown, stepover_mm: stepover, feed_mm_min: 600 })}
          disabled={busy}
          className="w-full flex items-center justify-center gap-2 py-2 rounded-sm border border-cad-accent/50 text-cad-accent-hot text-xs font-semibold hover:bg-cad-accent/10 transition-colors disabled:opacity-50"
        >
          {busy ? <Loader2 size={14} className="animate-spin" /> : <Route size={14} />}
          {busy ? 'Generating…' : 'Generate toolpaths'}
        </button>
        {cam && (
          <div className="space-y-2">
            <ul className="space-y-0.5">
              {cam.operations.map((op, i) => (
                <li key={i} className="text-[11px] text-cad-text flex items-start gap-1.5">
                  <span className="text-cad-accent mt-1.5 w-1 h-1 rounded-full bg-cad-accent shrink-0" />
                  {op}
                </li>
              ))}
            </ul>
            <div className="flex items-center justify-between text-[11px]">
              <span className="text-cad-text">Est. cycle time</span>
              <span className="tabular text-cad-text-heading">{cam.estimated_minutes.toFixed(1)} min</span>
            </div>
            <button
              onClick={downloadGcode}
              className="w-full flex items-center justify-center gap-2 py-2 rounded-sm bg-cad-accent text-cad-bg text-xs font-semibold hover:bg-cad-accent-hot transition-colors"
            >
              <Download size={14} /> Download G-code
            </button>
          </div>
        )}
      </div>
    </div>
  )
}

function ToolField({ label, value, unit, onChange }: { label: string; value: number; unit: string; onChange: (n: number) => void }) {
  return (
    <label className="text-[10px] text-cad-text flex flex-col gap-1">
      <span className="tabular">{label}</span>
      <div className="flex items-center gap-1 px-2 py-1 rounded-sm bg-cad-bg border border-cad-border focus-within:border-cad-accent transition-colors">
        <input
          type="number"
          step={0.5}
          min={0.5}
          value={value}
          onChange={(e) => {
            const n = parseFloat(e.target.value)
            if (!Number.isNaN(n) && n > 0) onChange(n)
          }}
          className="tabular w-full bg-transparent text-xs text-cad-text-heading outline-none"
        />
        <span className="text-[9px] text-cad-text/60">{unit}</span>
      </div>
    </label>
  )
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between">
      <dt>{label}</dt>
      <dd className="tabular text-cad-text-heading">{value}</dd>
    </div>
  )
}

import { useState } from 'react'
import { Loader2, Factory, Download, Check, AlertTriangle, X, Route, ShoppingCart, PackageCheck } from 'lucide-react'
import { useDocumentStore } from '../../store/useDocumentStore'
import { api } from '../../api/client'
import type { DfmCheck, QuoteBundle, ManufacturingOrder } from '../../../../shared/types'

const PROCESS_LABEL: Record<string, string> = {
  'cnc-3axis': 'CNC machining · Al 6061',
  fdm: 'FDM printing · PLA',
  sla: 'SLA printing · resin',
  'lpbf-metal': 'Metal printing · AlSi10Mg',
}

function GetItMade({ docId }: { docId: string }) {
  const [quantity, setQuantity] = useState(1)
  const [bundle, setBundle] = useState<QuoteBundle | null>(null)
  const [order, setOrder] = useState<ManufacturingOrder | null>(null)
  const [working, setWorking] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const getQuotes = async () => {
    setWorking(true)
    setError(null)
    setOrder(null)
    try {
      setBundle(await api.quoteDocument(docId, quantity))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Quoting failed')
    } finally {
      setWorking(false)
    }
  }

  const place = async (process: string) => {
    setWorking(true)
    setError(null)
    try {
      setOrder(await api.placeOrder(docId, process, quantity))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Order failed')
    } finally {
      setWorking(false)
    }
  }

  return (
    <div className="pt-4 border-t border-cad-border space-y-3">
      <h3 className="sec-head">Get it made</h3>
      <div className="flex items-end gap-2">
        <label className="text-[10px] text-cad-text flex flex-col gap-1 w-20">
          <span className="tabular">Quantity</span>
          <input
            type="number"
            min={1}
            step={1}
            value={quantity}
            onChange={(e) => setQuantity(Math.max(1, parseInt(e.target.value) || 1))}
            className="tabular px-2 py-1 rounded-sm bg-cad-bg border border-cad-border text-xs text-cad-text-heading outline-none focus:border-cad-accent"
          />
        </label>
        <button
          onClick={getQuotes}
          disabled={working}
          className="flex-1 flex items-center justify-center gap-2 py-2 rounded-sm bg-cad-accent text-cad-bg text-xs font-semibold hover:bg-cad-accent-hot transition-colors disabled:opacity-50"
        >
          {working ? <Loader2 size={14} className="animate-spin" /> : <ShoppingCart size={14} />}
          {working ? 'Working…' : 'Get instant quotes'}
        </button>
      </div>

      {error && <p className="text-[11px] text-cad-danger">{error}</p>}

      {order ? (
        <div className="p-3 rounded-sm border border-cad-success/50 bg-cad-success/5 space-y-1">
          <div className="flex items-center gap-2 text-cad-success text-xs font-semibold">
            <PackageCheck size={14} /> Order submitted
          </div>
          <p className="tabular text-[11px] text-cad-text">
            {PROCESS_LABEL[order.process] ?? order.process} · qty {order.quantity} · ${order.quote.total.toFixed(2)}
            {order.quote.lead_time_days != null && ` · ~${order.quote.lead_time_days} days`}
          </p>
          <p className="text-[10px] text-cad-text/60">Ref {order.id.slice(0, 8)} · {order.provider}</p>
        </div>
      ) : (
        bundle && (
          <div className="space-y-1.5">
            <p className="tabular text-[10px] text-cad-text/60">
              {bundle.volume_cm3.toFixed(1)} cm³ · {bundle.bbox_mm.map((v) => v.toFixed(0)).join('×')} mm · {bundle.provider}
            </p>
            {bundle.quotes.map((q) => (
              <div key={q.process} className="flex items-center justify-between p-2 rounded-sm border border-cad-border bg-cad-bg/50">
                <div>
                  <div className="text-[11px] text-cad-text-heading font-medium">{PROCESS_LABEL[q.process] ?? q.process}</div>
                  <div className="tabular text-[10px] text-cad-text/60">
                    ${q.total.toFixed(2)}{q.lead_time_days != null && ` · ${q.lead_time_days} days`}
                  </div>
                </div>
                <button
                  onClick={() => place(q.process)}
                  disabled={working}
                  className="px-2.5 py-1 rounded-sm border border-cad-accent/50 text-cad-accent-hot text-[10px] font-semibold hover:bg-cad-accent/10 transition-colors disabled:opacity-40"
                >
                  Order
                </button>
              </div>
            ))}
          </div>
        )
      )}
    </div>
  )
}

const STATUS_ICON = { pass: Check, warn: AlertTriangle, fail: X }
const STATUS_TONE = { pass: 'text-cad-success', warn: 'text-cad-warning', fail: 'text-cad-danger' }

export function MakePanel() {
  const { doc, busy, dfm, cam, runDfm, runCam, exportStep, downloadGcode } = useDocumentStore()
  const [diameter, setDiameter] = useState(3)
  const [stepdown, setStepdown] = useState(1)
  const [stepover, setStepover] = useState(1.2)
  if (!doc) return <p className="text-[11px] text-cad-text/60">Open a part to prepare it for manufacture.</p>

  return (
    <div className="space-y-5">
      <p className="text-xs text-cad-text leading-relaxed">
        Manufacturability checks and an instant quote for LPBF metal printing, plus a STEP export for
        any downstream CAM. Toolpath generation lands in the CAM step.
      </p>

      <div className="flex gap-2">
        <button
          onClick={() => runDfm('auto')}
          disabled={busy}
          className="flex-1 flex items-center justify-center gap-2 py-2.5 rounded-sm bg-cad-accent text-cad-bg text-xs font-semibold hover:bg-cad-accent-hot transition-colors disabled:opacity-50"
        >
          {busy ? <Loader2 size={14} className="animate-spin" /> : <Factory size={14} />}
          {busy ? 'Analyzing…' : 'Run DFM + quote'}
        </button>
        <button
          onClick={exportStep}
          title="Export STEP"
          className="flex items-center justify-center px-3 rounded-sm border border-cad-border text-cad-text hover:text-cad-text-heading hover:border-stress-void transition"
        >
          <Download size={15} />
        </button>
      </div>

      {dfm && (
        <>
          <div className="flex items-center justify-between p-2.5 rounded-sm border border-cad-border bg-cad-bg/50">
            <div>
              <div className="eyebrow text-cad-text/60">Printability</div>
              <div className="tabular text-[10px] text-cad-text mt-1">
                {dfm.process} · build {dfm.build_orientation ?? '—'}
              </div>
            </div>
            <span className="font-display text-2xl font-semibold text-cad-accent-hot">
              {dfm.printability_score}
            </span>
          </div>

          <div>
            <h3 className="sec-head mb-2">Checks</h3>
            <ul className="space-y-1.5">
              {dfm.checks.map((c: DfmCheck) => {
                const Icon = STATUS_ICON[c.status]
                return (
                  <li key={c.id} className="flex items-center gap-2 text-[11px]">
                    <Icon size={13} className={STATUS_TONE[c.status]} />
                    <span className="text-cad-text-heading flex-1">{c.label}</span>
                    <span className="tabular text-cad-text/70">{c.detail}</span>
                  </li>
                )
              })}
            </ul>
          </div>

          {dfm.quote && (
            <div className="pt-3 border-t border-cad-border">
              <h3 className="sec-head mb-2">Quote</h3>
              <dl className="space-y-1 text-[11px] text-cad-text">
                <Row label="Material" value={`$${dfm.quote.material_cost.toFixed(2)}`} />
                <Row label="Machine time" value={`$${dfm.quote.machine_cost.toFixed(2)}`} />
                <Row label="Setup" value={`$${dfm.quote.setup_cost.toFixed(2)}`} />
                <div className="flex items-center justify-between pt-1.5 mt-1 border-t border-cad-border">
                  <dt className="text-cad-text-heading font-medium">Total</dt>
                  <dd className="tabular text-cad-accent-hot font-semibold">${dfm.quote.total.toFixed(2)}</dd>
                </div>
                {dfm.quote.lead_time_days != null && (
                  <Row label="Lead time" value={`${dfm.quote.lead_time_days} days`} />
                )}
              </dl>
            </div>
          )}
        </>
      )}

      <GetItMade docId={doc.id} />

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

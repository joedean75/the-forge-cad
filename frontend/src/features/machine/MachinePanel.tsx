import { useEffect, useRef, useState } from 'react'
import { Home, Send, Loader2, Wifi, WifiOff, Download, ShoppingCart, PackageCheck } from 'lucide-react'
import { useDocumentStore } from '../../store/useDocumentStore'
import { api } from '../../api/client'
import type { MachineStatus, MachineSpec, QuoteBundle, ManufacturingOrder } from '../../../../shared/types'

const JOG_STEPS = [0.1, 1, 10]

const PROCESS_LABEL: Record<string, string> = {
  'cnc-3axis': 'CNC machining · Al 6061',
  fdm: 'FDM printing · PLA',
  sla: 'SLA printing · resin',
  'lpbf-metal': 'Metal printing · AlSi10Mg',
}

function SendOut({ docId }: { docId?: string }) {
  const [quantity, setQuantity] = useState(1)
  const [bundle, setBundle] = useState<QuoteBundle | null>(null)
  const [order, setOrder] = useState<ManufacturingOrder | null>(null)
  const [working, setWorking] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const getQuotes = async () => {
    if (!docId) return
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
    if (!docId) return
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
      <h3 className="sec-head">Send out for quote</h3>
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

export function MachinePanel() {
  const { doc, cam, machineId } = useDocumentStore()
  const [machines, setMachines] = useState<MachineSpec[]>([])
  const [url, setUrl] = useState('http://localhost:7125')
  const [status, setStatus] = useState<MachineStatus | null>(null)
  const [step, setStep] = useState(1)
  const [msg, setMsg] = useState<string | null>(null)
  const [sending, setSending] = useState(false)
  const timer = useRef<number | null>(null)

  useEffect(() => { api.listMachines().then((r) => setMachines(r.machines)).catch(() => {}) }, [])
  const machine = machines.find((m) => m.id === machineId)

  // Poll printer status while the panel is mounted.
  useEffect(() => {
    let cancelled = false
    const poll = async () => {
      try {
        const s = await api.machineStatus(url)
        if (!cancelled) setStatus(s)
      } catch {
        if (!cancelled) setStatus({ connected: false, error: 'unreachable' })
      }
      if (!cancelled) timer.current = window.setTimeout(poll, 2500)
    }
    poll()
    return () => {
      cancelled = true
      if (timer.current) window.clearTimeout(timer.current)
    }
  }, [url])

  const connected = status?.connected ?? false

  const jog = async (axis: string, dir: number) => {
    try {
      await api.machineJog(url, axis, dir * step)
    } catch (e) {
      setMsg(e instanceof Error ? e.message : 'Jog failed')
    }
  }

  const send = async (start: boolean) => {
    if (!doc) return
    setSending(true)
    setMsg(null)
    try {
      await api.machineSendDocument(url, doc.id, start)
      setMsg(start ? 'Uploaded and started print' : 'Uploaded to printer')
    } catch (e) {
      setMsg(e instanceof Error ? e.message : 'Upload failed')
    } finally {
      setSending(false)
    }
  }

  const hasGcode = !!(cam || doc?.derived.cam?.gcode_url)

  return (
    <div className="space-y-5">
      <MachineChooser />

      <SendOut docId={doc?.id} />

      {machine?.connection === 'moonraker' ? (
      <>
      <div className="pt-3 border-t border-cad-border">
        <h3 className="sec-head mb-2">Moonraker endpoint</h3>
        <input
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="http://printer.local:7125"
          className="tabular w-full px-2 py-1.5 rounded-sm bg-cad-bg border border-cad-border text-cad-text-heading text-xs focus:border-cad-accent outline-none"
        />
        <div className="flex items-center gap-1.5 mt-2">
          {connected ? <Wifi size={12} className="text-cad-success" /> : <WifiOff size={12} className="text-cad-text/50" />}
          <span className={`eyebrow ${connected ? 'text-cad-success' : 'text-cad-text/50'}`}>
            {connected ? `connected · ${status?.state ?? ''}` : 'not connected'}
          </span>
        </div>
      </div>

      {connected && status && (
        <>
          <div className="grid grid-cols-2 gap-2">
            <Temp label="Nozzle" now={status.extruder_temp} target={status.extruder_target} />
            <Temp label="Bed" now={status.bed_temp} target={status.bed_target} />
          </div>

          {(status.state === 'printing' || (status.progress ?? 0) > 0) && (
            <div>
              <div className="flex items-center justify-between mb-1">
                <span className="eyebrow text-cad-text">{status.filename || 'job'}</span>
                <span className="tabular text-xs text-cad-accent-hot">{Math.round((status.progress ?? 0) * 100)}%</span>
              </div>
              <div className="h-1.5 bg-stress-void/25 rounded-full overflow-hidden">
                <div
                  className="h-full rounded-full transition-all duration-500"
                  style={{
                    width: `${Math.round((status.progress ?? 0) * 100)}%`,
                    background:
                      'linear-gradient(90deg, var(--color-stress-void), var(--color-cad-accent) 70%, var(--color-cad-accent-hot))',
                  }}
                />
              </div>
            </div>
          )}

          <div>
            <div className="flex items-center justify-between mb-2">
              <h3 className="sec-head">Jog</h3>
              <div className="flex items-center gap-0.5 p-0.5 rounded-sm bg-cad-bg border border-cad-border">
                {JOG_STEPS.map((s) => (
                  <button
                    key={s}
                    onClick={() => setStep(s)}
                    className={`tabular px-1.5 py-0.5 rounded-[3px] text-[10px] ${step === s ? 'bg-cad-accent/15 text-cad-accent-hot' : 'text-cad-text'}`}
                  >
                    {s}
                  </button>
                ))}
                <span className="text-[9px] text-cad-text/50 px-1">mm</span>
              </div>
            </div>
            <div className="grid grid-cols-3 gap-1.5">
              <JogBtn label="X−" onClick={() => jog('X', -1)} />
              <JogBtn label="Y+" onClick={() => jog('Y', 1)} />
              <JogBtn label="X+" onClick={() => jog('X', 1)} />
              <JogBtn label="Z−" onClick={() => jog('Z', -1)} />
              <JogBtn label="Y−" onClick={() => jog('Y', -1)} />
              <JogBtn label="Z+" onClick={() => jog('Z', 1)} />
            </div>
            <button
              onClick={() => api.machineHome(url).catch((e) => setMsg(String(e)))}
              className="w-full flex items-center justify-center gap-1.5 mt-1.5 py-1.5 rounded-sm border border-cad-border text-cad-text hover:text-cad-text-heading transition text-[11px]"
            >
              <Home size={12} /> Home all
            </button>
          </div>
        </>
      )}

      <div className="pt-3 border-t border-cad-border space-y-2">
        <h3 className="sec-head">Run the job</h3>
        {!hasGcode && <p className="text-[10px] text-cad-text/60">Prepare the program in Make (slice, cut or CAM) first.</p>}
        <div className="flex gap-2">
          <button
            onClick={() => send(false)}
            disabled={!hasGcode || sending}
            className="flex-1 flex items-center justify-center gap-1.5 py-2 rounded-sm border border-cad-border text-cad-text hover:text-cad-text-heading transition text-xs disabled:opacity-40"
          >
            {sending ? <Loader2 size={13} className="animate-spin" /> : <Send size={13} />}
            Upload
          </button>
          <button
            onClick={() => send(true)}
            disabled={!hasGcode || sending}
            className="flex-1 flex items-center justify-center gap-1.5 py-2 rounded-sm bg-cad-accent text-cad-bg font-semibold hover:bg-cad-accent-hot transition-colors text-xs disabled:opacity-40"
          >
            Upload + start
          </button>
        </div>
        {msg && <p className="text-[10px] text-cad-text/70">{msg}</p>}
      </div>
      </>
      ) : (
        <DispatchJob docId={doc?.id} docName={doc?.name ?? 'part'} />
      )}
    </div>
  )
}

/** Pick the machine this job is destined for — drives Make, Validate and dispatch. */
function MachineChooser() {
  const { machineId, setMachineId } = useDocumentStore()
  const [machines, setMachines] = useState<MachineSpec[]>([])
  useEffect(() => { api.listMachines().then((r) => setMachines(r.machines)).catch(() => {}) }, [])
  const m = machines.find((x) => x.id === machineId)
  const ROUTE: Record<string, string> = {
    moonraker: 'Drive it live', dispatch: 'Export the job', quote: 'Send out for quote',
  }
  return (
    <div>
      <h3 className="sec-head mb-2">Machine</h3>
      <select value={machineId} onChange={(e) => setMachineId(e.target.value)}
        className="w-full px-2 py-1.5 rounded-sm bg-cad-bg border border-cad-border text-cad-text-heading text-xs focus:border-cad-accent outline-none">
        {machines.map((x) => <option key={x.id} value={x.id}>{x.vendor} {x.name}</option>)}
      </select>
      {m && (
        <div className="mt-2 p-2.5 rounded-sm border border-cad-border bg-cad-bg/50 space-y-1">
          <div className="flex items-center justify-between">
            <span className="eyebrow text-cad-text/60">{m.process}</span>
            <span className="text-[9px] px-1.5 py-0.5 rounded-sm border border-cad-accent/40 text-cad-accent-hot">
              {ROUTE[m.connection] ?? m.connection}
            </span>
          </div>
          <p className="tabular text-[10px] text-cad-text">
            {m.bed[1] > 0 ? `${m.bed[0]}×${m.bed[1]}${m.bed[2] ? `×${m.bed[2]}` : ''} mm` : `${m.bed[0]} mm`} · {m.dialect}
          </p>
          <p className="text-[10px] text-cad-text/60">{m.notes}</p>
        </div>
      )}
    </div>
  )
}

/** Machines we don't drive directly: hand off the posted program. */
function DispatchJob({ docId, docName }: { docId?: string; docName: string }) {
  const { machineId } = useDocumentStore()
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const exportJob = async () => {
    if (!docId) return
    setBusy(true); setMsg(null)
    try {
      const posted = await api.postGcode(docId, machineId, 'cut')
      const blob = new Blob([posted.gcode], { type: 'text/plain' })
      const a = document.createElement('a')
      a.href = URL.createObjectURL(blob)
      a.download = `${docName.slice(0, 40).replace(/\s+/g, '_')}_${machineId}.gcode`
      a.click()
      URL.revokeObjectURL(a.href)
      setMsg(`Job posted for ${posted.machine.name} (${posted.lines} lines) and downloaded.`)
    } catch (e) {
      setMsg(e instanceof Error ? e.message : 'Could not post the job')
    } finally { setBusy(false) }
  }
  return (
    <div className="pt-3 border-t border-cad-border space-y-2">
      <h3 className="sec-head">Run the job</h3>
      <p className="text-[10px] text-cad-text/60">
        This machine isn't driven live from here — export the posted program and load it at the control.
      </p>
      <button onClick={exportJob} disabled={busy || !docId}
        className="w-full flex items-center justify-center gap-1.5 py-2 rounded-sm bg-cad-accent text-cad-bg text-xs font-semibold hover:bg-cad-accent-hot transition-colors disabled:opacity-50">
        {busy ? <Loader2 size={13} className="animate-spin" /> : <Download size={13} />} Export job for machine
      </button>
      {msg && <p className="text-[10px] text-cad-text/70">{msg}</p>}
    </div>
  )
}

function Temp({ label, now, target }: { label: string; now?: number; target?: number }) {
  return (
    <div className="p-2.5 rounded-sm border border-cad-border bg-cad-bg/50">
      <div className="eyebrow text-cad-text/60">{label}</div>
      <div className="mt-1 flex items-baseline gap-1">
        <span className="font-display text-lg font-semibold text-cad-text-heading">{(now ?? 0).toFixed(0)}</span>
        <span className="tabular text-[10px] text-cad-text/60">/ {(target ?? 0).toFixed(0)}°C</span>
      </div>
    </div>
  )
}

function JogBtn({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className="tabular py-2 rounded-sm border border-cad-border text-cad-text-heading hover:border-cad-accent/50 hover:text-cad-accent-hot transition text-xs"
    >
      {label}
    </button>
  )
}

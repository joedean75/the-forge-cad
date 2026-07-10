import { useEffect, useRef, useState } from 'react'
import { Home, Send, Loader2, Wifi, WifiOff } from 'lucide-react'
import { useDocumentStore } from '../../store/useDocumentStore'
import { api } from '../../api/client'
import type { MachineStatus } from '../../../../shared/types'

const JOG_STEPS = [0.1, 1, 10]

export function MachinePanel() {
  const { doc, cam } = useDocumentStore()
  const [url, setUrl] = useState('http://localhost:7125')
  const [status, setStatus] = useState<MachineStatus | null>(null)
  const [step, setStep] = useState(1)
  const [msg, setMsg] = useState<string | null>(null)
  const [sending, setSending] = useState(false)
  const timer = useRef<number | null>(null)

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

  return (
    <div className="space-y-5">
      <div>
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
        <h3 className="sec-head">Send job</h3>
        {!cam && <p className="text-[10px] text-cad-text/60">Generate toolpaths in the Make step first.</p>}
        <div className="flex gap-2">
          <button
            onClick={() => send(false)}
            disabled={!cam || sending}
            className="flex-1 flex items-center justify-center gap-1.5 py-2 rounded-sm border border-cad-border text-cad-text hover:text-cad-text-heading transition text-xs disabled:opacity-40"
          >
            {sending ? <Loader2 size={13} className="animate-spin" /> : <Send size={13} />}
            Upload
          </button>
          <button
            onClick={() => send(true)}
            disabled={!cam || sending}
            className="flex-1 flex items-center justify-center gap-1.5 py-2 rounded-sm bg-cad-accent text-cad-bg font-semibold hover:bg-cad-accent-hot transition-colors text-xs disabled:opacity-40"
          >
            Upload + start
          </button>
        </div>
        {msg && <p className="text-[10px] text-cad-text/70">{msg}</p>}
      </div>
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

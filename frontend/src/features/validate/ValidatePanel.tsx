import { useEffect, useMemo, useRef, useState } from 'react'
import { Loader2, Play, Pause, RotateCcw, PlayCircle } from 'lucide-react'
import { useDocumentStore } from '../../store/useDocumentStore'
import { api } from '../../api/client'
import type { MachineSpec, SliceResponse } from '../../../../shared/types'

/**
 * Validate — watch the job run before it touches metal.
 *
 * Replays the program prepared in Make on the selected machine: an FDM print
 * builds up layer by layer, a laser/plasma job traces its cut paths in order.
 * Scrub or play it through to confirm the machine will make the right part.
 */

type Frame = { label: string; paths: number[][][]; solid?: number[][][] }

export function ValidatePanel() {
  const { doc, machineId } = useDocumentStore()
  const [machines, setMachines] = useState<MachineSpec[]>([])
  const [frames, setFrames] = useState<Frame[] | null>(null)
  const [idx, setIdx] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [stats, setStats] = useState<string | null>(null)
  const timer = useRef<number | null>(null)

  useEffect(() => { api.listMachines().then((r) => setMachines(r.machines)).catch(() => {}) }, [])
  const machine = machines.find((m) => m.id === machineId)

  // Reset when the part or machine changes — the old replay no longer applies.
  useEffect(() => { setFrames(null); setIdx(0); setPlaying(false) }, [doc?.derived.solid_hash, machineId])

  useEffect(() => {
    if (!playing || !frames) return
    timer.current = window.setInterval(() => {
      setIdx((i) => {
        if (i >= frames.length - 1) { setPlaying(false); return i }
        return i + 1
      })
    }, 90)
    return () => { if (timer.current) window.clearInterval(timer.current) }
  }, [playing, frames])

  if (!doc) return <p className="text-[11px] text-cad-text/60">Open a part to simulate its build.</p>

  const build = async () => {
    if (!machine) return
    setBusy(true); setErr(null); setFrames(null); setIdx(0)
    try {
      if (machine.process === 'fdm') {
        const s: SliceResponse = await api.sliceDocument(doc.id, { layer_height: 0.2, infill: 0.15 })
        setFrames(s.preview.map((l) => ({
          label: `Layer ${l.layer} · z${l.z.toFixed(2)} mm`,
          paths: [...l.walls, ...l.skin],
          solid: l.infill,
        })))
        setStats(`${s.stats.layers} layers · ${s.stats.estimated_minutes} min · ${s.stats.filament_g} g`)
      } else {
        const c = await api.profileCut(doc.id, 0.2)
        const paths = (c as unknown as { paths?: number[][][] }).paths ?? []
        // One frame per contour: the beam works through the profile in order.
        setFrames(paths.map((_, i) => ({ label: `Contour ${i + 1} of ${paths.length}`, paths: paths.slice(0, i + 1) })))
        setStats(`${c.path_count} paths · ${c.cut_length_mm.toFixed(0)} mm · ~${c.estimated_minutes} min`)
      }
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'Could not build the simulation')
    } finally {
      setBusy(false)
    }
  }

  const frame = frames?.[idx]
  return (
    <div className="space-y-4">
      <p className="text-xs text-cad-text leading-relaxed">
        Replay the job prepared in Make on{' '}
        <span className="text-cad-text-heading">{machine ? `${machine.vendor} ${machine.name}` : 'the selected machine'}</span>{' '}
        — confirm it builds the right part before you send it.
      </p>

      <button
        onClick={build}
        disabled={busy || !machine}
        className="w-full flex items-center justify-center gap-2 py-2.5 rounded-sm bg-cad-accent text-cad-bg text-xs font-semibold hover:bg-cad-accent-hot transition-colors disabled:opacity-50"
      >
        {busy ? <Loader2 size={14} className="animate-spin" /> : <PlayCircle size={14} />}
        {busy ? 'Preparing simulation…' : 'Simulate build'}
      </button>
      {err && <p className="text-[10px] text-cad-danger">{err}</p>}

      {frames && frame && (
        <>
          <BuildView frame={frame} />
          <div className="flex items-center gap-2">
            <button onClick={() => setPlaying((p) => !p)}
              className="flex items-center justify-center h-7 w-7 rounded-sm border border-cad-border text-cad-text hover:text-cad-accent-hot transition">
              {playing ? <Pause size={13} /> : <Play size={13} />}
            </button>
            <button onClick={() => { setIdx(0); setPlaying(false) }}
              className="flex items-center justify-center h-7 w-7 rounded-sm border border-cad-border text-cad-text hover:text-cad-text-heading transition">
              <RotateCcw size={12} />
            </button>
            <input type="range" min={0} max={frames.length - 1} value={idx}
              onChange={(e) => { setIdx(parseInt(e.target.value, 10)); setPlaying(false) }}
              className="flex-1 accent-[var(--color-cad-accent)]" />
          </div>
          <div className="flex items-center justify-between text-[10px]">
            <span className="tabular text-cad-text-heading">{frame.label}</span>
            <span className="tabular text-cad-text/50">{Math.round(((idx + 1) / frames.length) * 100)}%</span>
          </div>
          {stats && <p className="tabular text-[10px] text-cad-text/60">{stats}</p>}
        </>
      )}
    </div>
  )
}

/** The material laid down so far, to scale. */
function BuildView({ frame }: { frame: Frame }) {
  const view = useMemo(() => {
    const all = [...frame.paths, ...(frame.solid ?? [])].flat()
    if (!all.length) return null
    const xs = all.map((p) => p[0]); const ys = all.map((p) => p[1])
    const pad = 4
    const minX = Math.min(...xs) - pad, maxX = Math.max(...xs) + pad
    const minY = Math.min(...ys) - pad, maxY = Math.max(...ys) + pad
    return { box: `${minX} ${-maxY} ${maxX - minX} ${maxY - minY}` }
  }, [frame])
  if (!view) return <p className="text-[10px] text-cad-text/50">Nothing deposited on this step.</p>
  const pts = (poly: number[][]) => poly.map((p) => `${p[0]},${-p[1]}`).join(' ')
  return (
    <svg viewBox={view.box} className="w-full aspect-square rounded-sm border border-cad-border bg-cad-bg">
      {(frame.solid ?? []).map((p, i) => (
        <polyline key={`s${i}`} points={pts(p)} fill="none" stroke="var(--color-stress-void)" strokeWidth={0.3} />
      ))}
      {frame.paths.map((p, i) => (
        <polyline key={`p${i}`} points={pts(p)} fill="none" stroke="var(--color-cad-accent)"
          strokeWidth={0.45} strokeLinecap="round" strokeLinejoin="round" />
      ))}
    </svg>
  )
}

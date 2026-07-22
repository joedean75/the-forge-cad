import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Search, Box, Circle, Cylinder, Cone, Rotate3d, FlipHorizontal2, Spline, Layers,
  Blend, Triangle, Shell, CircleDot, Grid3x3, Move3d, PencilRuler, Boxes, Waypoints,
  Activity, Wrench, Printer, Undo2, Redo2, Save, Maximize, FileText, Scissors, Bot,
} from 'lucide-react'
import { useDocumentStore } from '../store/useDocumentStore'
import type { EditorMode } from '../../../shared/types'

type Cmd = { id: string; label: string; group: string; keywords?: string; icon: typeof Box; run: () => void }

/**
 * Command palette (Cmd/Ctrl-K): a searchable launcher for modes, creation and
 * modify tools, sketch, make and edit actions. Tools add a body with sensible
 * defaults — parameters are then tuned in the inspector — so the palette is a
 * fast, discoverable path to every workflow action without hunting the ribbon.
 */
export function CommandPalette({ onOpenCopilot }: { onOpenCopilot: () => void }) {
  const [open, setOpen] = useState(false)
  const [q, setQ] = useState('')
  const [active, setActive] = useState(0)
  const inputRef = useRef<HTMLInputElement>(null)
  const s = useDocumentStore()

  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        setOpen((v) => !v)
        setQ('')
        setActive(0)
      } else if (e.key === 'Escape') {
        setOpen(false)
      }
    }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [])

  useEffect(() => { if (open) setTimeout(() => inputRef.current?.focus(), 0) }, [open])

  const commands = useMemo<Cmd[]>(() => {
    const mode = (m: EditorMode, label: string, icon: typeof Box): Cmd =>
      ({ id: `mode-${m}`, label: `Go to ${label}`, group: 'Mode', icon, run: () => s.setMode(m) })
    const prim = (shape: string, label: string, icon: typeof Box): Cmd =>
      ({ id: `add-${shape}`, label: `Add ${label}`, group: 'Create', keywords: 'primitive body', icon,
         run: () => s.runOp('add_primitive', { shape, operation: 'union' }) })
    const form = (op: string, label: string, icon: typeof Box, kw: string): Cmd =>
      ({ id: op, label, group: 'Forms', keywords: kw, icon, run: () => s.runOp(op, { operation: 'union' }) })
    const mod = (modifier: string, label: string, icon: typeof Box): Cmd =>
      ({ id: `mod-${modifier}`, label, group: 'Modify', keywords: 'modifier', icon,
         run: () => s.runOp('add_modifier', { modifier }) })

    return [
      mode('model', 'Model', Boxes), mode('optimize', 'Optimize', Waypoints),
      mode('validate', 'Validate', Activity), mode('make', 'Make', Wrench), mode('machine', 'Machine', Printer),

      prim('box', 'box', Box), prim('cylinder', 'cylinder', Cylinder),
      prim('sphere', 'sphere', Circle), prim('cone', 'cone', Cone),

      form('add_revolve', 'Revolve profile', Rotate3d, 'shaft wheel pulley turned'),
      form('add_sweep', 'Sweep along path', Spline, 'pipe tube wire'),
      form('add_loft', 'Loft between profiles', Layers, 'adapter taper funnel'),
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

  const filtered = useMemo(() => {
    const query = q.trim().toLowerCase()
    if (!query) return commands
    return commands.filter((c) => (c.label + ' ' + c.group + ' ' + (c.keywords ?? '')).toLowerCase().includes(query))
  }, [q, commands])

  useEffect(() => { setActive(0) }, [q])

  if (!open) return null

  const run = (c: Cmd) => { c.run(); setOpen(false) }

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center pt-[12vh] bg-black/40 backdrop-blur-sm"
      onClick={() => setOpen(false)}>
      <div className="w-[560px] max-w-[92vw] bg-cad-panel border border-cad-border rounded-md shadow-2xl overflow-hidden"
        onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center gap-2 px-3 h-11 border-b border-cad-border">
          <Search size={15} className="text-cad-text/60" />
          <input
            ref={inputRef}
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'ArrowDown') { e.preventDefault(); setActive((a) => Math.min(a + 1, filtered.length - 1)) }
              else if (e.key === 'ArrowUp') { e.preventDefault(); setActive((a) => Math.max(a - 1, 0)) }
              else if (e.key === 'Enter' && filtered[active]) { e.preventDefault(); run(filtered[active]) }
            }}
            placeholder="Search commands — modes, tools, actions…"
            className="flex-1 bg-transparent text-sm text-cad-text-heading placeholder:text-cad-text/40 outline-none"
          />
          <kbd className="text-[9px] text-cad-text/40 border border-cad-border rounded px-1 py-0.5">esc</kbd>
        </div>
        <div className="max-h-[52vh] overflow-y-auto py-1">
          {filtered.length === 0 && <div className="px-3 py-4 text-xs text-cad-text/50">No matching command.</div>}
          {filtered.map((c, i) => (
            <button
              key={c.id}
              onMouseEnter={() => setActive(i)}
              onClick={() => run(c)}
              className={`w-full flex items-center gap-2.5 px-3 py-1.5 text-left transition-colors ${
                i === active ? 'bg-cad-accent/12 text-cad-text-heading' : 'text-cad-text hover:bg-cad-raised'}`}
            >
              <c.icon size={14} className={i === active ? 'text-cad-accent-hot' : 'text-cad-text/60'} />
              <span className="text-xs flex-1">{c.label}</span>
              <span className="eyebrow text-cad-text/35">{c.group}</span>
            </button>
          ))}
        </div>
      </div>
    </div>
  )
}

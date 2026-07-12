import { useEffect, useState } from 'react'
import { EditorHeader } from './EditorHeader'
import { LibraryDrawer } from './LibraryDrawer'
import { AccountDrawer } from './AccountDrawer'
import { HistoryShareDrawer } from './HistoryShareDrawer'
import { ModeSpine } from './ModeSpine'
import { useAuthStore } from '../store/useAuthStore'
import { SketchCanvas } from '../features/sketch/SketchCanvas'
import { DocumentViewport } from './DocumentViewport'
import { FeatureTreePanel } from '../features/tree/FeatureTreePanel'
import { ModelInspector } from '../features/model/ModelInspector'
import { OptimizePanel } from '../features/optimize/OptimizePanel'
import { ValidatePanel } from '../features/validate/ValidatePanel'
import { MakePanel } from '../features/make/MakePanel'
import { MachinePanel } from '../features/machine/MachinePanel'
import { useDocumentStore } from '../store/useDocumentStore'
import type { EditorMode } from '../../../shared/types'

const MODE_TITLE: Record<EditorMode, string> = {
  model: 'Model',
  optimize: 'Optimize',
  validate: 'Validate',
  make: 'Make',
  machine: 'Machine',
}

function RightPanel() {
  const { mode } = useDocumentStore()
  return (
    <aside className="w-80 border-l border-cad-border bg-cad-panel flex flex-col shrink-0 z-10">
      <div className="h-11 border-b border-cad-border flex items-center gap-2.5 px-4">
        <span aria-hidden className="w-0.5 h-3.5 bg-cad-accent rounded-full" />
        <div className="leading-none">
          <span className="eyebrow text-cad-text/70">Inspector</span>
          <h2 className="font-display text-sm font-semibold text-cad-text-heading leading-none mt-1">
            {MODE_TITLE[mode]}
          </h2>
        </div>
      </div>
      <div className="flex-1 overflow-y-auto p-4">
        {mode === 'model' ? (
          <ModelInspector />
        ) : mode === 'optimize' ? (
          <OptimizePanel />
        ) : mode === 'validate' ? (
          <ValidatePanel />
        ) : mode === 'make' ? (
          <MakePanel />
        ) : (
          <MachinePanel />
        )}
      </div>
    </aside>
  )
}

function Readout({ label, value }: { label: string; value: string }) {
  return (
    <span className="flex items-baseline gap-1">
      <span className="eyebrow text-cad-text/45">{label}</span>
      <span className="tabular text-[10px] text-cad-text">{value}</span>
    </span>
  )
}

function StatusBar() {
  const { doc, mode, busy, error } = useDocumentStore()
  const d = doc?.derived
  return (
    <div className="h-8 border-t border-cad-border bg-cad-panel px-4 flex items-center gap-3 shrink-0">
      {/* machine state */}
      <span className={`w-1.5 h-1.5 rounded-full ${busy ? 'bg-cad-accent-hot animate-filament' : error ? 'bg-cad-danger' : 'bg-cad-success'}`} />
      <span className="eyebrow text-cad-text">{busy ? 'evaluating' : error ? 'fault' : 'ready'}</span>

      {/* active stage */}
      <span className="flex items-center gap-1.5 pl-1">
        <span aria-hidden className="w-0.5 h-3 bg-cad-accent rounded-full" />
        <span className="eyebrow text-cad-text/80">{MODE_TITLE[mode]}</span>
      </span>

      {/* live telemetry */}
      {error ? (
        <span className="text-[11px] text-cad-danger truncate flex-1">{error}</span>
      ) : (
        <div className="flex items-center gap-4 ml-1">
          {d?.mass != null && d.mass > 0 && <Readout label="mass" value={`${d.mass.toFixed(3)} kg`} />}
          {d?.volume != null && d.volume > 0 && <Readout label="vol" value={`${(d.volume * 1e6).toFixed(1)} cm³`} />}
        </div>
      )}

      {d?.solid_hash && (
        <span className="tabular text-[10px] text-cad-text/50 ml-auto">
          <span className="text-cad-text/35">sha</span> {d.solid_hash}
        </span>
      )}
    </div>
  )
}

export function Layout() {
  const [libraryOpen, setLibraryOpen] = useState(false)
  const [accountOpen, setAccountOpen] = useState(false)
  const [historyOpen, setHistoryOpen] = useState(false)
  const initAuth = useAuthStore((s) => s.init)

  // Load the current session + plan catalog once on mount.
  useEffect(() => {
    initAuth()
  }, [initAuth])

  // Undo/redo keyboard shortcuts (ignored while typing in a field).
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return
      if (!(e.ctrlKey || e.metaKey)) return
      const k = e.key.toLowerCase()
      if (k === 'z' && !e.shiftKey) {
        e.preventDefault()
        useDocumentStore.getState().undo()
      } else if (k === 'y' || (k === 'z' && e.shiftKey)) {
        e.preventDefault()
        useDocumentStore.getState().redo()
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [])

  return (
    <div className="flex flex-col h-full">
      <EditorHeader
        onOpenLibrary={() => setLibraryOpen(true)}
        onOpenAccount={() => setAccountOpen(true)}
        onOpenHistory={() => setHistoryOpen(true)}
      />
      <div className="flex flex-1 min-h-0">
        <ModeSpine />
        <FeatureTreePanel />
        <DocumentViewport />
        <RightPanel />
      </div>
      <StatusBar />
      <LibraryDrawer open={libraryOpen} onClose={() => setLibraryOpen(false)} />
      <AccountDrawer open={accountOpen} onClose={() => setAccountOpen(false)} />
      <HistoryShareDrawer open={historyOpen} onClose={() => setHistoryOpen(false)} />
      <SketchCanvas />
    </div>
  )
}

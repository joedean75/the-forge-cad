import { lazy, Suspense, useState, type ComponentType } from 'react'
import { Loader2 } from 'lucide-react'
import { EditorHeader } from '../components/EditorHeader'
import { ToolRibbon } from '../components/ToolRibbon'
import { CommandPalette } from '../components/CommandPalette'
import { AccountDrawer } from '../components/AccountDrawer'
import { HistoryShareDrawer } from '../components/HistoryShareDrawer'
import { CopilotPanel } from '../components/CopilotPanel'
import { ModeSpine } from '../components/ModeSpine'
import { DocumentViewport } from '../components/DocumentViewport'
import { FeatureTreePanel } from '../features/tree/FeatureTreePanel'
import { ModelInspector } from '../features/model/ModelInspector'
import { useDocumentStore } from '../store/useDocumentStore'
import { useNavStore } from '../store/useNavStore'
import { useCollab } from '../hooks/useCollab'
import type { EditorMode } from '../../../shared/types'

// Model is the mode the editor opens in and the one most sessions never leave;
// the other five carry the slicer, the drawing viewer and the FEA readouts
// between them. Same for the sketch canvas, which is mounted always and open
// rarely. Each is its own chunk, fetched the first time the mode is entered.
const lazyPanel = (load: () => Promise<Record<string, ComponentType>>, name: string) =>
  lazy(() => load().then((m) => ({ default: m[name] })))

const OptimizePanel = lazyPanel(() => import('../features/optimize/OptimizePanel'), 'OptimizePanel')
const AnalysisPanel = lazyPanel(() => import('../features/analysis/AnalysisPanel'), 'AnalysisPanel')
const ValidatePanel = lazyPanel(() => import('../features/validate/ValidatePanel'), 'ValidatePanel')
const MakePanel = lazyPanel(() => import('../features/make/MakePanel'), 'MakePanel')
const MachinePanel = lazyPanel(() => import('../features/machine/MachinePanel'), 'MachinePanel')
const SketchCanvas = lazyPanel(() => import('../features/sketch/SketchCanvas'), 'SketchCanvas')

const MODE_TITLE: Record<EditorMode, string> = {
  model: 'Model',
  optimize: 'Optimize',
  analysis: 'Analysis',
  make: 'Make',
  validate: 'Validate',
  machine: 'Machine',
}

function PanelFallback() {
  return (
    <div className="flex items-center gap-2 text-xs text-cad-text/50 py-6">
      <Loader2 size={13} className="animate-spin" /> Loading panel…
    </div>
  )
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
        <Suspense fallback={<PanelFallback />}>
        {mode === 'model' ? (
          <ModelInspector />
        ) : mode === 'optimize' ? (
          <OptimizePanel />
        ) : mode === 'analysis' ? (
          <AnalysisPanel />
        ) : mode === 'make' ? (
          <MakePanel />
        ) : mode === 'validate' ? (
          <ValidatePanel />
        ) : (
          <MachinePanel />
        )}
        </Suspense>
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
      <span className={`w-1.5 h-1.5 rounded-full ${busy ? 'bg-cad-accent-hot animate-filament' : error ? 'bg-cad-danger' : 'bg-cad-success'}`} />
      <span className="eyebrow text-cad-text">{busy ? 'evaluating' : error ? 'fault' : 'ready'}</span>
      <span className="flex items-center gap-1.5 pl-1">
        <span aria-hidden className="w-0.5 h-3 bg-cad-accent rounded-full" />
        <span className="eyebrow text-cad-text/80">{MODE_TITLE[mode]}</span>
      </span>
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

export function EditorScreen() {
  const [accountOpen, setAccountOpen] = useState(false)
  const [historyOpen, setHistoryOpen] = useState(false)
  const [copilotOpen, setCopilotOpen] = useState(false)
  const go = useNavStore((s) => s.go)
  const docId = useDocumentStore((s) => s.doc?.id)
  const peers = useCollab(docId)

  return (
    <div className="flex flex-col h-full">
      <EditorHeader
        onOpenLibrary={() => go('library')}
        onOpenAccount={() => setAccountOpen(true)}
        onOpenHistory={() => setHistoryOpen(true)}
        onOpenCopilot={() => setCopilotOpen((v) => !v)}
        copilotOpen={copilotOpen}
        peers={peers}
      />
      <ToolRibbon />
      <div className="flex flex-1 min-h-0">
        <ModeSpine />
        <FeatureTreePanel />
        <DocumentViewport />
        <RightPanel />
      </div>
      <StatusBar />
      <CommandPalette onOpenCopilot={() => setCopilotOpen(true)} />
      <AccountDrawer open={accountOpen} onClose={() => setAccountOpen(false)} />
      <HistoryShareDrawer open={historyOpen} onClose={() => setHistoryOpen(false)} />
      <CopilotPanel open={copilotOpen} onClose={() => setCopilotOpen(false)} />
      {/* No fallback: the canvas renders nothing until it is opened, so a
          spinner here would be a spinner over the viewport for no reason. */}
      <Suspense fallback={null}><SketchCanvas /></Suspense>
    </div>
  )
}

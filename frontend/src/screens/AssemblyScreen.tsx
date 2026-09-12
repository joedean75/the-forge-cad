import { useEffect, useState } from 'react'
import {
  ArrowLeft, Plus, Trash2, Boxes, Package, X, Loader2, AlertTriangle, ScanEye, UserCircle2, PencilRuler,
  Link2, Bot,
} from 'lucide-react'
import { api } from '../api/client'
import { useAssemblyStore } from '../store/useAssemblyStore'
import { useDocumentStore } from '../store/useDocumentStore'
import { useAuthStore } from '../store/useAuthStore'
import { useNavStore } from '../store/useNavStore'
import { AccountDrawer } from '../components/AccountDrawer'
import { AssemblyViewport } from '../components/AssemblyViewport'
import { CopilotPanel } from '../components/CopilotPanel'
import { PanelRail, usePanelCollapse } from '../components/PanelRail'
import { BomPanel, ClashPanel, MatesPanel } from '../features/assembly/AssemblyInspector'
import { StandardPartsPicker } from '../features/assembly/StandardPartsPicker'
import type { AssemblyComponent, ComponentTransform, DocumentSummary } from '../../../shared/types'

/** A labelled numeric field bound to one axis of a component transform. */
function NumField({ label, value, step, onChange }: { label: string; value: number; step: number; onChange: (v: number) => void }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="eyebrow text-cad-text/55">{label}</span>
      <input
        type="number"
        value={Number.isFinite(value) ? +value.toFixed(3) : 0}
        step={step}
        onChange={(e) => onChange(parseFloat(e.target.value) || 0)}
        className="tabular w-full px-2 py-1 rounded-sm bg-cad-bg border border-cad-border text-cad-text-heading text-[11px] outline-none focus:border-cad-accent"
      />
    </label>
  )
}

function PlacementInspector({ component, onChange, driven }: {
  component: AssemblyComponent
  onChange: (t: ComponentTransform) => void
  /** This component's position is an *output* of the mate solver. */
  driven: boolean
}) {
  const t = component.transform
  const set = (patch: Partial<ComponentTransform>) => onChange({ ...t, ...patch })
  const setT = (axis: 'x' | 'y' | 'z', vMm: number) => set({ translate: { ...t.translate, [axis]: vMm / 1000 } })
  const setR = (axis: 'x' | 'y' | 'z', deg: number) => set({ rotate_deg: { ...t.rotate_deg, [axis]: deg } })

  return (
    <div className="space-y-4">
      {/* A mate writes this component's translation, so typing one here is a
          number the next solve overwrites — the user types, watches it snap
          back, and concludes the tool is broken. Disable it and say why. */}
      {driven && (
        <p className="flex items-start gap-1.5 p-2 rounded-sm border border-cad-accent/30 bg-cad-accent/[0.05] text-[10px] text-cad-accent-hot">
          <Link2 size={11} className="mt-px shrink-0" />
          A mate positions this part. Remove the mate to place it by hand.
        </p>
      )}
      <div className={driven ? 'opacity-45 pointer-events-none' : undefined}>
        <span className="eyebrow text-cad-text/60 block mb-1.5">Position (mm)</span>
        <div className="grid grid-cols-3 gap-2">
          <NumField label="X" step={1} value={t.translate.x * 1000} onChange={(v) => setT('x', v)} />
          <NumField label="Y" step={1} value={t.translate.y * 1000} onChange={(v) => setT('y', v)} />
          <NumField label="Z" step={1} value={t.translate.z * 1000} onChange={(v) => setT('z', v)} />
        </div>
      </div>
      <div>
        <span className="eyebrow text-cad-text/60 block mb-1.5">Rotation (°)</span>
        <div className="grid grid-cols-3 gap-2">
          <NumField label="X" step={5} value={t.rotate_deg.x} onChange={(v) => setR('x', v)} />
          <NumField label="Y" step={5} value={t.rotate_deg.y} onChange={(v) => setR('y', v)} />
          <NumField label="Z" step={5} value={t.rotate_deg.z} onChange={(v) => setR('z', v)} />
        </div>
      </div>
      <div className="w-1/3">
        <span className="eyebrow text-cad-text/60 block mb-1.5">Scale</span>
        <NumField label="×" step={0.1} value={t.scale} onChange={(v) => set({ scale: v > 0 ? v : 1 })} />
      </div>
    </div>
  )
}

/** Part picker: choose one of the account's part documents to insert. */
function PartPicker({ onPick, onCreate, creating, onClose }: {
  onPick: (id: string) => void
  onCreate: () => void
  creating: boolean
  onClose: () => void
}) {
  const [items, setItems] = useState<DocumentSummary[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    api.listDocuments().then(setItems).catch(() => setItems([])).finally(() => setLoading(false))
  }, [])

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm" onClick={onClose}>
      <div className="w-[min(720px,92vw)] max-h-[80vh] flex flex-col bg-cad-panel border border-cad-border rounded-md overflow-hidden shadow-2xl" onClick={(e) => e.stopPropagation()}>
        <div className="h-12 border-b border-cad-border flex items-center justify-between px-4 shrink-0">
          <div className="flex items-center gap-2.5">
            <span aria-hidden className="w-0.5 h-3.5 bg-cad-accent rounded-full" />
            <h2 className="font-display text-sm font-semibold text-cad-text-heading">Insert a part</h2>
          </div>
          <div className="flex items-center gap-2">
            {/* Nothing to insert yet is the common case on a new assembly, so
                modelling a part is offered right here rather than sending the
                user back to the library to start over. */}
            <button
              onClick={onCreate}
              disabled={creating}
              title="Model a new part and place it in this assembly"
              className="flex items-center gap-1.5 h-7 px-2.5 rounded-sm bg-cad-accent text-cad-bg text-[11px] font-semibold hover:bg-cad-accent-hot transition-colors disabled:opacity-50"
            >
              {creating ? <Loader2 size={12} className="animate-spin" /> : <PencilRuler size={12} />}
              New part
            </button>
            <button onClick={onClose} className="p-1.5 rounded-sm text-cad-text hover:text-cad-text-heading hover:bg-cad-raised"><X size={16} /></button>
          </div>
        </div>
        <div className="flex-1 overflow-y-auto p-4">
          {loading ? (
            <div className="flex items-center justify-center h-40 text-cad-text gap-2 text-xs"><Loader2 size={14} className="animate-spin" /> Loading parts…</div>
          ) : items.length === 0 ? (
            <div className="flex flex-col items-center justify-center h-40 gap-2 text-center">
              <Package size={30} className="text-cad-border" />
              <p className="text-sm text-cad-text">No parts to insert</p>
              <p className="text-[11px] text-cad-text/60">Model one with <span className="text-cad-accent-hot">New part</span> — it lands in this assembly.</p>
            </div>
          ) : (
            <div className="grid grid-cols-2 sm:grid-cols-3 gap-2.5">
              {items.map((it) => (
                <button
                  key={it.id}
                  onClick={() => onPick(it.id)}
                  className="group text-left p-3 rounded-sm border border-cad-border bg-cad-bg/40 hover:border-cad-accent/50 hover:bg-cad-accent/5 transition-colors"
                >
                  <Package size={18} className="text-cad-text/70 group-hover:text-cad-accent" />
                  <div className="mt-2 text-xs text-cad-text-heading font-medium truncate">{it.name}</div>
                  <div className="eyebrow text-cad-text/60 mt-1">{it.provenance.replace('_', '-')}</div>
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

type InspectorTab = 'place' | 'parts' | 'mates' | 'fit' | 'bom'

const TABS: { id: InspectorTab; label: string }[] = [
  { id: 'place', label: 'Place' },
  { id: 'parts', label: 'Parts' },
  { id: 'mates', label: 'Mates' },
  { id: 'fit', label: 'Fit' },
  { id: 'bom', label: 'BOM' },
]

const TAB_TITLE: Record<InspectorTab, string> = {
  place: 'Placement', parts: 'Purchased parts', mates: 'Mates',
  fit: 'Interference', bom: 'Bill of materials',
}

export function AssemblyScreen() {
  const { assembly, busy, error, selectedComponentId, insertPart, removeComponent, setTransform,
    rename, select, markStale, refresh, stale, refreshing, drivenComponentIds } = useAssemblyStore()
  const { openDocument, newBlank } = useDocumentStore()
  const account = useAuthStore((s) => s.account)
  const go = useNavStore((s) => s.go)
  const goFrom = useNavStore((s) => s.goFrom)
  const [picking, setPicking] = useState(false)
  const [isolate, setIsolate] = useState(false)
  const [accountOpen, setAccountOpen] = useState(false)
  const [editingName, setEditingName] = useState(false)
  const [creatingPart, setCreatingPart] = useState(false)
  const [tab, setTab] = useState<InspectorTab>('place')
  const [copilotOpen, setCopilotOpen] = useState(false)
  const [listHidden, toggleList] = usePanelCollapse('assembly.components')
  const [inspectorHidden, toggleInspector] = usePanelCollapse('assembly.inspector')

  // Coming back from the part editor: pull the assembly again so a component
  // shows the geometry as just edited rather than the copy we left with.
  useEffect(() => { refresh() }, [refresh])

  // Modelling happens in the part editor, so the assembly hands off to it and
  // remembers the way back — a component's geometry is a part document, not
  // something you edit in place here.
  const toEditor = () => {
    if (!assembly) return
    markStale()  // the part's geometry may change while we're away
    goFrom('editor', { screen: 'assembly', label: assembly.name })
  }

  const editPart = async (partId: string) => {
    await openDocument(partId)
    toEditor()
  }

  /** Model a brand-new part and place it in this assembly in one step. */
  const createPart = async () => {
    setCreatingPart(true)
    try {
      await newBlank()
      const created = useDocumentStore.getState().doc
      if (created) {
        await insertPart(created.id)
        setPicking(false)
        toEditor()
      }
    } finally {
      setCreatingPart(false)
    }
  }

  if (!assembly) {
    return (
      <div className="h-full flex items-center justify-center text-cad-text text-sm">
        <Loader2 size={16} className="animate-spin mr-2" /> Loading assembly…
      </div>
    )
  }

  const selected = assembly.components.find((c) => c.id === selectedComponentId) ?? null
  const driven = drivenComponentIds()

  return (
    <div className="flex flex-col h-full">
      {/* header */}
      <header className="h-14 border-b border-cad-border bg-cad-panel flex items-center gap-4 pl-3 pr-4 shrink-0 z-20">
        <button
          onClick={() => go('library')}
          className="flex items-center gap-1.5 h-9 px-2.5 rounded-sm border border-cad-border text-cad-text hover:text-cad-text-heading hover:border-stress-void transition-colors"
        >
          <ArrowLeft size={14} /> <span className="text-[11px] font-medium">Library</span>
        </button>
        <div className="flex items-center gap-2.5 min-w-0">
          <Boxes size={18} className="text-cad-accent shrink-0" />
          <div className="leading-none min-w-0">
            <span className="eyebrow text-cad-text/70">Assembly</span>
            {editingName ? (
              <input
                autoFocus
                defaultValue={assembly.name}
                onBlur={(e) => { rename(e.target.value.trim() || assembly.name); setEditingName(false) }}
                onKeyDown={(e) => e.key === 'Enter' && (e.target as HTMLInputElement).blur()}
                className="block w-56 bg-transparent font-display text-sm font-semibold text-cad-text-heading leading-none mt-1 outline-none border-b border-cad-accent"
              />
            ) : (
              <h1 onClick={() => setEditingName(true)} title="Rename" className="font-display text-sm font-semibold text-cad-text-heading leading-none mt-1 truncate cursor-text">
                {assembly.name}
              </h1>
            )}
          </div>
        </div>

        <div className="flex-1" />
        <span className="tabular text-[10px] text-cad-text/60">{assembly.components.length} part{assembly.components.length === 1 ? '' : 's'}</span>
        <button
          onClick={() => setCopilotOpen((v) => !v)}
          title="Forge Copilot — place, mate and measure components"
          aria-pressed={copilotOpen}
          className={`flex items-center gap-1.5 h-9 px-2.5 rounded-sm border transition-colors ${
            copilotOpen
              ? 'border-cad-accent text-cad-accent bg-cad-accent/10'
              : 'border-cad-border text-cad-text hover:text-cad-text-heading hover:border-stress-void'
          }`}
        >
          <Bot size={15} /> <span className="text-[11px] font-medium">Copilot</span>
        </button>
        <button
          onClick={() => setAccountOpen(true)}
          title={account ? `${account.email} · ${account.plan.name} plan` : 'Account'}
          className="flex items-center gap-1.5 h-9 px-2.5 rounded-sm border border-cad-border text-cad-text hover:text-cad-text-heading hover:border-stress-void transition-colors"
        >
          <UserCircle2 size={15} /> <span className="text-[11px] font-medium max-w-[90px] truncate">{account ? account.plan.name : 'Account'}</span>
        </button>
      </header>

      <div className="flex flex-1 min-h-0">
        {/* components panel */}
        {!listHidden && (
        <aside className="w-72 border-r border-cad-border bg-cad-panel flex flex-col shrink-0 z-10">
          <div className="h-11 border-b border-cad-border flex items-center justify-between px-4">
            <span className="eyebrow text-cad-text/70">Components</span>
            <button
              onClick={() => setPicking(true)}
              disabled={busy}
              className="flex items-center gap-1 px-2 py-1 rounded-sm bg-cad-accent text-cad-bg text-[10px] font-semibold hover:bg-cad-accent-hot transition-colors disabled:opacity-40"
            >
              <Plus size={12} /> Insert part
            </button>
          </div>
          <div className="flex-1 overflow-y-auto py-2">
            {assembly.components.length === 0 ? (
              <p className="px-4 py-6 text-center text-[11px] text-cad-text/60">
                No parts yet. Click <span className="text-cad-accent-hot">Insert part</span> to compose this assembly.
              </p>
            ) : (
              assembly.components.map((c, i) => {
                const active = c.id === selectedComponentId
                return (
                  <div key={c.id} className="group flex items-center pr-1.5" style={{ paddingLeft: 12 }}>
                    <button
                      onClick={() => select(active ? null : c.id)}
                      className={`flex-1 flex items-center gap-2 py-1.5 min-w-0 text-left transition-colors ${
                        c.missing ? 'text-cad-danger' : active ? 'text-cad-accent-hot' : 'text-cad-text hover:text-cad-text-heading'
                      }`}
                    >
                      {c.missing ? <AlertTriangle size={14} /> : <Package size={14} className={active ? 'text-cad-accent' : 'text-cad-text/70'} />}
                      <span className="text-xs truncate">{c.name}</span>
                      <span className="tabular text-[9px] text-cad-text/40 ml-auto">#{i + 1}</span>
                    </button>
                    <button
                      onClick={() => editPart(c.part_id)}
                      disabled={c.missing}
                      title={c.missing ? 'This part no longer exists' : `Edit ${c.name} in the part editor`}
                      className="opacity-0 group-hover:opacity-100 text-cad-text hover:text-cad-accent transition ml-1 disabled:opacity-0"
                    >
                      <PencilRuler size={12} />
                    </button>
                    <button
                      onClick={() => removeComponent(c.id)}
                      title="Remove from assembly"
                      className="opacity-0 group-hover:opacity-100 text-cad-text hover:text-cad-danger transition ml-1"
                    >
                      <Trash2 size={12} />
                    </button>
                  </div>
                )
              })
            )}
          </div>
          {assembly.components.some((c) => c.missing) && (
            <div className="border-t border-cad-border p-3 text-[10px] text-cad-danger flex items-start gap-1.5">
              <AlertTriangle size={12} className="mt-0.5 shrink-0" /> A referenced part was deleted. Remove the missing component or recreate the part.
            </div>
          )}
        </aside>
        )}
        <PanelRail side="left" label="Components" collapsed={listHidden} onToggle={toggleList} />

        {/* viewport */}
        <div className="relative flex flex-1 min-w-0">
          <AssemblyViewport
            assemblyId={assembly.id}
            components={assembly.components}
            bbox={assembly.bbox}
            selectedId={selectedComponentId}
            isolate={isolate}
            driven={driven}
            onSelect={select}
            onMove={setTransform}
          />
          {stale && (
            <div role="status" className="absolute top-3 left-3 max-w-[70%] flex items-center gap-3 rounded-sm border border-cad-warning/40 bg-cad-panel/95 px-3 py-2 text-[11px] text-cad-warning">
              <span>{refreshing ? 'Refreshing assembly…' : 'Parts may be out of date.'}</span>
              <button type="button" disabled={refreshing} onClick={() => void refresh()}
                className="underline disabled:opacity-50">Retry</button>
            </div>
          )}
          {/* The inspector is where a failure is reported, so collapsing it
              would silently swallow one. Shown over the viewport instead —
              the editor solves the same problem with `FailureNotice`, for the
              same reason: a failure is news wherever it came from. */}
          {inspectorHidden && error && (
            <p className="absolute bottom-3 right-3 max-w-[min(420px,60%)] text-[11px]
                          text-cad-danger bg-cad-panel/90 backdrop-blur-sm border
                          border-cad-danger/40 rounded-sm px-2.5 py-1.5">
              {error}
            </p>
          )}
        </div>

        <PanelRail side="right" label="Inspector" collapsed={inspectorHidden}
                   onToggle={toggleInspector} />
        {/* inspector: placement, mates, fit and the parts list */}
        {!inspectorHidden && (
        <aside className="w-80 border-l border-cad-border bg-cad-panel flex flex-col shrink-0 z-10">
          <div className="h-11 border-b border-cad-border flex items-center justify-between px-4">
            <div className="leading-none min-w-0">
              <span className="eyebrow text-cad-text/70">Assembly</span>
              <h2 className="font-display text-sm font-semibold text-cad-text-heading leading-none mt-1 truncate max-w-[180px]">
                {TAB_TITLE[tab]}
              </h2>
            </div>
            {tab === 'place' && selected && (
              <button
                onClick={() => setIsolate((v) => !v)}
                title="Isolate the selected part"
                className={`flex items-center gap-1 text-[10px] px-2 py-1 rounded-sm border transition-colors ${
                  isolate ? 'border-cad-accent/50 text-cad-accent-hot bg-cad-accent/10' : 'border-cad-border text-cad-text hover:text-cad-text-heading'
                }`}
              >
                <ScanEye size={11} /> Isolate
              </button>
            )}
          </div>
          <div className="flex border-b border-cad-border">
            {TABS.map((t) => (
              <button
                key={t.id}
                onClick={() => setTab(t.id)}
                className={`flex-1 py-1.5 text-[10px] font-medium border-b-2 transition-colors ${
                  tab === t.id
                    ? 'border-cad-accent text-cad-accent-hot'
                    : 'border-transparent text-cad-text hover:text-cad-text-heading'
                }`}
              >
                {t.label}
              </button>
            ))}
          </div>
          <div className="flex-1 overflow-y-auto p-4">
            {tab === 'place' && (selected ? (
              selected.missing ? (
                <p className="text-[11px] text-cad-danger">This component's part was deleted. Remove it from the assembly.</p>
              ) : (
                <PlacementInspector
                  component={selected}
                  driven={driven.has(selected.id)}
                  onChange={(t) => setTransform(selected.id, t)}
                />
              )
            ) : (
              <p className="text-[11px] text-cad-text/60">Select a component to position it, or insert a part to begin.</p>
            ))}
            {tab === 'parts' && <StandardPartsPicker />}
            {tab === 'mates' && <MatesPanel components={assembly.components} />}
            {tab === 'fit' && <ClashPanel />}
            {tab === 'bom' && <BomPanel />}
            {error && <p className="mt-4 text-[11px] text-cad-danger">{error}</p>}
          </div>
        </aside>
        )}
      </div>

      {picking && (
        <PartPicker
          onClose={() => setPicking(false)}
          onPick={async (id) => { setPicking(false); await insertPart(id) }}
          onCreate={createPart}
          creating={creatingPart}
        />
      )}
      <AccountDrawer open={accountOpen} onClose={() => setAccountOpen(false)} />
      <CopilotPanel open={copilotOpen} onClose={() => setCopilotOpen(false)} subject="assembly" />
    </div>
  )
}

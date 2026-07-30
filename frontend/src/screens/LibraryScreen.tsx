import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Package, Boxes, Plus, Trash2, Loader2, Search, UserCircle2, Users, Upload, Sparkles,
} from 'lucide-react'
import { api } from '../api/client'
import { useDocumentStore } from '../store/useDocumentStore'
import { useAssemblyStore } from '../store/useAssemblyStore'
import { useAuthStore } from '../store/useAuthStore'
import { useNavStore } from '../store/useNavStore'
import { AccountDrawer } from '../components/AccountDrawer'
import type { DocumentSummary, AssemblySummary } from '../../../shared/types'

function relTime(iso: string): string {
  const s = (Date.now() - new Date(iso).getTime()) / 1000
  if (s < 60) return 'just now'
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}

function TrussMark() {
  return (
    <svg width="26" height="26" viewBox="0 0 24 24" fill="none" aria-hidden>
      <path d="M4 19 L12 6 L20 19 M4 19 L20 19 M12 6 L12 19" stroke="var(--color-stress-void)" strokeWidth="1.25" strokeLinejoin="round" />
      <circle cx="4" cy="19" r="1.8" fill="var(--color-cad-text)" />
      <circle cx="20" cy="19" r="1.8" fill="var(--color-cad-text)" />
      <circle cx="12" cy="6" r="2.4" fill="var(--color-cad-accent)" />
    </svg>
  )
}

type Tab = 'parts' | 'assemblies'

export function LibraryScreen() {
  const { openDocument, newBlank } = useDocumentStore()
  const { open: openAssembly, createAndOpen } = useAssemblyStore()
  const account = useAuthStore((s) => s.account)
  const go = useNavStore((s) => s.go)

  const [tab, setTab] = useState<Tab>('parts')
  const [parts, setParts] = useState<DocumentSummary[]>([])
  const [assemblies, setAssemblies] = useState<AssemblySummary[]>([])
  const [loading, setLoading] = useState(true)
  const [query, setQuery] = useState('')
  const [accountOpen, setAccountOpen] = useState(false)
  const [creating, setCreating] = useState(false)
  const [importing, setImporting] = useState(false)
  const [importError, setImportError] = useState<string | null>(null)
  const [genPrompt, setGenPrompt] = useState('')
  const [generating, setGenerating] = useState(false)
  const [genMsg, setGenMsg] = useState<string | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  const reload = () => {
    setLoading(true)
    Promise.all([
      api.listDocuments().catch(() => [] as DocumentSummary[]),
      api.listAssemblies().catch(() => [] as AssemblySummary[]),
    ])
      .then(([p, a]) => { setParts(p); setAssemblies(a) })
      .finally(() => setLoading(false))
  }
  useEffect(reload, [])

  const openPart = async (id: string) => { await openDocument(id); go('editor') }
  const newPart = async () => { setCreating(true); try { await newBlank(); go('editor') } finally { setCreating(false) } }
  const openAsm = async (id: string) => { await openAssembly(id); go('assembly') }
  const newAssembly = async () => {
    setCreating(true)
    try { const id = await createAndOpen(); if (id) go('assembly') } finally { setCreating(false) }
  }
  const generateAssembly = async () => {
    const p = genPrompt.trim()
    if (!p || generating) return
    setGenerating(true)
    setGenMsg('planning the assembly')
    try {
      const id = await api.assemblyFromPrompt(p, setGenMsg)
      setGenPrompt('')
      setGenMsg(null)
      await openAsm(id)
    } catch (e) {
      setGenMsg(e instanceof Error ? e.message : 'Assembly generation failed')
      window.setTimeout(() => setGenMsg(null), 8000)
    } finally {
      setGenerating(false)
    }
  }
  const importFile = async (file: File) => {
    setImporting(true)
    setImportError(null)
    try {
      const doc = await api.importDocument(file)
      await openDocument(doc.id)
      go('editor')
    } catch (e) {
      setImportError(e instanceof Error ? e.message : 'Import failed')
    } finally {
      setImporting(false)
      if (fileRef.current) fileRef.current.value = ''
    }
  }

  const removePart = async (id: string, e: React.MouseEvent) => {
    e.stopPropagation()
    setParts((p) => p.filter((i) => i.id !== id))
    await api.deleteDocument(id).catch(() => {})
  }
  const removeAsm = async (id: string, e: React.MouseEvent) => {
    e.stopPropagation()
    setAssemblies((a) => a.filter((i) => i.id !== id))
    await api.deleteAssembly(id).catch(() => {})
  }

  const q = query.trim().toLowerCase()
  const shownParts = useMemo(() => parts.filter((p) => !q || p.name.toLowerCase().includes(q)), [parts, q])
  const shownAsm = useMemo(() => assemblies.filter((a) => !q || a.name.toLowerCase().includes(q)), [assemblies, q])

  return (
    <div className="h-full flex flex-col" style={{ background: 'radial-gradient(130% 120% at 50% 0%, #171b22 0%, var(--color-cad-bg) 45%)' }}>
      {/* header */}
      <header className="h-14 border-b border-cad-border bg-cad-panel/80 backdrop-blur-sm flex items-center gap-3 px-4 shrink-0 z-20">
        <TrussMark />
        <div className="leading-none">
          <h1 className="font-display text-cad-text-heading font-semibold text-[15px] tracking-tight leading-none">Forge</h1>
          <p className="eyebrow text-cad-text mt-1 leading-none">Library</p>
        </div>
        <div className="flex-1" />
        <div className="flex items-center gap-2 h-9 px-2.5 rounded-sm bg-cad-bg border border-cad-border focus-within:border-cad-accent transition-colors w-64">
          <Search size={13} className="text-cad-text/60" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search parts and assemblies"
            className="flex-1 bg-transparent text-xs text-cad-text-heading placeholder:text-cad-text/50 outline-none"
          />
        </div>
        <button
          onClick={() => setAccountOpen(true)}
          title={account ? `${account.email} · ${account.plan.name} plan` : 'Account'}
          className="flex items-center gap-1.5 h-9 px-2.5 rounded-sm border border-cad-border text-cad-text hover:text-cad-text-heading hover:border-stress-void transition-colors"
        >
          <UserCircle2 size={15} /> <span className="text-[11px] font-medium max-w-[110px] truncate">{account ? account.email : 'Account'}</span>
        </button>
      </header>

      {/* tabs + actions */}
      <div className="border-b border-cad-border bg-cad-panel/40 px-4 flex items-center gap-1 shrink-0">
        {([['parts', 'Parts', parts.length], ['assemblies', 'Assemblies', assemblies.length]] as const).map(([id, label, count]) => (
          <button
            key={id}
            onClick={() => setTab(id)}
            className={`flex items-center gap-2 px-3 py-3 text-xs font-medium border-b-2 -mb-px transition-colors ${
              tab === id ? 'border-cad-accent text-cad-text-heading' : 'border-transparent text-cad-text hover:text-cad-text-heading'
            }`}
          >
            {id === 'parts' ? <Package size={14} /> : <Boxes size={14} />}
            {label}
            <span className="tabular text-[10px] text-cad-text/50">{count}</span>
          </button>
        ))}
        <div className="flex-1" />
        {importError && <span className="text-[10px] text-cad-danger mr-3 max-w-xs truncate" title={importError}>{importError}</span>}
        {tab === 'parts' && (
          <>
            <input
              ref={fileRef}
              type="file"
              accept=".stl,.obj,.ply,.glb,.off,.step,.stp,.iges,.igs"
              className="hidden"
              onChange={(e) => e.target.files?.[0] && importFile(e.target.files[0])}
            />
            <button
              onClick={() => fileRef.current?.click()}
              disabled={importing}
              title="Import STEP / IGES as an exact editable solid, or STL / OBJ / PLY / GLB as a voxel body"
              className="flex items-center gap-1.5 px-3 py-1.5 mr-2 rounded-sm border border-cad-border text-cad-text hover:text-cad-text-heading hover:border-cad-accent/50 text-[11px] font-medium transition-colors disabled:opacity-40"
            >
              {importing ? <Loader2 size={12} className="animate-spin" /> : <Upload size={13} />}
              {importing ? 'Importing…' : 'Import'}
            </button>
          </>
        )}
        {tab === 'assemblies' && (
          <div className="flex items-center gap-2 mr-2">
            {genMsg && (
              <span className="text-[10px] text-cad-accent-hot max-w-56 truncate" title={genMsg}>
                {genMsg}
              </span>
            )}
            <div className="flex items-center gap-2 h-8 px-2.5 rounded-sm bg-cad-bg border border-cad-border focus-within:border-cad-accent transition-colors w-80">
              <Sparkles size={12} className="text-cad-accent shrink-0" />
              <input
                value={genPrompt}
                onChange={(e) => setGenPrompt(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && generateAssembly()}
                disabled={generating}
                placeholder="Describe an assembly — e.g. a hand cart with four wheels"
                className="flex-1 bg-transparent text-xs text-cad-text-heading placeholder:text-cad-text/50 outline-none disabled:opacity-60"
              />
            </div>
            <button
              onClick={generateAssembly}
              disabled={!genPrompt.trim() || generating}
              title="Generate every part and place them as an assembly"
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-sm border border-cad-accent/50 text-cad-accent-hot hover:bg-cad-accent/10 text-[11px] font-semibold transition-colors disabled:opacity-40"
            >
              {generating ? <Loader2 size={12} className="animate-spin" /> : <Sparkles size={12} />}
              {generating ? 'Generating…' : 'Generate'}
            </button>
          </div>
        )}
        <button
          onClick={tab === 'parts' ? newPart : newAssembly}
          disabled={creating}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-sm bg-cad-accent text-cad-bg text-[11px] font-semibold hover:bg-cad-accent-hot transition-colors disabled:opacity-40"
        >
          {creating ? <Loader2 size={12} className="animate-spin" /> : <Plus size={13} />}
          New {tab === 'parts' ? 'part' : 'assembly'}
        </button>
      </div>

      {/* grid */}
      <div className="flex-1 overflow-y-auto p-5">
        {loading ? (
          <div className="flex items-center justify-center h-48 text-cad-text gap-2 text-sm"><Loader2 size={16} className="animate-spin" /> Loading your library…</div>
        ) : tab === 'parts' ? (
          shownParts.length === 0 ? (
            <EmptyState icon={<Package size={34} className="text-cad-border" />} title={q ? 'No parts match your search' : 'No parts yet'} hint={q ? undefined : 'Create a blank part, then describe or model it in the editor.'} action={q ? undefined : { label: 'New part', onClick: newPart }} />
          ) : (
            <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3">
              {shownParts.map((it) => (
                <Card
                  key={it.id}
                  icon={<Package size={18} className="text-cad-text/70 group-hover:text-cad-accent" />}
                  title={it.name}
                  meta={it.provenance.replace('_', '-')}
                  time={relTime(it.updated_at)}
                  shared={it.shared}
                  onOpen={() => openPart(it.id)}
                  onRemove={it.role === 'owner' ? (e) => removePart(it.id, e) : undefined}
                />
              ))}
            </div>
          )
        ) : shownAsm.length === 0 ? (
          <EmptyState icon={<Boxes size={34} className="text-cad-border" />} title={q ? 'No assemblies match your search' : 'No assemblies yet'} hint={q ? undefined : 'Create an assembly, then insert parts to compose it.'} action={q ? undefined : { label: 'New assembly', onClick: newAssembly }} />
        ) : (
          <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3">
            {shownAsm.map((it) => (
              <Card
                key={it.id}
                icon={<Boxes size={18} className="text-cad-text/70 group-hover:text-cad-accent" />}
                title={it.name}
                meta={`${it.component_count} part${it.component_count === 1 ? '' : 's'}`}
                time={relTime(it.updated_at)}
                shared={it.shared}
                onOpen={() => openAsm(it.id)}
                onRemove={it.role === 'owner' ? (e) => removeAsm(it.id, e) : undefined}
              />
            ))}
          </div>
        )}
      </div>

      <AccountDrawer open={accountOpen} onClose={() => setAccountOpen(false)} />
    </div>
  )
}

function Card({
  icon, title, meta, time, shared, onOpen, onRemove,
}: {
  icon: React.ReactNode
  title: string
  meta: string
  time: string
  shared: boolean
  onOpen: () => void
  onRemove?: (e: React.MouseEvent) => void
}) {
  return (
    <button
      onClick={onOpen}
      className="group relative text-left p-3.5 rounded-md border border-cad-border bg-cad-panel/60 hover:border-cad-accent/50 hover:bg-cad-accent/[0.04] transition-colors"
    >
      <div className="flex items-start justify-between">
        {icon}
        {shared && <Users size={12} className="text-cad-text/50" />}
      </div>
      <div className="mt-3 text-[13px] text-cad-text-heading font-medium truncate">{title}</div>
      <div className="mt-1.5 flex items-center justify-between">
        <span className="eyebrow text-cad-text/60">{meta}</span>
        <span className="tabular text-[9px] text-cad-text/50">{time}</span>
      </div>
      {onRemove && (
        <span
          onClick={onRemove}
          title="Delete"
          className="absolute top-2.5 right-2.5 opacity-0 group-hover:opacity-100 p-1 rounded-sm text-cad-text hover:text-cad-danger transition"
        >
          <Trash2 size={13} />
        </span>
      )}
    </button>
  )
}

function EmptyState({
  icon, title, hint, action,
}: {
  icon: React.ReactNode
  title: string
  hint?: string
  action?: { label: string; onClick: () => void }
}) {
  return (
    <div className="flex flex-col items-center justify-center h-64 gap-3 text-center">
      {icon}
      <p className="text-sm text-cad-text-heading">{title}</p>
      {hint && <p className="text-[11px] text-cad-text/60 max-w-xs">{hint}</p>}
      {action && (
        <button onClick={action.onClick} className="mt-1 flex items-center gap-1.5 px-3 py-1.5 rounded-sm bg-cad-accent text-cad-bg text-[11px] font-semibold hover:bg-cad-accent-hot transition-colors">
          <Plus size={13} /> {action.label}
        </button>
      )}
    </div>
  )
}

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
import { Thumbnail } from '../components/Thumbnail'
import type { CatalogMaterial, DocumentSummary, AssemblySummary } from '../../../shared/types'

function relTime(iso: string): string {
  const s = (Date.now() - new Date(iso).getTime()) / 1000
  if (s < 60) return 'just now'
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}

/** Grams below a kilogram: "0.02 kg" is a number nobody pictures. */
function massLabel(kg?: number | null): string | null {
  if (!kg || kg <= 0) return null
  if (kg < 1) return `${Math.round(kg * 1000)} g`
  return `${kg.toFixed(kg < 10 ? 2 : 1)} kg`
}

type Lifecycle = DocumentSummary['lifecycle_state']

/** The state a part is in, said in the card's own colour language: released is
 *  verified, in-review is caution, obsolete is dimmed rather than shouted. */
const LIFECYCLE: Record<Lifecycle, { label: string; className: string }> = {
  draft: { label: 'Draft', className: 'text-cad-text/70 border-cad-border' },
  in_review: { label: 'In review', className: 'text-cad-warning border-cad-warning/40' },
  released: { label: 'Released', className: 'text-cad-success border-cad-success/40' },
  obsolete: { label: 'Obsolete', className: 'text-cad-text/40 border-cad-border/60' },
}

type SortKey = 'edited' | 'created' | 'name'
type OwnerFilter = 'all' | 'mine' | 'shared'
type StateFilter = 'all' | Lifecycle

const SORTS: [SortKey, string][] = [['edited', 'Last edited'], ['created', 'Newest'], ['name', 'Name']]
const OWNERS: [OwnerFilter, string][] = [['all', 'Everything'], ['mine', 'Mine'], ['shared', 'Shared with me']]
const STATES: [StateFilter, string][] = [
  ['all', 'Any state'], ['draft', 'Draft'], ['in_review', 'In review'],
  ['released', 'Released'], ['obsolete', 'Obsolete'],
]

function ownedAs(filter: OwnerFilter, shared: boolean) {
  return filter === 'all' || (filter === 'shared') === shared
}

function bySort(key: SortKey) {
  return (a: { name: string; updated_at: string; created_at: string },
          b: { name: string; updated_at: string; created_at: string }) => {
    if (key === 'name') return a.name.localeCompare(b.name)
    const field = key === 'created' ? 'created_at' : 'updated_at'
    return (b[field] || '').localeCompare(a[field] || '')
  }
}

function Select<T extends string>({
  value, onChange, options, title,
}: { value: T; onChange: (v: T) => void; options: [T, string][]; title: string }) {
  return (
    <select
      value={value}
      title={title}
      onChange={(e) => onChange(e.target.value as T)}
      className="h-8 px-2 rounded-sm bg-cad-bg border border-cad-border text-[11px] text-cad-text hover:text-cad-text-heading focus:border-cad-accent outline-none transition-colors"
    >
      {options.map(([id, label]) => <option key={id} value={id}>{label}</option>)}
    </select>
  )
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
  const [sort, setSort] = useState<SortKey>('edited')
  const [owner, setOwner] = useState<OwnerFilter>('all')
  const [state, setState] = useState<StateFilter>('all')
  const [materials, setMaterials] = useState<Record<string, string>>({})
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
  // The catalog is small and static, and it is what turns `ss-17-4ph` on a card
  // into "Stainless 17-4 PH". Fetched beside the listing rather than before it,
  // so a slow or failed catalog never delays the library.
  useEffect(() => {
    api.listMaterials()
      .then(({ materials: list }: { materials: CatalogMaterial[] }) =>
        setMaterials(Object.fromEntries(list.map((m) => [m.id, m.name]))))
      .catch(() => {})
  }, [])

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
  // The name is often the one thing nobody remembers about a generated part —
  // `source_prompt` is what they actually asked for, and the material is how
  // people describe a part they made twice in two alloys.
  const shownParts = useMemo(() => parts.filter((p) => {
    if (!ownedAs(owner, p.shared)) return false
    if (state !== 'all' && p.lifecycle_state !== state) return false
    if (!q) return true
    const haystack = [p.name, p.source_prompt || '', materials[p.material_id || ''] || p.material_id || '']
    return haystack.some((s) => s.toLowerCase().includes(q))
  }).sort(bySort(sort)), [parts, q, sort, owner, state, materials])
  const shownAsm = useMemo(() => assemblies.filter(
    (a) => ownedAs(owner, a.shared) && (!q || a.name.toLowerCase().includes(q)),
  ).sort(bySort(sort)), [assemblies, q, sort, owner])
  const narrowed = !!q || owner !== 'all' || (tab === 'parts' && state !== 'all')

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
            placeholder="Search name, prompt or material"
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

      {/* sort + filter */}
      <div className="border-b border-cad-border bg-cad-panel/20 px-5 py-2 flex items-center gap-2 shrink-0 flex-wrap">
        <Select value={sort} onChange={setSort} options={SORTS} title="Sort by" />
        <Select value={owner} onChange={setOwner} options={OWNERS} title="Ownership" />
        {tab === 'parts' && <Select value={state} onChange={setState} options={STATES} title="Lifecycle state" />}
        <div className="flex-1" />
        <span className="tabular text-[10px] text-cad-text/50">
          {tab === 'parts' ? shownParts.length : shownAsm.length} shown
        </span>
      </div>

      {/* grid */}
      <div className="flex-1 overflow-y-auto p-5">
        {loading ? (
          <div className="flex items-center justify-center h-48 text-cad-text gap-2 text-sm"><Loader2 size={16} className="animate-spin" /> Loading your library…</div>
        ) : tab === 'parts' ? (
          shownParts.length === 0 ? (
            <EmptyState icon={<Package size={34} className="text-cad-border" />} title={narrowed ? 'No parts match those filters' : 'No parts yet'} hint={narrowed ? undefined : 'Create a blank part, then describe or model it in the editor.'} action={narrowed ? undefined : { label: 'New part', onClick: newPart }} />
          ) : (
            <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3">
              {shownParts.map((it) => (
                <Card
                  key={it.id}
                  icon={<Package size={18} className="text-cad-text/70 group-hover:text-cad-accent" />}
                  preview={
                    <Thumbnail
                      documentId={it.id}
                      meshHash={it.mesh_hash}
                      fallback={<Package size={26} className="text-cad-border" />}
                    />
                  }
                  title={it.name}
                  badge={<StateBadge state={it.lifecycle_state} revision={it.revision} />}
                  meta={it.provenance.replace('_', '-')}
                  detail={[materials[it.material_id || ''] || it.material_id, massLabel(it.mass_kg)]
                    .filter(Boolean).join(' · ')}
                  time={relTime(it.updated_at)}
                  shared={it.shared}
                  onOpen={() => openPart(it.id)}
                  onRemove={it.role === 'owner' ? (e) => removePart(it.id, e) : undefined}
                />
              ))}
            </div>
          )
        ) : shownAsm.length === 0 ? (
          <EmptyState icon={<Boxes size={34} className="text-cad-border" />} title={narrowed ? 'No assemblies match those filters' : 'No assemblies yet'} hint={narrowed ? undefined : 'Create an assembly, then insert parts to compose it.'} action={narrowed ? undefined : { label: 'New assembly', onClick: newAssembly }} />
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

/** Lifecycle plus the released revision letter — one chip, because a released
 *  part with no letter and a draft at revision B are both real states. */
function StateBadge({ state, revision }: { state: Lifecycle; revision?: string | null }) {
  const { label, className } = LIFECYCLE[state] || LIFECYCLE.draft
  return (
    <span className={`px-1.5 py-px rounded-sm border text-[9px] font-medium tracking-wide uppercase whitespace-nowrap ${className}`}>
      {label}{revision ? ` · Rev ${revision}` : ''}
    </span>
  )
}

function Card({
  icon, preview, title, badge, meta, detail, time, shared, onOpen, onRemove,
}: {
  icon: React.ReactNode
  preview?: React.ReactNode
  title: string
  badge?: React.ReactNode
  meta: string
  detail?: string
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
      {preview}
      <div className={`flex items-start justify-between gap-2 ${preview ? 'mt-3' : ''}`}>
        {preview ? <span className="min-w-0 flex-1 text-[13px] text-cad-text-heading font-medium truncate">{title}</span> : icon}
        <span className="flex items-center gap-1.5 shrink-0">
          {badge}
          {shared && <Users size={12} className="text-cad-text/50" />}
        </span>
      </div>
      {!preview && <div className="mt-3 text-[13px] text-cad-text-heading font-medium truncate">{title}</div>}
      {detail && <div className="mt-1 text-[10px] text-cad-text/70 truncate" title={detail}>{detail}</div>}
      <div className="mt-1.5 flex items-center justify-between gap-2">
        <span className="eyebrow text-cad-text/60 truncate">{meta}</span>
        <span className="tabular text-[9px] text-cad-text/50 shrink-0">{time}</span>
      </div>
      {onRemove && (
        <span
          onClick={onRemove}
          title="Delete"
          className="absolute top-2.5 right-2.5 opacity-0 group-hover:opacity-100 p-1 rounded-sm bg-cad-panel/80 text-cad-text hover:text-cad-danger transition"
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

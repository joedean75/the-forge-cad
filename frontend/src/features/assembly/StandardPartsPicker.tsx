import { useEffect, useRef, useState } from 'react'
import { Loader2, Plus, Search, ExternalLink, Upload, Trash2, X, SlidersHorizontal } from 'lucide-react'
import { api } from '../../api/client'
import { useAssemblyStore } from '../../store/useAssemblyStore'
import type { PartSource, StandardPart } from '../../../../shared/types'

/**
 * The purchased parts, from the assembly that needs them.
 *
 * One list, two halves. The **generated** parts are built from the published
 * dimensional standards — always there, no account anywhere, and an ISO 4762
 * M4×16 is an ISO 4762 M4×16 whoever makes it. The **imported** ones are
 * supplier CAD files this account brought in, which is the only lawful way to
 * get *this* connector or *that* cylinder: 3Dfindit disallows automated access
 * and TraceParts' API terms forbid copying its data into another catalog, so
 * the portal links here run a search and the user fetches the file themselves.
 *
 * Three things this shows on purpose:
 *
 * - **Fidelity.** A generated screw has no thread helix. That is the right body
 *   for clearance and interference and the wrong one for a thread study, and
 *   someone about to place a hundred should be told which before, not after.
 * - **Geometry.** `exact` means faces and edges can be selected, measured and
 *   filleted; `mesh` means it arrived as triangles and cannot. Both place fine
 *   and they are not the same part.
 * - **A search link, not a part number.** For a generated part nothing was
 *   verified against a supplier's catalogue, and a part number is what an order
 *   is placed against. An imported part shows the number its importer typed,
 *   attributed to them.
 */

const CATEGORIES: { id: string; label: string }[] = [
  { id: '', label: 'All' },
  { id: 'fastener', label: 'Fasteners' },
  { id: 'bearing', label: 'Bearings' },
  { id: 'motor', label: 'Motors' },
  { id: 'electronics', label: 'Electronics' },
  { id: 'battery', label: 'Batteries' },
  { id: 'propeller', label: 'Props' },
  { id: 'stock', label: 'Stock' },
  { id: 'extrusion', label: 'Extrusion' },
  { id: 'hardware', label: 'Hardware' },
]

const IMPORT_CATEGORIES = ['fastener', 'bearing', 'motor', 'extrusion',
                           'electronics', 'battery', 'propeller', 'stock',
                           'pneumatic', 'hardware', 'other']

export function StandardPartsPicker() {
  const { busy, insertStandardPart } = useAssemblyStore()
  const [parts, setParts] = useState<StandardPart[]>([])
  const [sources, setSources] = useState<PartSource[]>([])
  const [total, setTotal] = useState(0)
  const [category, setCategory] = useState('')
  const [search, setSearch] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [quantity, setQuantity] = useState(1)
  const [importing, setImporting] = useState(false)
  const [reload, setReload] = useState(0)
  // What the user has typed into a parameterised row, by key. Empty until they
  // open one: a row placed with nothing typed goes in at its listed defaults,
  // which is the configuration the row's `size_mm` and mass describe.
  const [tuned, setTuned] = useState<Record<string, Record<string, number>>>({})
  const [open, setOpen] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    api.listStandardParts({ category: category || undefined,
                            search: search || undefined, limit: 60 })
      .then((page) => {
        if (cancelled) return
        setParts(page.parts)
        setTotal(page.count)
        setSources(page.sources ?? [])
        setError(null)
      })
      .catch((e) => !cancelled && setError(e instanceof Error ? e.message : 'Unavailable'))
      .finally(() => !cancelled && setLoading(false))
    return () => { cancelled = true }
  }, [category, search, reload])

  const forget = async (key: string) => {
    try {
      await api.forgetSupplierPart(key)
      setReload((n) => n + 1)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not remove that part')
    }
  }

  return (
    <div className="space-y-2.5">
      <div className="flex items-center gap-2 h-8 px-2 rounded-sm bg-cad-bg border border-cad-border focus-within:border-cad-accent transition-colors">
        <Search size={12} className="text-cad-text/60 shrink-0" />
        <input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="M4, 608, NEMA 17, 2020…"
          className="flex-1 min-w-0 bg-transparent text-[11px] text-cad-text-heading placeholder:text-cad-text/50 outline-none"
        />
      </div>

      <div className="flex gap-px rounded-sm overflow-hidden border border-cad-border">
        {CATEGORIES.map((c) => (
          <button
            key={c.id}
            onClick={() => setCategory(c.id)}
            className={`flex-1 py-1 text-[10px] transition-colors ${
              category === c.id ? 'bg-cad-accent text-cad-bg'
                : 'bg-cad-bg text-cad-text hover:text-cad-text-heading'}`}
          >
            {c.label}
          </button>
        ))}
      </div>

      <label className="flex items-center gap-2 text-[10px] text-cad-text">
        Quantity
        <input
          type="number" min={1} max={64} value={quantity}
          onChange={(e) => setQuantity(Math.max(1, Math.min(64, Number(e.target.value) || 1)))}
          className="tabular w-14 px-1.5 py-0.5 rounded-sm bg-cad-bg border border-cad-border text-cad-text-heading outline-none focus:border-cad-accent"
        />
        <span className="text-cad-text/50">
          {loading ? 'searching…' : `${parts.length} of ${total}`}
        </span>
        <button
          onClick={() => setImporting((v) => !v)}
          className={`ml-auto flex items-center gap-1 px-1.5 py-0.5 rounded-sm border text-[10px] transition-colors ${
            importing ? 'border-cad-accent/60 text-cad-accent-hot'
              : 'border-cad-border text-cad-text hover:text-cad-text-heading'}`}
        >
          <Upload size={10} /> Import
        </button>
      </label>

      {importing && (
        <SupplierImport
          sources={sources}
          query={search}
          onDone={() => { setImporting(false); setReload((n) => n + 1) }}
          onError={setError}
        />
      )}

      {error && <p className="text-[10px] text-cad-danger">{error}</p>}

      <div className="space-y-1 max-h-72 overflow-y-auto">
        {parts.map((part) => (
          <div key={part.key} className="group rounded-sm border border-cad-border bg-cad-bg/50 px-2 py-1.5">
            <div className="flex items-center gap-2">
              <span className="flex-1 min-w-0 text-[11px] text-cad-text-heading truncate"
                    title={part.notes}>
                {part.designation}
              </span>
              {part.supplier_search && (
                <a
                  href={part.supplier_search} target="_blank" rel="noreferrer"
                  title={part.fidelity === 'supplier'
                    ? 'Where this file came from'
                    : 'Search a supplier for this designation'}
                  className="opacity-0 group-hover:opacity-100 text-cad-text hover:text-cad-accent-hot transition"
                >
                  <ExternalLink size={11} />
                </a>
              )}
              {/* Only an imported part can be forgotten; a generated one is not
                  stored anywhere to remove. */}
              {part.fidelity === 'supplier' && (
                <button
                  onClick={() => forget(part.key)}
                  title="Take out of the catalog (the part document is kept)"
                  className="opacity-0 group-hover:opacity-100 text-cad-text hover:text-cad-danger transition"
                >
                  <Trash2 size={11} />
                </button>
              )}
              {/* A row whose dimensions vary by manufacturer says so by
                  carrying `params`, and this is where they get typed. Without
                  it the picker would place a default LiPo — a confident,
                  specific, wrong 165 g — which is the exact failure the
                  parameters exist to prevent. */}
              {part.params && (
                <button
                  onClick={() => setOpen(open === part.key ? null : part.key)}
                  title="Set the dimensions this part does not have a standard for"
                  className={`px-1 py-0.5 rounded-sm border text-[10px] transition ${
                    open === part.key
                      ? 'border-cad-accent/50 text-cad-accent-hot'
                      : 'border-cad-border text-cad-text hover:text-cad-accent-hot'}`}
                >
                  <SlidersHorizontal size={10} />
                </button>
              )}
              <button
                onClick={() => insertStandardPart(part.key, quantity, tuned[part.key])}
                disabled={busy}
                title={`Place ${quantity} × ${part.designation}`}
                className="flex items-center gap-1 px-1.5 py-0.5 rounded-sm border border-cad-accent/50 text-cad-accent-hot hover:bg-cad-accent/10 text-[10px] font-medium transition disabled:opacity-40"
              >
                {busy ? <Loader2 size={10} className="animate-spin" /> : <Plus size={10} />}
                Place
              </button>
            </div>
            <div className="tabular text-[9px] text-cad-text/50 flex flex-wrap gap-x-2">
              <span>{part.size_mm.x}×{part.size_mm.y}×{part.size_mm.z} mm</span>
              <span>{part.standard}</span>
              {part.typical_mass_g != null && <span>~{part.typical_mass_g} g</span>}
              {/* Said before you place a hundred, not after. */}
              {part.fidelity === 'envelope' && (
                <span className="text-cad-warning/80">envelope</span>
              )}
              {part.fidelity === 'supplier' && (
                <span className="text-cad-accent/80">{part.source ?? 'imported'}</span>
              )}
              {part.geometry === 'mesh' && (
                <span className="text-cad-warning/80" title="Triangles, not a solid — no faces or edges to select">
                  mesh
                </span>
              )}
              {part.params && (
                <span className="text-cad-accent/70" title="Some dimensions vary by manufacturer and are set here, not tabled">
                  parameterised
                </span>
              )}
            </div>
            {open === part.key && part.params && (
              <div className="mt-1.5 grid grid-cols-2 gap-1 border-t border-cad-border pt-1.5">
                {Object.entries(part.params).map(([name, spec]) => (
                  <label key={name} className="flex items-center gap-1 text-[9px] text-cad-text/70"
                         title={spec.description}>
                    <span className="flex-1 truncate">{name}</span>
                    <input
                      type="number" step="any"
                      min={spec.min ?? undefined} max={spec.max ?? undefined}
                      value={tuned[part.key]?.[name] ?? spec.default}
                      onChange={(e) => {
                        const value = Number(e.target.value)
                        setTuned((prev) => ({
                          ...prev,
                          [part.key]: { ...(prev[part.key] ?? {}), [name]: value },
                        }))
                      }}
                      className="tabular w-14 bg-cad-bg border border-cad-border rounded-sm px-1 py-0.5 text-[9px] text-cad-text-heading"
                    />
                  </label>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>

      <p className="text-[9px] text-cad-text/40 leading-snug">
        Generated from the published dimensional standards (ISO&nbsp;4762/4032/7089/2338,
        ISO&nbsp;15, NEMA frames) — not downloaded from a supplier. Import brings in a
        file you fetched yourself; nothing here calls a portal on your behalf.
      </p>
    </div>
  )
}

/**
 * Bring in a supplier's file.
 *
 * The portal links come first and are seeded with whatever is in the search
 * box, because the order of operations is: go and find it, download it under
 * your own account, then come back with the file. Anything that looked like
 * "fetch it for me" would be promising something no portal here permits.
 */
function SupplierImport({ sources, query, onDone, onError }: {
  sources: PartSource[]
  query: string
  onDone: () => void
  onError: (message: string | null) => void
}) {
  const fileInput = useRef<HTMLInputElement>(null)
  const [file, setFile] = useState<File | null>(null)
  const [designation, setDesignation] = useState('')
  const [manufacturer, setManufacturer] = useState('')
  const [partNumber, setPartNumber] = useState('')
  const [source, setSource] = useState('manufacturer')
  const [category, setCategory] = useState('hardware')
  const [sourceUrl, setSourceUrl] = useState('')
  const [running, setRunning] = useState(false)

  const submit = async () => {
    if (!file || running) return
    setRunning(true)
    onError(null)
    try {
      await api.importSupplierPart(file, {
        designation: designation || file.name.replace(/\.[^.]+$/, ''),
        manufacturer, part_number: partNumber, source, category,
        source_url: sourceUrl,
      })
      onDone()
    } catch (e) {
      onError(e instanceof Error ? e.message : 'Could not import that file')
    } finally {
      setRunning(false)
    }
  }

  const field = 'w-full px-1.5 py-1 rounded-sm bg-cad-bg border border-cad-border text-[10px] text-cad-text-heading placeholder:text-cad-text/40 outline-none focus:border-cad-accent'

  return (
    <div className="rounded-sm border border-cad-accent/40 bg-cad-bg/40 p-2 space-y-2">
      <div className="flex items-start gap-2">
        <p className="flex-1 text-[9.5px] text-cad-text/70 leading-snug">
          Download the STEP from the maker or a portal under your own account, then
          bring the file here. STEP and IGES stay exact; a mesh comes in as triangles.
        </p>
        <button onClick={onDone} className="text-cad-text/60 hover:text-cad-text-heading">
          <X size={12} />
        </button>
      </div>

      <div className="flex flex-wrap gap-1">
        {sources.filter((s) => s.search).map((s) => (
          <a
            key={s.id}
            href={s.search.replace('{q}', encodeURIComponent(query || 'bearing'))}
            target="_blank" rel="noreferrer"
            title={s.note}
            className="flex items-center gap-1 px-1.5 py-0.5 rounded-sm border border-cad-border text-[9.5px] text-cad-text hover:text-cad-accent-hot hover:border-cad-accent/50 transition"
          >
            <ExternalLink size={9} /> {s.label}
          </a>
        ))}
      </div>

      <button
        onClick={() => fileInput.current?.click()}
        className="w-full flex items-center justify-center gap-1.5 py-1.5 rounded-sm border border-dashed border-cad-border text-[10px] text-cad-text hover:text-cad-accent-hot hover:border-cad-accent/50 transition"
      >
        <Upload size={11} /> {file ? file.name : 'Choose a STEP, IGES or mesh file'}
      </button>
      <input
        ref={fileInput} type="file" className="hidden"
        accept=".step,.stp,.iges,.igs,.stl,.obj,.ply,.glb,.off"
        onChange={(e) => { setFile(e.target.files?.[0] ?? null); e.target.value = '' }}
      />

      <div className="grid grid-cols-2 gap-1">
        <input className={field} placeholder="Designation" value={designation}
               onChange={(e) => setDesignation(e.target.value)} />
        <input className={field} placeholder="Manufacturer" value={manufacturer}
               onChange={(e) => setManufacturer(e.target.value)} />
        <input className={field} placeholder="Part number" value={partNumber}
               onChange={(e) => setPartNumber(e.target.value)} />
        <select className={field} value={category}
                onChange={(e) => setCategory(e.target.value)}>
          {IMPORT_CATEGORIES.map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
        <select className={field} value={source}
                onChange={(e) => setSource(e.target.value)}>
          {sources.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}
        </select>
        <input className={field} placeholder="Source URL (optional)" value={sourceUrl}
               onChange={(e) => setSourceUrl(e.target.value)} />
      </div>

      <button
        onClick={submit}
        disabled={!file || running}
        className="w-full flex items-center justify-center gap-1.5 py-1 rounded-sm bg-cad-accent text-cad-bg text-[10px] font-semibold hover:bg-cad-accent-hot transition disabled:opacity-40"
      >
        {running ? <Loader2 size={11} className="animate-spin" /> : <Plus size={11} />}
        Add to my catalog
      </button>
      <p className="text-[9px] text-cad-text/40 leading-snug">
        The part number is recorded as you enter it — nothing here checks it against a
        supplier. Imported parts are yours alone and are never shared.
      </p>
    </div>
  )
}

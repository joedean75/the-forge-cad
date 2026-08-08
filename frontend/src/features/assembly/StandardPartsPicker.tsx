import { useEffect, useState } from 'react'
import { Loader2, Plus, Search, ExternalLink } from 'lucide-react'
import { api } from '../../api/client'
import { useAssemblyStore } from '../../store/useAssemblyStore'
import type { StandardPart } from '../../../../shared/types'

/**
 * The standard purchased parts, from the assembly that needs them.
 *
 * Every real assembly needs screws, and the only way to get one used to be to
 * model it. These are generated from the published dimensional standards, so
 * they arrive as ordinary editable parts.
 *
 * Two things this shows on purpose:
 *
 * - **Fidelity.** A screw here has no thread helix. That is the right body for
 *   clearance and interference and the wrong one for a thread study, and
 *   someone about to place a hundred of them should be told which before, not
 *   after.
 * - **A search link, not a part number.** None of these were verified against a
 *   supplier's catalogue, and a part number is the thing an order is placed
 *   against. The designation is what actually gets ordered and the link runs a
 *   search on it.
 */

const CATEGORIES: { id: string; label: string }[] = [
  { id: '', label: 'All' },
  { id: 'fastener', label: 'Fasteners' },
  { id: 'bearing', label: 'Bearings' },
  { id: 'motor', label: 'Motors' },
  { id: 'extrusion', label: 'Extrusion' },
]

export function StandardPartsPicker() {
  const { busy, insertStandardPart } = useAssemblyStore()
  const [parts, setParts] = useState<StandardPart[]>([])
  const [total, setTotal] = useState(0)
  const [category, setCategory] = useState('')
  const [search, setSearch] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [quantity, setQuantity] = useState(1)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    api.listStandardParts({ category: category || undefined,
                            search: search || undefined, limit: 60 })
      .then((page) => {
        if (cancelled) return
        setParts(page.parts)
        setTotal(page.count)
        setError(null)
      })
      .catch((e) => !cancelled && setError(e instanceof Error ? e.message : 'Unavailable'))
      .finally(() => !cancelled && setLoading(false))
    return () => { cancelled = true }
  }, [category, search])

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
      </label>

      {error && <p className="text-[10px] text-cad-danger">{error}</p>}

      <div className="space-y-1 max-h-72 overflow-y-auto">
        {parts.map((part) => (
          <div key={part.key} className="group rounded-sm border border-cad-border bg-cad-bg/50 px-2 py-1.5">
            <div className="flex items-center gap-2">
              <span className="flex-1 min-w-0 text-[11px] text-cad-text-heading truncate"
                    title={part.notes}>
                {part.designation}
              </span>
              <a
                href={part.supplier_search} target="_blank" rel="noreferrer"
                title="Search a supplier for this designation"
                className="opacity-0 group-hover:opacity-100 text-cad-text hover:text-cad-accent-hot transition"
              >
                <ExternalLink size={11} />
              </a>
              <button
                onClick={() => insertStandardPart(part.key, quantity)}
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
            </div>
          </div>
        ))}
      </div>

      <p className="text-[9px] text-cad-text/40 leading-snug">
        Generated from the published dimensional standards (ISO&nbsp;4762/4032/7089/2338,
        ISO&nbsp;15, NEMA frames) — not downloaded from a supplier. Links run a search
        on the designation rather than claiming a part number.
      </p>
    </div>
  )
}

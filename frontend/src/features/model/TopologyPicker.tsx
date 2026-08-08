import { useEffect, useState } from 'react'
import { AlertTriangle, Check, Loader2, Scan, Split, X } from 'lucide-react'
import { useDocumentStore } from '../../store/useDocumentStore'
import type { EdgeRef, FaceRef, ReferenceRow } from '../../../../shared/types'

/**
 * Selecting the faces and edges an operation acts on.
 *
 * This is the idiom parametric CAD runs on and the one thing the SDF kernel
 * could never offer: `fillet` there is a smooth boolean between two bodies, with
 * no way to name one edge of one of them. The exact kernel gives every face and
 * edge a stable id (`cad_kernel/core/naming`), and the three operations below
 * consume those ids.
 *
 * Two decisions worth stating, because both look like omissions:
 *
 * **The list is the picker, not a 3D overlay.** Picking directly on the body
 * would need per-face meshes, and the display mesh is one welded triangle soup
 * with no face ids in it — clicking it can tell you *where* you clicked, not
 * *which face*. Rather than invent a mapping that would be right most of the
 * time, entities are listed with the measurements that tell them apart (a
 * cylinder's radius, an edge's length, both centres in mm) and the selection is
 * marked in the viewport so the two stay tied together.
 *
 * **A body with no exact form is explained, not hidden.** A smooth-boolean
 * shell, an optimised body or an imported mesh evaluates as a distance field and
 * genuinely has nothing addressable; the operation says so as a `PRECONDITION`
 * and that message is shown as-is.
 */

const EDGE_FILTERS = [
  { id: 'all', label: 'All' },
  { id: 'line', label: 'Straight' },
  { id: 'circle', label: 'Circular' },
] as const

type EdgeFilter = (typeof EDGE_FILTERS)[number]['id']

export function TopologyPicker() {
  const { doc, topo, busy, loadTopology, toggleTopoSelection, setTopoSelection, applyTopology } =
    useDocumentStore()
  const [size, setSize] = useState(2)
  const [filter, setFilter] = useState<EdgeFilter>('all')

  const entities: Array<FaceRef | EdgeRef> = topo.kind === 'face' ? topo.faces : topo.edges
  const listed = topo.kind === 'edge' && filter !== 'all'
    ? (entities as EdgeRef[]).filter((e) => e.kind === filter)
    : entities
  const chosen = topo.selected.length

  if (!doc) return null

  return (
    <div className="pt-3 border-t border-cad-border">
      <div className="flex items-center justify-between mb-2">
        <h3 className="sec-head">
          Faces &amp; edges
          {topo.kind === 'face' && entities.length > 0 && (
            <span className="ml-1.5 normal-case tracking-normal text-[9px] text-cad-accent-hot">
              · click the part
            </span>
          )}
        </h3>
        <div className="flex gap-px rounded-sm overflow-hidden border border-cad-border">
          {(['edge', 'face'] as const).map((k) => (
            <button
              key={k}
              onClick={() => loadTopology(k)}
              className={`px-1.5 py-0.5 text-[10px] uppercase tracking-wide transition-colors ${
                topo.kind === k && entities.length
                  ? 'bg-cad-accent text-cad-bg'
                  : 'bg-cad-bg text-cad-text hover:text-cad-text-heading'
              }`}
            >
              {k === 'edge' ? 'Edges' : 'Faces'}
            </button>
          ))}
        </div>
      </div>

      <button
        onClick={() => loadTopology(topo.kind)}
        disabled={topo.loading}
        className="w-full flex items-center justify-center gap-1.5 py-1.5 rounded-sm border border-cad-border text-cad-text hover:text-cad-text-heading transition text-[11px] disabled:opacity-50"
      >
        {topo.loading ? <Loader2 size={12} className="animate-spin" /> : <Scan size={12} />}
        {entities.length ? `Re-read ${topo.kind}s` : `List the body's ${topo.kind}s`}
      </button>

      {topo.error && (
        <p className="mt-2 text-[10px] leading-snug text-cad-warning">{topo.error}</p>
      )}

      {entities.length > 0 && (
        <>
          {topo.kind === 'edge' && (
            <div className="mt-2 flex gap-px rounded-sm overflow-hidden border border-cad-border">
              {EDGE_FILTERS.map((f) => (
                <button
                  key={f.id}
                  onClick={() => setFilter(f.id)}
                  className={`flex-1 px-1.5 py-0.5 text-[10px] transition-colors ${
                    filter === f.id
                      ? 'bg-cad-accent/15 text-cad-accent-hot'
                      : 'bg-cad-bg text-cad-text hover:text-cad-text-heading'
                  }`}
                >
                  {f.label}
                </button>
              ))}
            </div>
          )}

          <div className="mt-2 flex items-center justify-between text-[10px] text-cad-text/60">
            <span className="tabular">
              {chosen ? `${chosen} selected` : `${listed.length} ${topo.kind}${listed.length === 1 ? '' : 's'}`}
            </span>
            <div className="flex gap-2">
              <button
                onClick={() => setTopoSelection(listed.map((e) => e.id))}
                className="hover:text-cad-accent-hot"
              >
                All
              </button>
              <button onClick={() => setTopoSelection([])} className="hover:text-cad-text-heading">
                None
              </button>
            </div>
          </div>

          <ul className="mt-1 max-h-56 overflow-y-auto rounded-sm border border-cad-border divide-y divide-cad-border/60">
            {listed.map((e) => (
              <EntityRow
                key={e.id}
                entity={e}
                picked={topo.selected.includes(e.id)}
                onToggle={() => toggleTopoSelection(e.id)}
              />
            ))}
          </ul>

          <div className="mt-2 flex items-center gap-2">
            <label className="text-[10px] text-cad-text flex items-center gap-1">
              {topo.kind === 'face' ? 'wall' : 'size'}
              <input
                type="number"
                value={size}
                step={0.5}
                min={0.1}
                onChange={(ev) => setSize(Math.max(0.1, parseFloat(ev.target.value) || 0.1))}
                className="tabular w-14 px-1.5 py-1 rounded-sm bg-cad-bg border border-cad-border text-cad-text-heading text-[11px] outline-none focus:border-cad-accent"
              />
              mm
            </label>
            {/* Named after the operations they call, and deliberately not just
                "Fillet": the tool ribbon's Fillet rounds the whole body, and two
                buttons of the same name on one screen doing different things is
                how a user learns to distrust both. */}
            {topo.kind === 'edge' ? (
              <>
                <Action label="Fillet picked" disabled={!chosen || busy} onClick={() => applyTopology('fillet', size)} />
                <Action label="Chamfer picked" disabled={!chosen || busy} onClick={() => applyTopology('chamfer', size)} />
              </>
            ) : (
              <Action label="Shell open picked" disabled={!chosen || busy} onClick={() => applyTopology('shell', size)} />
            )}
          </div>
          <p className="mt-1.5 text-[10px] leading-snug text-cad-text/50">
            {topo.kind === 'edge'
              ? 'A real edge treatment, not a blend between bodies. A radius the geometry cannot carry is refused and the part is left as it was.'
              : 'Hollows the body to that wall and leaves the selected faces open — the way in.'}
          </p>
        </>
      )}

      <ReferenceHealth />
    </div>
  )
}

function Action({ label, disabled, onClick }: { label: string; disabled: boolean; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className="flex-1 py-1.5 rounded-sm border border-cad-accent/50 text-cad-accent-hot hover:bg-cad-accent/10 text-[11px] font-medium transition disabled:opacity-40 disabled:hover:bg-transparent"
    >
      {label}
    </button>
  )
}

function EntityRow({
  entity,
  picked,
  onToggle,
}: {
  entity: FaceRef | EdgeRef
  picked: boolean
  onToggle: () => void
}) {
  const isFace = 'area_mm2' in entity
  const size = isFace ? `${entity.area_mm2.toFixed(0)} mm²` : `${entity.length_mm.toFixed(1)} mm`
  const at = isFace ? entity.center_mm : entity.midpoint_mm
  return (
    <li>
      <button
        onClick={onToggle}
        className={`w-full flex items-center gap-2 px-2 py-1 text-left transition-colors ${
          picked ? 'bg-cad-accent/15' : 'hover:bg-cad-bg/60'
        }`}
      >
        <span
          className={`flex items-center justify-center h-3 w-3 rounded-[2px] border shrink-0 ${
            picked ? 'border-cad-accent bg-cad-accent text-cad-bg' : 'border-cad-border'
          }`}
        >
          {picked && <Check size={9} strokeWidth={3} />}
        </span>
        <span className={`text-[10px] w-14 shrink-0 ${picked ? 'text-cad-accent-hot' : 'text-cad-text'}`}>
          {entity.kind}
        </span>
        <span className="tabular text-[10px] text-cad-text-heading w-16 shrink-0 text-right">{size}</span>
        {entity.radius_mm != null && (
          <span className="tabular text-[10px] text-cad-text/70 shrink-0">⌀{(entity.radius_mm * 2).toFixed(1)}</span>
        )}
        <span className="tabular text-[9px] text-cad-text/45 flex-1 text-right truncate">
          {at.map((v) => v.toFixed(0)).join(' ')}
        </span>
      </button>
    </li>
  )
}

/**
 * What became of the selections already stored on the part's features.
 *
 * The failure this exists for is invisible everywhere else: an upstream edit
 * destroys an edge a fillet was placed on, the body rebuilds without that
 * rounded corner, and it is a perfectly valid solid that `check_geometry` passes
 * happily. `split` is the benign case — a cut divided an edge and the reference
 * now names both halves — and is shown for the same reason, because "one
 * selection, two fillets" is surprising if you did not ask for it.
 */
function ReferenceHealth() {
  const { doc, refs, checkReferences } = useDocumentStore()
  useEffect(() => { checkReferences() }, [doc?.derived.solid_hash, checkReferences])

  if (!refs || refs.features.length === 0) return null
  const rows = refs.features.flatMap((f) =>
    f.references.map((r) => ({ ...r, feature: f.feature.replace('Feature', ''), op: f.operation })),
  )
  const bad = rows.filter((r) => r.status === 'deleted' || r.status === 'unresolved')
  const split = rows.filter((r) => r.status === 'split')
  if (bad.length === 0 && split.length === 0) {
    return (
      <p className="mt-3 flex items-center gap-1.5 text-[10px] text-cad-text/50">
        <Check size={11} className="text-cad-success" />
        All {rows.length} stored {rows.length === 1 ? 'selection resolves' : 'selections resolve'}.
      </p>
    )
  }

  return (
    <div className="mt-3 space-y-1.5">
      <h4 className="eyebrow text-cad-text/60">Stored selections</h4>
      {[...bad, ...split].map((r, i) => (
        <ReferenceLine key={`${r.id}-${i}`} row={r} feature={r.feature} />
      ))}
      {bad.length > 0 && (
        <p className="text-[10px] leading-snug text-cad-warning">
          The body still builds — it is simply missing {bad.length === 1 ? 'that treatment' : 'those treatments'}.
          Re-list the {'edges'} above and reapply.
        </p>
      )}
    </div>
  )
}

function ReferenceLine({ row, feature }: { row: ReferenceRow; feature: string }) {
  const lost = row.status === 'deleted' || row.status === 'unresolved'
  const Icon = lost ? X : row.status === 'split' ? Split : AlertTriangle
  const tone = lost ? 'text-cad-danger' : 'text-cad-warning'
  return (
    <div className="flex items-start gap-1.5 text-[10px]">
      <Icon size={11} className={`${tone} mt-px shrink-0`} />
      <span className="text-cad-text-heading shrink-0">{feature}</span>
      <span className="text-cad-text/70 flex-1 leading-snug">
        {row.status === 'split' ? `now names ${row.resolves_to} pieces` : row.meaning}
      </span>
    </div>
  )
}

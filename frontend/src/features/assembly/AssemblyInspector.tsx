import { useState } from 'react'
import { AlertTriangle, CheckCircle2, Download, Link2, Loader2, Ruler, Trash2 } from 'lucide-react'
import { useAssemblyStore } from '../../store/useAssemblyStore'
import type { AssemblyComponent, AssemblyMate } from '../../../../shared/types'

/**
 * The three things an assembly is for, and none of them were reachable.
 *
 * `assembly_mates.solve`, `interference.check_assembly` and
 * `bom.bill_of_materials` were all implemented, routed and tested on the
 * server; the screen offered hand-typed coordinates. An assembly you position
 * by nudging numbers is a picture of an assembly.
 */

const MATE_KINDS: { id: AssemblyMate['type']; label: string; hint: string }[] = [
  { id: 'coincident', label: 'Coincident', hint: 'a face of each part meets — stacking' },
  { id: 'concentric', label: 'Concentric', hint: 'the two parts share an axis' },
  { id: 'distance', label: 'Distance', hint: 'a fixed gap along one axis' },
  { id: 'fasten', label: 'Fasten', hint: 'rigidly attached at an offset' },
]

const AXES = ['x', 'y', 'z'] as const
const FACES = ['min', 'center', 'max'] as const

function Select<T extends string>({ value, onChange, options, title }: {
  value: T; onChange: (v: T) => void; options: readonly T[]; title?: string
}) {
  return (
    <select
      title={title}
      value={value}
      onChange={(e) => onChange(e.target.value as T)}
      className="w-full px-1.5 py-1 rounded-sm bg-cad-bg border border-cad-border text-cad-text-heading text-[11px] outline-none focus:border-cad-accent"
    >
      {options.map((o) => <option key={o} value={o}>{o}</option>)}
    </select>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="eyebrow text-cad-text/60 block mb-1">{label}</span>
      {children}
    </label>
  )
}

export function MatesPanel({ components }: { components: AssemblyComponent[] }) {
  const { assembly, busy, addMate, removeMate } = useAssemblyStore()
  const usable = components.filter((c) => !c.missing)
  const [draft, setDraft] = useState<Omit<AssemblyMate, 'id'>>({
    type: 'coincident', base: '', moved: '', axis: 'z',
    base_face: 'max', moved_face: 'min', offset_mm: 0, angle_deg: 0,
  })

  const mates = assembly?.mates ?? []
  const name = (id: string) => usable.find((c) => c.id === id)?.name ?? '(removed)'
  const patch = (p: Partial<Omit<AssemblyMate, 'id'>>) => setDraft((d) => ({ ...d, ...p }))
  const ready = draft.base && draft.moved && draft.base !== draft.moved

  if (usable.length < 2) {
    return (
      <p className="text-[11px] text-cad-text/60">
        A mate is a relationship between two parts. Insert a second part to start
        constraining them instead of positioning by hand.
      </p>
    )
  }

  return (
    <div className="space-y-4">
      <div>
        <span className="eyebrow text-cad-text/60 block mb-1.5">Mates</span>
        {mates.length === 0 ? (
          <p className="text-[11px] text-cad-text/60">
            Nothing constrained yet — every part is placed by hand.
          </p>
        ) : (
          <ul className="space-y-1">
            {mates.map((m) => (
              <li key={m.id} className="group flex items-center gap-1.5 text-[11px] text-cad-text">
                <Link2 size={11} className="text-cad-accent shrink-0" />
                <span className="truncate">
                  <span className="text-cad-text-heading">{name(m.moved)}</span>
                  {' '}{m.type}{' '}
                  <span className="text-cad-text-heading">{name(m.base)}</span>
                  <span className="text-cad-text/50"> · {m.axis}
                    {m.offset_mm ? ` · ${m.offset_mm} mm` : ''}</span>
                </span>
                <button
                  onClick={() => removeMate(m.id)}
                  title="Remove this mate"
                  className="ml-auto opacity-0 group-hover:opacity-100 text-cad-text hover:text-cad-danger transition"
                >
                  <Trash2 size={11} />
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="pt-3 border-t border-cad-border space-y-2.5">
        <span className="eyebrow text-cad-text/60 block">Add a mate</span>
        <Field label="Type">
          <select
            value={draft.type}
            onChange={(e) => patch({ type: e.target.value as AssemblyMate['type'] })}
            className="w-full px-1.5 py-1 rounded-sm bg-cad-bg border border-cad-border text-cad-text-heading text-[11px] outline-none focus:border-cad-accent"
          >
            {MATE_KINDS.map((k) => <option key={k.id} value={k.id}>{k.label}</option>)}
          </select>
        </Field>
        <p className="text-[10px] text-cad-text/50 -mt-1">
          {MATE_KINDS.find((k) => k.id === draft.type)?.hint}
        </p>

        <div className="grid grid-cols-2 gap-2">
          <Field label="Base (held)">
            <select
              value={draft.base}
              onChange={(e) => patch({ base: e.target.value })}
              className="w-full px-1.5 py-1 rounded-sm bg-cad-bg border border-cad-border text-cad-text-heading text-[11px] outline-none focus:border-cad-accent"
            >
              <option value="">—</option>
              {usable.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
            </select>
          </Field>
          <Field label="Moved">
            <select
              value={draft.moved}
              onChange={(e) => patch({ moved: e.target.value })}
              className="w-full px-1.5 py-1 rounded-sm bg-cad-bg border border-cad-border text-cad-text-heading text-[11px] outline-none focus:border-cad-accent"
            >
              <option value="">—</option>
              {usable.filter((c) => c.id !== draft.base).map((c) => (
                <option key={c.id} value={c.id}>{c.name}</option>
              ))}
            </select>
          </Field>
        </div>

        <div className="grid grid-cols-3 gap-2">
          <Field label="Axis"><Select value={draft.axis} onChange={(v) => patch({ axis: v })} options={AXES} /></Field>
          <Field label="Base face">
            <Select value={draft.base_face} onChange={(v) => patch({ base_face: v })} options={FACES} />
          </Field>
          <Field label="Moved face">
            <Select value={draft.moved_face} onChange={(v) => patch({ moved_face: v })} options={FACES} />
          </Field>
        </div>

        <Field label="Offset (mm)">
          <input
            type="number" step={1} value={draft.offset_mm}
            onChange={(e) => patch({ offset_mm: Number(e.target.value) || 0 })}
            className="w-full px-1.5 py-1 rounded-sm bg-cad-bg border border-cad-border text-cad-text-heading text-[11px] tabular outline-none focus:border-cad-accent"
          />
        </Field>

        <button
          onClick={() => { void addMate(draft); }}
          disabled={!ready || busy}
          className="w-full flex items-center justify-center gap-1.5 py-1.5 rounded-sm bg-cad-accent text-cad-bg font-semibold text-[11px] hover:bg-cad-accent-hot transition-colors disabled:opacity-40"
        >
          {busy ? <Loader2 size={12} className="animate-spin" /> : <Link2 size={12} />} Add mate
        </button>

        {/* Said plainly rather than implied. `base_face`/`moved_face` are the six
            bounding-box extremes, not the addressable faces `list_faces` returns —
            right for plates and standoffs, and not yet able to name a counterbore. */}
        <p className="text-[10px] text-cad-text/45 leading-relaxed">
          Faces are the part's bounding-box extremes along the chosen axis, and
          mates keep parts axis-aligned. Selecting a specific face or bore is not
          supported yet.
        </p>
      </div>
    </div>
  )
}

export function ClashPanel() {
  const { clash, busy, checkClash } = useAssemblyStore()
  const [clearance, setClearance] = useState(1)

  return (
    <div className="space-y-3">
      <div className="flex items-end gap-2">
        <Field label="Clearance (mm)">
          <input
            type="number" step={0.5} min={0} value={clearance}
            onChange={(e) => setClearance(Math.max(0, Number(e.target.value) || 0))}
            className="w-20 px-1.5 py-1 rounded-sm bg-cad-bg border border-cad-border text-cad-text-heading text-[11px] tabular outline-none focus:border-cad-accent"
          />
        </Field>
        <button
          onClick={() => { void checkClash(clearance) }}
          disabled={busy}
          className="flex-1 flex items-center justify-center gap-1.5 py-1.5 rounded-sm border border-cad-border text-cad-text-heading text-[11px] hover:border-cad-accent/50 transition-colors disabled:opacity-40"
        >
          {busy ? <Loader2 size={12} className="animate-spin" /> : <Ruler size={12} />} Check fit
        </button>
      </div>

      {!clash ? (
        <p className="text-[11px] text-cad-text/60">
          Nothing checked yet. Run this before exporting — two parts that
          interpenetrate look fine in the viewport.
        </p>
      ) : (
        <>
          <div className={`flex items-start gap-2 p-2.5 rounded-sm border text-[11px] ${
            clash.interferences > 0
              ? 'border-cad-danger/40 bg-cad-danger/[0.06] text-cad-danger'
              : clash.clearance_warnings > 0
                ? 'border-cad-accent/40 bg-cad-accent/[0.06] text-cad-accent-hot'
                : 'border-stress-void/40 text-cad-text'
          }`}>
            {clash.interferences > 0 ? <AlertTriangle size={13} className="mt-px shrink-0" />
              : <CheckCircle2 size={13} className="mt-px shrink-0" />}
            <span>
              {clash.interferences > 0
                ? `${clash.interferences} pair(s) interfere`
                : clash.clearance_warnings > 0
                  ? `Clear, but ${clash.clearance_warnings} pair(s) closer than ${clash.clearance_threshold_mm} mm`
                  : 'No interference or close pairs'}
            </span>
          </div>

          {clash.pairs.length > 0 && (
            <ul className="space-y-1">
              {clash.pairs.map((p, i) => (
                <li key={i} className="flex items-center gap-2 text-[11px]">
                  <span className={p.status === 'interference' ? 'text-cad-danger' : 'text-cad-accent-hot'}>
                    {p.status === 'interference' ? '✕' : '!'}
                  </span>
                  <span className="truncate text-cad-text">{p.a} ↔ {p.b}</span>
                  <span className="tabular text-[10px] text-cad-text/60 ml-auto shrink-0">
                    {p.status === 'interference'
                      ? `${p.penetration_mm} mm deep`
                      : `${p.gap_mm} mm gap`}
                  </span>
                </li>
              ))}
            </ul>
          )}

          {/* What it measured, not just what it concluded. The check samples
              surfaces; "clear" means no sample landed inside, which is not the
              same as proof. */}
          <p className="text-[10px] text-cad-text/45 leading-relaxed">
            Sampled across {clash.components_checked} placed component(s) at a
            {' '}{clash.clearance_threshold_mm} mm threshold. Surfaces are sampled,
            not solved — a very thin overlap can be missed.
          </p>
        </>
      )}
    </div>
  )
}

export function BomPanel() {
  const { bom, busy, loadBom, downloadBomCsv } = useAssemblyStore()

  return (
    <div className="space-y-3">
      <div className="flex gap-2">
        <button
          onClick={() => { void loadBom() }}
          disabled={busy}
          className="flex-1 flex items-center justify-center gap-1.5 py-1.5 rounded-sm border border-cad-border text-cad-text-heading text-[11px] hover:border-cad-accent/50 transition-colors disabled:opacity-40"
        >
          {busy ? <Loader2 size={12} className="animate-spin" /> : <Ruler size={12} />} Parts list
        </button>
        {bom && (
          <button
            onClick={() => { void downloadBomCsv() }}
            title="Download as CSV"
            className="px-2.5 rounded-sm border border-cad-border text-cad-text hover:text-cad-accent-hot hover:border-cad-accent/50 transition-colors"
          >
            <Download size={12} />
          </button>
        )}
      </div>

      {!bom ? (
        <p className="text-[11px] text-cad-text/60">
          Grouped by part, with mass and material cost rolled up — the list a
          purchasing conversation starts from.
        </p>
      ) : (
        <>
          <table className="w-full text-[10px] tabular">
            <thead>
              <tr className="text-cad-text/55 text-left">
                <th className="font-normal pb-1">#</th>
                <th className="font-normal pb-1">Part</th>
                <th className="font-normal pb-1 text-right">Qty</th>
                <th className="font-normal pb-1 text-right">Mass</th>
              </tr>
            </thead>
            <tbody>
              {bom.lines.map((l) => (
                <tr key={l.part_id} className={l.missing ? 'text-cad-danger' : 'text-cad-text'}>
                  <td className="py-0.5 pr-1">{l.item}</td>
                  <td className="py-0.5 pr-1 truncate max-w-[110px]" title={`${l.name} · ${l.material}`}>{l.name}</td>
                  <td className="py-0.5 text-right">{l.quantity}</td>
                  <td className="py-0.5 text-right">{l.ext_mass_g.toFixed(1)} g</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="pt-2 border-t border-cad-border flex items-center justify-between text-[10px]">
            <span className="text-cad-text/60">{bom.line_count} line(s), {bom.component_count} part(s)</span>
            <span className="tabular text-cad-text-heading">
              {bom.total_mass_g.toFixed(1)} g · ${bom.total_material_cost_usd.toFixed(2)}
            </span>
          </div>
          <p className="text-[10px] text-cad-text/45">
            Cost is material only — mass × price per kilo. It is not a quote.
          </p>
        </>
      )}
    </div>
  )
}

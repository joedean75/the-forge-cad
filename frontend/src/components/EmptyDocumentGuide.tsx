import { useEffect, useState, type ReactElement } from 'react'
import { Loader2, X, PencilRuler, Box, Sparkles } from 'lucide-react'
import { api } from '../api/client'
import { useDocumentStore } from '../store/useDocumentStore'
import type { PartTemplate } from '../../../shared/types'

/**
 * What to do in a document that has no body yet.
 *
 * A new document is three datum planes and nothing else — the right change, and
 * it left the one place this app explained itself keyed on `!doc`, so the
 * guidance vanished the instant somebody clicked *New part*, which is the
 * moment they need it.
 *
 * Three properties, each of which was a way the previous empty state was wrong:
 *
 * - **It lives in the document, not instead of it.** The planes stay visible and
 *   clickable behind it; this is a card along the bottom, not a takeover.
 * - **It leaves on its own.** It is keyed on the document having no solid, so
 *   the first feature — from here or from anywhere else — is the last frame it
 *   is drawn in. Nothing has to remember to hide it.
 * - **It can be dismissed for good.** Guidance you cannot turn off is an
 *   obstacle. The choice is remembered, and a template still needs somewhere to
 *   live afterwards, which is why the ribbon keeps its own way in.
 */

const DISMISSED = 'forge_hide_empty_guide'

/** Little sketches of what each template builds — a name is not a shape. */
const GLYPH: Record<string, ReactElement> = {
  plate: (
    <svg viewBox="0 0 32 20" className="w-8 h-5">
      <rect x="1.5" y="2.5" width="29" height="15" rx="1"
            fill="none" stroke="currentColor" strokeWidth="1.2" />
      {[[6, 6], [26, 6], [6, 14], [26, 14]].map(([cx, cy]) => (
        <circle key={`${cx}-${cy}`} cx={cx} cy={cy} r="1.8"
                fill="none" stroke="currentColor" strokeWidth="1.1" />
      ))}
    </svg>
  ),
  bracket: (
    <svg viewBox="0 0 32 20" className="w-8 h-5">
      <path d="M4 2 L4 18 L29 18 L29 14 L8 14 L8 2 Z"
            fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" />
      <circle cx="22" cy="16" r="1.4" fill="none" stroke="currentColor" strokeWidth="1" />
      <circle cx="6" cy="6" r="1.4" fill="none" stroke="currentColor" strokeWidth="1" />
    </svg>
  ),
  enclosure: (
    <svg viewBox="0 0 32 20" className="w-8 h-5">
      <path d="M3 4 L3 17 L29 17 L29 4" fill="none" stroke="currentColor"
            strokeWidth="1.2" strokeLinejoin="round" />
      <path d="M6 4 L6 14 L26 14 L26 4" fill="none" stroke="currentColor"
            strokeWidth="1" strokeDasharray="2 1.5" />
    </svg>
  ),
  shaft: (
    <svg viewBox="0 0 32 20" className="w-8 h-5">
      <rect x="2" y="6" width="20" height="8" rx="0.8"
            fill="none" stroke="currentColor" strokeWidth="1.2" />
      <rect x="22" y="8.5" width="8" height="3" rx="0.6"
            fill="none" stroke="currentColor" strokeWidth="1.2" />
    </svg>
  ),
}

export function EmptyDocumentGuide() {
  const { doc, busy, runOp, startSketchPick } = useDocumentStore()
  const [templates, setTemplates] = useState<PartTemplate[]>([])
  const [building, setBuilding] = useState<string | null>(null)
  const [hidden, setHidden] = useState(
    () => typeof localStorage !== 'undefined' && localStorage.getItem(DISMISSED) === '1')

  // A body is the exit condition: `derived.mesh_url` is absent for a document
  // whose tree prunes to no solid, which is exactly the state this explains.
  const empty = !!doc && !doc.derived?.mesh_url

  useEffect(() => {
    if (!doc || !empty || hidden || templates.length) return
    let cancelled = false
    api.listTemplates(doc.id)
      .then((r) => !cancelled && setTemplates(r.result?.templates ?? []))
      .catch(() => { /* the guide is optional; a failed listing just shows fewer options */ })
    return () => { cancelled = true }
  }, [doc, empty, hidden, templates.length])

  if (!doc || !empty || hidden) return null

  const build = async (key: string) => {
    setBuilding(key)
    try {
      await runOp('apply_template', { template: key })
    } finally {
      setBuilding(null)
    }
  }

  return (
    // Clear of the navigation hint in the bottom-left corner, which the card
    // would otherwise sit on top of.
    <div className="absolute inset-x-0 bottom-0 flex justify-center px-3 pt-3 pb-11 pointer-events-none">
      <div className="pointer-events-auto w-full max-w-2xl rounded-md border border-cad-border bg-cad-panel/95 backdrop-blur-sm shadow-lg px-3.5 py-3">
        <div className="flex items-start gap-2">
          <div className="flex-1 leading-tight">
            <p className="text-[12px] font-medium text-cad-text-heading">
              Empty part — three datum planes and no material yet
            </p>
            <p className="text-[10px] text-cad-text/70 mt-0.5">
              Start from a shape below and change its numbers, or sketch on a plane.
            </p>
          </div>
          <button
            onClick={() => { setHidden(true); localStorage.setItem(DISMISSED, '1') }}
            title="Don't show this again"
            className="p-1 -mt-0.5 rounded-sm text-cad-text/60 hover:text-cad-text-heading"
          >
            <X size={13} />
          </button>
        </div>

        <div className="grid grid-cols-2 sm:grid-cols-4 gap-1.5 mt-2.5">
          {templates.map((t) => (
            <button
              key={t.key}
              onClick={() => build(t.key)}
              disabled={busy}
              title={`${t.summary}\n\nParameters: ${t.parameters.map((p) => `${p.name} ${p.value} ${p.unit}`).join(', ')}`}
              className="flex flex-col items-start gap-1 px-2 py-1.5 rounded-sm border border-cad-border bg-cad-bg/50 text-cad-text hover:text-cad-accent-hot hover:border-cad-accent/50 transition-colors disabled:opacity-40"
            >
              <span className="flex items-center gap-1.5 w-full">
                {building === t.key
                  ? <Loader2 size={13} className="animate-spin shrink-0" />
                  : GLYPH[t.key] ?? <Box size={13} className="shrink-0" />}
              </span>
              <span className="text-[10.5px] font-medium text-left leading-tight">{t.name}</span>
              {/* The count is the promise: this many numbers to change, not a tree to build. */}
              <span className="tabular text-[9px] text-cad-text/50">
                {t.parameters.length} parameters
              </span>
            </button>
          ))}
        </div>

        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 mt-2.5 pt-2 border-t border-cad-border text-[10px]">
          <button
            onClick={startSketchPick}
            className="flex items-center gap-1.5 text-cad-text hover:text-cad-accent-hot transition-colors"
          >
            <PencilRuler size={11} /> Sketch on a plane
          </button>
          <button
            onClick={() => runOp('add_primitive', {
              shape: 'box', params: { size: { x: 0.05, y: 0.05, z: 0.05 } },
            })}
            disabled={busy}
            className="flex items-center gap-1.5 text-cad-text hover:text-cad-accent-hot transition-colors disabled:opacity-40"
          >
            <Box size={11} /> Add a 50 mm box
          </button>
          <span className="flex items-center gap-1.5 text-cad-text/50">
            <Sparkles size={11} /> or describe an edit in the bar above
          </span>
        </div>
      </div>
    </div>
  )
}

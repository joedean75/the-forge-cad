import { X } from 'lucide-react'
import { SHORTCUTS } from '../hooks/useShortcuts'

/** The `?` sheet. Reads `SHORTCUTS`, which the handler also reads, so the two
 *  cannot describe different keys. */
export function ShortcutSheet({ open, onClose }: { open: boolean; onClose: () => void }) {
  if (!open) return null
  const groups = [...new Set(SHORTCUTS.map((s) => s.group))]

  return (
    <div
      className="fixed inset-0 z-50 bg-black/50 flex items-center justify-center p-6"
      onClick={onClose}
    >
      <div
        className="w-[min(28rem,100%)] rounded-md border border-cad-border bg-cad-panel shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="h-11 border-b border-cad-border flex items-center gap-2.5 px-4">
          <span aria-hidden className="w-0.5 h-3.5 bg-cad-accent rounded-full" />
          <h2 className="font-display text-sm font-semibold text-cad-text-heading">Shortcuts</h2>
          <div className="flex-1" />
          <button onClick={onClose} className="p-1 rounded-sm text-cad-text hover:text-cad-text-heading">
            <X size={14} />
          </button>
        </div>
        <div className="p-4 space-y-3 max-h-[70vh] overflow-y-auto">
          {groups.map((group) => (
            <div key={group}>
              <span className="eyebrow text-cad-text/60">{group}</span>
              <div className="mt-1 space-y-0.5">
                {SHORTCUTS.filter((s) => s.group === group).map((s) => (
                  <div key={s.keys} className="flex items-center justify-between text-[11px]">
                    <span className="text-cad-text">{s.label}</span>
                    <kbd className="tabular text-[10px] px-1.5 py-0.5 rounded-sm border border-cad-border bg-cad-bg text-cad-text-heading">
                      {s.keys}
                    </kbd>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

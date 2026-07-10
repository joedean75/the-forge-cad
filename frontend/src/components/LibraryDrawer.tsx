import { useEffect, useState } from 'react'
import { X, Trash2, FileBox, Plus, Loader2 } from 'lucide-react'
import { api } from '../api/client'
import { useDocumentStore } from '../store/useDocumentStore'
import type { DocumentSummary } from '../../../shared/types'

function relTime(iso: string): string {
  const s = (Date.now() - new Date(iso).getTime()) / 1000
  if (s < 60) return 'just now'
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}

export function LibraryDrawer({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { openDocument, newBlank, doc } = useDocumentStore()
  const [items, setItems] = useState<DocumentSummary[]>([])
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!open) return
    setLoading(true)
    api.listDocuments().then(setItems).catch(() => setItems([])).finally(() => setLoading(false))
  }, [open])

  if (!open) return null

  const openDoc = async (id: string) => {
    await openDocument(id)
    onClose()
  }
  const remove = async (id: string, e: React.MouseEvent) => {
    e.stopPropagation()
    setItems((prev) => prev.filter((i) => i.id !== id))
    await api.deleteDocument(id).catch(() => {})
  }
  const create = async () => {
    await newBlank()
    onClose()
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className="w-[min(780px,92vw)] max-h-[82vh] flex flex-col bg-cad-panel border border-cad-border rounded-md overflow-hidden shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="h-12 border-b border-cad-border flex items-center justify-between px-4 shrink-0">
          <div className="flex items-center gap-2.5">
            <span aria-hidden className="w-0.5 h-3.5 bg-cad-accent rounded-full" />
            <div className="leading-none">
              <span className="eyebrow text-cad-text/70">Library</span>
              <h2 className="font-display text-sm font-semibold text-cad-text-heading leading-none mt-1">Parts</h2>
            </div>
            <span className="tabular text-[10px] text-cad-text/50 ml-1">{items.length}</span>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={create}
              className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-sm bg-cad-accent text-cad-bg text-[11px] font-semibold hover:bg-cad-accent-hot transition-colors"
            >
              <Plus size={13} /> New
            </button>
            <button onClick={onClose} className="p-1.5 rounded-sm text-cad-text hover:text-cad-text-heading hover:bg-cad-raised transition">
              <X size={16} />
            </button>
          </div>
        </div>

        <div className="flex-1 overflow-y-auto p-4">
          {loading ? (
            <div className="flex items-center justify-center h-40 text-cad-text gap-2 text-xs">
              <Loader2 size={14} className="animate-spin" /> Loading…
            </div>
          ) : items.length === 0 ? (
            <div className="flex flex-col items-center justify-center h-40 gap-2 text-center">
              <FileBox size={32} className="text-cad-border" />
              <p className="text-sm text-cad-text">No saved parts yet</p>
              <p className="text-[11px] text-cad-text/60">Generate one from a prompt, or start a blank body.</p>
            </div>
          ) : (
            <div className="grid grid-cols-2 sm:grid-cols-3 gap-2.5">
              {items.map((it) => {
                const active = it.id === doc?.id
                return (
                  <button
                    key={it.id}
                    onClick={() => openDoc(it.id)}
                    className={`group relative text-left p-3 rounded-sm border transition-colors ${
                      active
                        ? 'border-cad-accent/60 bg-cad-accent/[0.06]'
                        : 'border-cad-border bg-cad-bg/40 hover:border-stress-void'
                    }`}
                  >
                    <FileBox size={18} className={active ? 'text-cad-accent' : 'text-cad-text/70'} />
                    <div className="mt-2 text-xs text-cad-text-heading font-medium truncate">{it.name}</div>
                    <div className="mt-1 flex items-center justify-between">
                      <span className="eyebrow text-cad-text/60">{it.provenance.replace('_', '-')}</span>
                      <span className="tabular text-[9px] text-cad-text/50">{relTime(it.updated_at)}</span>
                    </div>
                    <span
                      onClick={(e) => remove(it.id, e)}
                      title="Delete"
                      className="absolute top-2 right-2 opacity-0 group-hover:opacity-100 p-1 rounded-sm text-cad-text hover:text-cad-danger transition"
                    >
                      <Trash2 size={13} />
                    </span>
                  </button>
                )
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

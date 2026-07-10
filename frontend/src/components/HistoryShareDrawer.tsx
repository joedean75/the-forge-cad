import { useEffect, useState } from 'react'
import { X, History, GitBranch, RotateCcw, Save, Users, Trash2, Loader2, Share2 } from 'lucide-react'
import { api } from '../api/client'
import { useDocumentStore } from '../store/useDocumentStore'
import { useAuthStore } from '../store/useAuthStore'
import type { VersionSummary, Collaborator } from '../../../shared/types'

export function HistoryShareDrawer({ open, onClose }: { open: boolean; onClose: () => void }) {
  const doc = useDocumentStore((s) => s.doc)
  const openDocument = useDocumentStore((s) => s.openDocument)
  const account = useAuthStore((s) => s.account)

  const [versions, setVersions] = useState<VersionSummary[]>([])
  const [collaborators, setCollaborators] = useState<Collaborator[]>([])
  const [label, setLabel] = useState('')
  const [email, setEmail] = useState('')
  const [role, setRole] = useState<'viewer' | 'editor'>('viewer')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const canManage = !!doc && (doc.owner_id == null || doc.owner_id === account?.id)

  const reload = async () => {
    if (!doc) return
    try {
      const [v, c] = await Promise.all([api.listVersions(doc.id), api.listCollaborators(doc.id)])
      setVersions(v); setCollaborators(c)
    } catch { /* non-critical */ }
  }

  useEffect(() => { if (open) { setErr(null); reload() } }, [open, doc?.id]) // eslint-disable-line react-hooks/exhaustive-deps

  if (!open || !doc) return null

  const wrap = async (fn: () => Promise<void>) => {
    setBusy(true); setErr(null)
    try { await fn() } catch (e) { setErr(e instanceof Error ? e.message : 'Action failed') } finally { setBusy(false) }
  }

  const saveVersion = () => wrap(async () => {
    await api.createVersion(doc.id, label || `Version ${versions.length + 1}`)
    setLabel(''); await reload()
  })
  const restore = (vid: string) => wrap(async () => { await api.restoreVersion(doc.id, vid); await openDocument(doc.id); await reload() })
  const branch = (vid: string) => wrap(async () => { const b = await api.branchVersion(doc.id, vid); await openDocument(b.id); onClose() })
  const share = () => wrap(async () => { await api.addCollaborator(doc.id, email.trim(), role); setEmail(''); await reload() })
  const unshare = (accId: string) => wrap(async () => { await api.removeCollaborator(doc.id, accId); await reload() })

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-black/40" onClick={onClose}>
      <aside className="w-[420px] max-w-full h-full bg-cad-panel border-l border-cad-border flex flex-col overflow-y-auto" onClick={(e) => e.stopPropagation()}>
        <div className="h-12 border-b border-cad-border flex items-center justify-between px-4 shrink-0">
          <div className="flex items-center gap-2.5">
            <span aria-hidden className="w-0.5 h-3.5 bg-cad-accent rounded-full" />
            <h2 className="font-display text-sm font-semibold text-cad-text-heading">History &amp; Sharing</h2>
          </div>
          <button onClick={onClose} className="p-1.5 rounded-sm text-cad-text hover:text-cad-text-heading hover:bg-cad-raised"><X size={16} /></button>
        </div>

        <div className="p-5 space-y-6">
          {err && <p className="text-[11px] text-cad-danger">{err}</p>}

          {/* versions */}
          <div className="space-y-2">
            <span className="eyebrow text-cad-text/70 flex items-center gap-1"><History size={11} /> Version history</span>
            <div className="flex items-center gap-1.5">
              <input value={label} onChange={(e) => setLabel(e.target.value)} placeholder="version label"
                className="flex-1 px-2 py-1 rounded-sm bg-cad-bg border border-cad-border text-cad-text-heading text-[10px] outline-none" />
              <button onClick={saveVersion} disabled={busy}
                className="flex items-center gap-1 px-2 py-1 rounded-sm bg-cad-accent text-cad-bg text-[10px] font-semibold hover:bg-cad-accent-hot disabled:opacity-40">
                <Save size={11} /> Save
              </button>
            </div>
            {versions.length === 0 && <p className="text-[10px] text-cad-text/50">No saved versions yet.</p>}
            {versions.map((v) => (
              <div key={v.id} className="flex items-center justify-between rounded-sm border border-cad-border px-2 py-1.5">
                <div className="min-w-0">
                  <p className="text-[11px] text-cad-text-heading truncate">{v.label}</p>
                  <p className="tabular text-[9px] text-cad-text/50">{new Date(v.created_at).toLocaleString()}</p>
                </div>
                <div className="flex items-center gap-1 shrink-0">
                  <button onClick={() => restore(v.id)} disabled={busy} title="Restore this version"
                    className="p-1 rounded-sm text-cad-text hover:text-cad-accent-hot disabled:opacity-40"><RotateCcw size={12} /></button>
                  <button onClick={() => branch(v.id)} disabled={busy} title="Branch into a new document"
                    className="p-1 rounded-sm text-cad-text hover:text-cad-accent-hot disabled:opacity-40"><GitBranch size={12} /></button>
                </div>
              </div>
            ))}
          </div>

          {/* sharing */}
          <div className="space-y-2">
            <span className="eyebrow text-cad-text/70 flex items-center gap-1"><Users size={11} /> Shared with</span>
            {!canManage && <p className="text-[10px] text-cad-text/50">Only the owner can manage sharing.</p>}
            {canManage && (
              <div className="flex items-center gap-1.5">
                <input value={email} onChange={(e) => setEmail(e.target.value)} placeholder="teammate@company.com" type="email"
                  className="flex-1 px-2 py-1 rounded-sm bg-cad-bg border border-cad-border text-cad-text-heading text-[10px] outline-none" />
                <select value={role} onChange={(e) => setRole(e.target.value as 'viewer' | 'editor')}
                  className="bg-cad-bg border border-cad-border rounded-sm text-[10px] text-cad-text-heading px-1 py-1 outline-none">
                  <option value="viewer">viewer</option>
                  <option value="editor">editor</option>
                </select>
                <button onClick={share} disabled={busy || !email.trim()}
                  className="flex items-center gap-1 px-2 py-1 rounded-sm bg-cad-accent text-cad-bg text-[10px] font-semibold hover:bg-cad-accent-hot disabled:opacity-40">
                  <Share2 size={11} /> Share
                </button>
              </div>
            )}
            {collaborators.length === 0 && <p className="text-[10px] text-cad-text/50">Not shared with anyone.</p>}
            {collaborators.map((c) => (
              <div key={c.account_id} className="flex items-center justify-between text-[10px] text-cad-text">
                <span className="tabular truncate">{c.email} · {c.role}</span>
                {canManage && (
                  <button onClick={() => unshare(c.account_id)} disabled={busy} className="text-cad-text hover:text-cad-danger"><Trash2 size={11} /></button>
                )}
              </div>
            ))}
          </div>

          {busy && <div className="flex items-center gap-1.5 text-[10px] text-cad-text"><Loader2 size={11} className="animate-spin" /> working…</div>}
        </div>
      </aside>
    </div>
  )
}

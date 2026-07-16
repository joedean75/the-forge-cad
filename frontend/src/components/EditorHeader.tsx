import { useEffect, useState } from 'react'
import { Sparkles, Plus, Loader2, Cpu, FolderOpen, Undo2, Redo2, UserCircle2, History, Save, Check, Bot } from 'lucide-react'
import { useDocumentStore } from '../store/useDocumentStore'
import { useAuthStore } from '../store/useAuthStore'
import { api } from '../api/client'
import type { LlmConfig } from '../../../shared/types'

function TrussMark() {
  return (
    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" aria-hidden>
      <path
        d="M4 19 L12 6 L20 19 M4 19 L20 19 M12 6 L12 19"
        stroke="var(--color-stress-void)"
        strokeWidth="1.25"
        strokeLinejoin="round"
      />
      <circle cx="4" cy="19" r="1.8" fill="var(--color-cad-text)" />
      <circle cx="20" cy="19" r="1.8" fill="var(--color-cad-text)" />
      <circle cx="12" cy="6" r="2.4" fill="var(--color-cad-accent)" />
    </svg>
  )
}

// Friendly vendor headings for OpenRouter's "vendor/model" slugs.
const VENDOR_LABEL: Record<string, string> = {
  anthropic: 'Anthropic · Claude',
  openai: 'OpenAI · GPT',
  google: 'Google · Gemini',
  'x-ai': 'xAI · Grok',
  moonshotai: 'Moonshot · Kimi',
  deepseek: 'DeepSeek',
  'meta-llama': 'Meta · Llama',
  mistralai: 'Mistral',
}

/** Group "vendor/model" slugs under a vendor heading; flat models fall under one. */
function groupModels(models: string[]): [string, { value: string; label: string }[]][] {
  const groups = new Map<string, { value: string; label: string }[]>()
  for (const m of models) {
    const slash = m.indexOf('/')
    const vendor = slash > 0 ? m.slice(0, slash) : ''
    const heading = vendor ? VENDOR_LABEL[vendor] ?? vendor : 'Models'
    const label = slash > 0 ? m.slice(slash + 1) : m
    if (!groups.has(heading)) groups.set(heading, [])
    groups.get(heading)!.push({ value: m, label })
  }
  return [...groups.entries()]
}

function SaveControl() {
  const { doc, busy, saveCheckpoint } = useDocumentStore()
  const [flash, setFlash] = useState(false)

  const save = async () => {
    if (useDocumentStore.getState().busy) return
    if (await saveCheckpoint()) {
      setFlash(true)
      setTimeout(() => setFlash(false), 1600)
    }
  }

  // Ctrl/Cmd+S saves a checkpoint (and stops the browser's save dialog).
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
        e.preventDefault()
        save()
      }
    }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  if (!doc) return null
  const justSaved = flash && !busy

  return (
    <button
      onClick={save}
      disabled={busy}
      title="Your work autosaves on every edit. Click (Ctrl+S) to save a restore point you can return to."
      className={`flex items-center gap-1.5 h-9 px-2.5 rounded-sm border text-[11px] font-medium transition-colors disabled:opacity-70 ${
        justSaved ? 'border-cad-success/50 text-cad-success' : 'border-cad-border text-cad-text hover:text-cad-text-heading hover:border-stress-void'
      }`}
    >
      {busy ? <Loader2 size={13} className="animate-spin" /> : justSaved ? <Check size={14} /> : <Save size={14} />}
      {busy ? 'Saving…' : justSaved ? 'Saved' : 'Saved'}
    </button>
  )
}

function LlmSelector() {
  const [cfg, setCfg] = useState<LlmConfig | null>(null)
  const [updating, setUpdating] = useState(false)

  useEffect(() => {
    api.getLlmConfig().then(setCfg).catch(() => setCfg(null))
  }, [])

  const on = cfg?.configured ?? false

  const changeModel = async (model: string) => {
    if (!cfg || model === cfg.model) return
    setUpdating(true)
    try {
      setCfg(await api.setLlmModel(model))
    } catch {
      /* keep prior config on failure */
    } finally {
      setUpdating(false)
    }
  }

  return (
    <div
      className="flex items-center gap-1.5 h-9 pl-2 pr-1.5 rounded-sm border border-cad-border shrink-0"
      title={on ? `Text-to-CAD via ${cfg?.provider}` : 'No LLM key — using the rule-based parser (add LLM_API_KEY to backend/.env)'}
    >
      <Cpu size={13} className={on ? 'text-cad-success' : 'text-cad-text/50'} />
      {on && cfg ? (
        <select
          value={cfg.model}
          onChange={(e) => changeModel(e.target.value)}
          disabled={updating}
          className="tabular max-w-[180px] bg-transparent text-[10px] text-cad-text-heading outline-none cursor-pointer disabled:opacity-50"
        >
          {groupModels(cfg.available_models).map(([heading, items]) => (
            <optgroup key={heading} label={heading} className="bg-cad-panel text-cad-text">
              {items.map((it) => (
                <option key={it.value} value={it.value} className="bg-cad-panel text-cad-text-heading">
                  {it.label}
                </option>
              ))}
            </optgroup>
          ))}
        </select>
      ) : (
        <span className="tabular text-[10px] text-cad-text/60 pr-1">rule-based</span>
      )}
      {updating && <Loader2 size={11} className="animate-spin text-cad-text shrink-0" />}
    </div>
  )
}

export function EditorHeader({ onOpenLibrary, onOpenAccount, onOpenHistory, onOpenCopilot, copilotOpen, peers = [] }: { onOpenLibrary: () => void; onOpenAccount: () => void; onOpenHistory: () => void; onOpenCopilot: () => void; copilotOpen: boolean; peers?: { id: string; email: string }[] }) {
  const { busy, createFromPrompt, newBlank, doc, undo, redo, past, future } = useDocumentStore()
  const account = useAuthStore((s) => s.account)
  const [prompt, setPrompt] = useState('')
  const others = peers.filter((p) => p.id !== account?.id)

  const submit = () => {
    if (!prompt.trim() || busy) return
    createFromPrompt(prompt)
    setPrompt('')
  }

  return (
    <header className="h-14 border-b border-cad-border bg-cad-panel flex items-center gap-4 pl-3 pr-4 shrink-0 z-20">
      <div className="flex items-center gap-2.5 shrink-0">
        <TrussMark />
        <div className="leading-none">
          <h1 className="font-display text-cad-text-heading font-semibold text-[15px] tracking-tight leading-none">
            Forge
          </h1>
          <p className="eyebrow text-cad-text mt-1 leading-none">Design Studio</p>
        </div>
        <button
          onClick={onOpenLibrary}
          title="Open parts library"
          className="flex items-center gap-1.5 h-9 px-2.5 ml-1 rounded-sm border border-cad-border text-cad-text hover:text-cad-text-heading hover:border-stress-void transition-colors"
        >
          <FolderOpen size={14} />
          <span className="text-[11px] font-medium">Files</span>
        </button>
        <div className="flex items-center gap-0.5 ml-1">
          <button
            onClick={undo}
            disabled={past.length === 0 || busy}
            title="Undo (Ctrl+Z)"
            className="flex items-center justify-center h-9 w-8 rounded-sm text-cad-text hover:text-cad-text-heading hover:bg-cad-raised transition-colors disabled:opacity-30"
          >
            <Undo2 size={15} />
          </button>
          <button
            onClick={redo}
            disabled={future.length === 0 || busy}
            title="Redo (Ctrl+Shift+Z)"
            className="flex items-center justify-center h-9 w-8 rounded-sm text-cad-text hover:text-cad-text-heading hover:bg-cad-raised transition-colors disabled:opacity-30"
          >
            <Redo2 size={15} />
          </button>
        </div>
        <SaveControl />
      </div>

      {/* always-available text-to-CAD prompt bar */}
      <div className="flex-1 max-w-2xl mx-auto flex items-center gap-2">
        <div className="flex-1 flex items-center gap-2 h-9 px-3 rounded-sm bg-cad-bg border border-cad-border focus-within:border-cad-accent transition-colors">
          <Sparkles size={14} className="text-cad-accent shrink-0" />
          <input
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && submit()}
            placeholder="Describe a part — e.g. a 100×50×30 mm bracket with two mounting holes"
            className="flex-1 bg-transparent text-xs text-cad-text-heading placeholder:text-cad-text/50 outline-none"
          />
          <button
            onClick={submit}
            disabled={!prompt.trim() || busy}
            className="flex items-center gap-1.5 px-2.5 h-6 rounded-sm bg-cad-accent text-cad-bg text-[11px] font-semibold hover:bg-cad-accent-hot transition-colors disabled:opacity-40"
          >
            {busy ? <Loader2 size={12} className="animate-spin" /> : <Sparkles size={12} />}
            Generate
          </button>
        </div>
        <button
          onClick={newBlank}
          disabled={busy}
          title="New blank body"
          className="flex items-center justify-center h-9 w-9 rounded-sm border border-cad-border text-cad-text hover:text-cad-text-heading hover:border-stress-void transition-colors disabled:opacity-40"
        >
          <Plus size={16} />
        </button>
        <button
          onClick={onOpenCopilot}
          disabled={!doc}
          title="Forge Copilot — describe a goal, it plans and builds"
          className={`flex items-center gap-1.5 h-9 px-2.5 rounded-sm border text-[11px] font-semibold transition-colors disabled:opacity-40 ${
            copilotOpen ? 'border-cad-accent/60 bg-cad-accent/12 text-cad-accent-hot' : 'border-cad-border text-cad-text hover:text-cad-text-heading hover:border-stress-void'
          }`}
        >
          <Bot size={15} /> Copilot
        </button>
        <LlmSelector />
      </div>

      {/* live collaborators on this document */}
      {others.length > 0 && (
        <div
          className="flex items-center gap-1.5 h-9 px-2.5 rounded-sm border border-cad-success/40 bg-cad-success/5 shrink-0"
          title={`Also here: ${others.map((p) => p.email).join(', ')}`}
        >
          <span className="w-1.5 h-1.5 rounded-full bg-cad-success animate-pulse" />
          <span className="text-[10px] text-cad-success font-medium">
            {others.length === 1 ? others[0].email.split('@')[0] : `${others.length} online`}
          </span>
        </div>
      )}

      {/* version history & sharing */}
      <button
        onClick={onOpenHistory}
        disabled={!doc}
        title="Version history & sharing"
        className="flex items-center justify-center h-9 w-9 rounded-sm border border-cad-border text-cad-text hover:text-cad-text-heading hover:border-stress-void transition-colors shrink-0 disabled:opacity-40"
      >
        <History size={15} />
      </button>

      {/* account / subscription */}
      <button
        onClick={onOpenAccount}
        title={account ? `${account.email} · ${account.plan.name} plan` : 'Sign in'}
        className="flex items-center gap-1.5 h-9 px-2.5 rounded-sm border border-cad-border text-cad-text hover:text-cad-text-heading hover:border-stress-void transition-colors shrink-0"
      >
        <UserCircle2 size={15} />
        <span className="text-[11px] font-medium max-w-[90px] truncate">{account ? account.plan.name : 'Sign in'}</span>
      </button>
    </header>
  )
}

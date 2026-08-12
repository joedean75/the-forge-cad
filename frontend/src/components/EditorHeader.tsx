import { useEffect, useRef, useState } from 'react'
import { Sparkles, Plus, Loader2, Cpu, FolderOpen, Undo2, Redo2, UserCircle2, History, Save, Check, Bot, ImagePlus, ArrowLeft } from 'lucide-react'
import { useDocumentStore } from '../store/useDocumentStore'
import { useAuthStore } from '../store/useAuthStore'
import { useNavStore } from '../store/useNavStore'
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
      title={on
        ? `Text-to-CAD via ${cfg?.provider}`
        : 'No LLM key — describing a part is unavailable. Start from a template instead, or add LLM_API_KEY to backend/.env.'}
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
        // Not "rule-based" any more: there is no parser behind this, so
        // saying so would promise a fallback that no longer exists.
        <span className="tabular text-[10px] text-cad-text/60 pr-1">no model</span>
      )}
      {updating && <Loader2 size={11} className="animate-spin text-cad-text shrink-0" />}
    </div>
  )
}

export function EditorHeader({ onOpenLibrary, onOpenAccount, onOpenHistory, onOpenCopilot, copilotOpen, peers = [] }: { onOpenLibrary: () => void; onOpenAccount: () => void; onOpenHistory: () => void; onOpenCopilot: () => void; copilotOpen: boolean; peers?: { id: string; email: string }[] }) {
  const { busy, createFromPrompt, createFromImage, aiEdit, newBlank, doc, undo, redo, past, future } = useDocumentStore()
  const account = useAuthStore((s) => s.account)
  const returnTo = useNavStore((s) => s.returnTo)
  const goBack = useNavStore((s) => s.goBack)
  const [prompt, setPrompt] = useState('')
  const imageInput = useRef<HTMLInputElement>(null)
  // Text-to-CAD is a single ~20s call; surface the real phases (interpret →
  // generate → evaluate) on a timer so the wait isn't a blind spinner. Driven
  // off `busy` in an effect so the timers are always cleaned up.
  const [generating, setGenerating] = useState(false)
  const [genPhase, setGenPhase] = useState<string | null>(null)
  const others = peers.filter((p) => p.id !== account?.id)

  useEffect(() => {
    if (!generating || !busy) { setGenPhase(null); return }
    const phases = ['Interpreting your description…', 'Generating geometry…', 'Evaluating the model…']
    let i = 0
    setGenPhase(phases[0])
    const timer = window.setInterval(() => { i = Math.min(i + 1, phases.length - 1); setGenPhase(phases[i]) }, 6000)
    return () => window.clearInterval(timer)
  }, [generating, busy])

  useEffect(() => { if (!busy && generating) setGenerating(false) }, [busy, generating])

  /**
   * With a part on screen, the bar edits it; with nothing, it makes one.
   *
   * The bar used to only ever call `createFromPrompt`, so typing into it with a
   * part open silently *replaced* that part with a new document — the one thing
   * a user typing a change to what they are looking at does not want. `ai_edit`
   * has existed the whole time and took a whole-part instruction. Creating is
   * still one click away, and is spelled out rather than being the surprise.
   */
  const hasBody = !!doc?.derived?.mesh_url

  const submit = () => {
    if (!prompt.trim() || busy) return
    if (hasBody) {
      aiEdit(prompt)
    } else {
      createFromPrompt(prompt)
      setGenerating(true)
    }
    setPrompt('')
  }

  const submitAsNew = () => {
    if (!prompt.trim() || busy) return
    createFromPrompt(prompt)
    setPrompt('')
    setGenerating(true)
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
        {returnTo ? (
          // Opened from an assembly: offer the way back rather than dropping the
          // user in the library, which would lose the assembly they were composing.
          <button
            onClick={goBack}
            title={`Back to ${returnTo.label}`}
            className="flex items-center gap-1.5 h-9 px-2.5 ml-1 rounded-sm border border-cad-accent/50 bg-cad-accent/10 text-cad-accent-hot hover:bg-cad-accent/20 transition-colors max-w-[220px]"
          >
            <ArrowLeft size={14} className="shrink-0" />
            <span className="text-[11px] font-medium truncate">{returnTo.label}</span>
          </button>
        ) : (
          <button
            onClick={onOpenLibrary}
            title="Open parts library"
            className="flex items-center gap-1.5 h-9 px-2.5 ml-1 rounded-sm border border-cad-border text-cad-text hover:text-cad-text-heading hover:border-stress-void transition-colors"
          >
            <FolderOpen size={14} />
            <span className="text-[11px] font-medium">Files</span>
          </button>
        )}
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
          {genPhase ? (
            <span className="flex-1 flex items-center gap-2 text-xs text-cad-accent-hot">
              <Loader2 size={12} className="animate-spin shrink-0" /> {genPhase}
            </span>
          ) : (
            <input
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && submit()}
              placeholder={hasBody
                ? 'Describe a change — e.g. round the top edges to 3 mm'
                : 'Describe a part — e.g. a 100×50×30 mm bracket with two mounting holes'}
              className="flex-1 bg-transparent text-xs text-cad-text-heading placeholder:text-cad-text/50 outline-none"
            />
          )}
          {/* Generate from a picture instead of words. Any text already in the
              box rides along as a hint, so "from this photo, 60 mm wide" works. */}
          <button
            onClick={() => imageInput.current?.click()}
            disabled={busy}
            title="Generate from an image — a sketch, logo, drawing or photo"
            className="shrink-0 text-cad-text hover:text-cad-accent transition-colors disabled:opacity-40"
          >
            <ImagePlus size={14} />
          </button>
          <input
            ref={imageInput}
            type="file"
            accept="image/*"
            className="hidden"
            onChange={(e) => {
              const file = e.target.files?.[0]
              e.target.value = ''  // let the same file be picked again
              if (!file || busy) return
              createFromImage(file, prompt)
              setPrompt('')
              setGenerating(true)
            }}
          />
          {hasBody && (
            <button
              onClick={submitAsNew}
              disabled={!prompt.trim() || busy}
              title="Generate a new part from this description instead of editing the open one"
              className="shrink-0 px-1.5 h-6 rounded-sm text-[11px] text-cad-text hover:text-cad-accent-hot transition-colors disabled:opacity-40"
            >
              New part
            </button>
          )}
          <button
            onClick={submit}
            disabled={!prompt.trim() || busy}
            title={hasBody ? 'Apply this as an edit to the open part' : 'Generate a part'}
            className="flex items-center gap-1.5 px-2.5 h-6 rounded-sm bg-cad-accent text-cad-bg text-[11px] font-semibold hover:bg-cad-accent-hot transition-colors disabled:opacity-40"
          >
            {busy ? <Loader2 size={12} className="animate-spin" /> : <Sparkles size={12} />}
            {hasBody ? 'Edit' : 'Generate'}
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

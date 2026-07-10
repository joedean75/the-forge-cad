import { useEffect, useState } from 'react'
import { Sparkles, Loader2, Box, Puzzle, Wand2 } from 'lucide-react'
import { useAppStore } from '../../store/useAppStore'
import { api } from '../../api/client'
import { LlmModelSelector } from '../../components/LlmModelSelector'
import type { Candidate } from '../../../../shared/types'

const EXAMPLES = [
  {
    label: 'RC chassis',
    mode: 'auto' as const,
    prompt: 'a rally style rc chassis on a 185x185x185 3d printer build plate',
  },
  {
    label: 'Aluminum bracket',
    mode: 'part' as const,
    prompt: 'a 100x50x30 mm aluminum bracket with two mounting holes',
  },
  {
    label: 'Truss frame',
    mode: 'assembly' as const,
    prompt: 'a truss frame spanning 2 meters',
  },
  {
    label: 'Gear knob',
    mode: 'auto' as const,
    prompt: 'a small plastic gear knob 30mm diameter',
  },
]

export function GenerativePanel() {
  const { activeJob, setCandidates, setSelectedCandidate, setAssembly, setStep } = useAppStore()

  const [prompt, setPrompt] = useState('')
  const [mode, setMode] = useState<'auto' | 'part' | 'assembly'>('auto')
  const [generating, setGenerating] = useState(false)

  useEffect(() => {
    if (!activeJob) return
    if (activeJob.status === 'done') {
      api.getResult(activeJob.id).then((res) => setCandidates(res.candidates)).catch(console.error)
      return
    }

    const interval = setInterval(() => {
      api
        .getJob(activeJob.id)
        .then((job) => {
          useAppStore.getState().setActiveJob(job)
          if (job.status === 'done') {
            api.getResult(activeJob.id).then((res) => setCandidates(res.candidates)).catch(console.error)
          }
        })
        .catch(console.error)
    }, 800)

    return () => clearInterval(interval)
  }, [activeJob, setCandidates])

  const generate = async () => {
    if (!prompt.trim()) return
    setGenerating(true)
    try {
      if (mode === 'assembly') {
        const assembly = await api.generateAssembly(prompt)
        setAssembly(assembly)
        setStep('assembly')
        return
      }
      if (mode === 'part') {
        const candidate = await api.generatePart(prompt)
        setCandidates([candidate])
        setSelectedCandidate(candidate)
        setStep('topology')
        return
      }
      const result = await api.generateDesign(prompt)
      if (result.kind === 'assembly') {
        setAssembly(result.assembly!)
        setStep('assembly')
      } else {
        setCandidates([result.part!])
        setSelectedCandidate(result.part!)
        setStep('topology')
      }
    } catch (err) {
      console.error(err)
    } finally {
      setGenerating(false)
    }
  }

  return (
    <div className="space-y-5">
      <section className="p-4 rounded-xl border border-cad-border bg-gradient-to-br from-cad-bg/80 to-cad-panel/50">
        <div className="flex items-center gap-2 mb-2">
          <Wand2 size={14} className="text-cad-accent" />
          <h3 className="sec-head">
            Text-to-CAD
          </h3>
        </div>
        <p className="text-[10px] text-cad-text mb-3">
          Describe a part or assembly in plain English. The LLM will extract dimensions and generate
          kernel-ready geometry.
        </p>

        <textarea
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              generate()
            }
          }}
          placeholder="e.g. a lightweight drone arm 200mm long, 20mm wide, 5mm thick with two mounting holes"
          className="w-full h-24 px-3 py-2 rounded-lg bg-cad-bg border border-cad-border text-cad-text-heading text-xs focus:border-cad-accent outline-none resize-none"
        />

        <div className="flex items-center justify-between mt-3">
          <div className="flex items-center gap-2">
            <div className="flex items-center gap-1 p-0.5 rounded-lg bg-cad-bg border border-cad-border">
            {(
              [
                { id: 'auto', label: 'Auto', icon: Sparkles },
                { id: 'part', label: 'Part', icon: Box },
                { id: 'assembly', label: 'Assembly', icon: Puzzle },
              ] as const
            ).map((m) => {
              const Icon = m.icon
              const active = mode === m.id
              return (
                <button
                  key={m.id}
                  onClick={() => setMode(m.id)}
                  className={`flex items-center gap-1 px-2 py-1 rounded-md text-[10px] transition ${
                    active
                      ? 'bg-cad-accent text-white'
                      : 'text-cad-text hover:text-cad-text-heading'
                  }`}
                >
                  <Icon size={10} />
                  {m.label}
                </button>
              )
            })}
            </div>
            <LlmModelSelector />
          </div>

          <button
            onClick={generate}
            disabled={generating || !prompt.trim()}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-cad-success text-white text-xs disabled:opacity-50"
          >
            {generating ? <Loader2 size={12} className="animate-spin" /> : <Sparkles size={12} />}
            {generating ? 'Generating…' : `Generate ${mode === 'auto' ? 'Design' : mode}`}
          </button>
        </div>
      </section>

      <section>
        <h4 className="eyebrow text-cad-text-heading mb-2">
          Examples
        </h4>
        <div className="grid grid-cols-1 gap-2">
          {EXAMPLES.map((example) => (
            <button
              key={example.label}
              onClick={() => {
                setPrompt(example.prompt)
                setMode(example.mode)
              }}
              className="text-left p-2 rounded border border-cad-border bg-cad-bg/50 hover:border-cad-accent transition"
            >
              <div className="flex items-center justify-between mb-0.5">
                <span className="text-xs font-medium text-cad-text-heading">{example.label}</span>
                <span className="text-[9px] px-1 py-0.5 rounded bg-cad-border text-cad-text capitalize">
                  {example.mode}
                </span>
              </div>
              <div className="text-[10px] text-cad-text truncate">{example.prompt}</div>
            </button>
          ))}
        </div>
      </section>

      <SimpResults />
    </div>
  )
}

function SimpResults() {
  const { activeJob, candidates, selectedCandidate, setSelectedCandidate, setStep } = useAppStore()

  if (!activeJob && candidates.length === 0) return null

  if (activeJob?.status !== 'done' && candidates.length === 0) {
    return (
      <section className="space-y-2 pt-2 border-t border-cad-border">
        <h4 className="eyebrow text-cad-text-heading">
          Generative Study
        </h4>
        <div className="text-xs text-cad-text">{activeJob?.message}</div>
        <div className="h-2 bg-cad-border rounded overflow-hidden">
          <div
            className="h-full bg-cad-accent transition-all duration-300"
            style={{ width: `${(activeJob?.progress ?? 0) * 100}%` }}
          />
        </div>
      </section>
    )
  }

  return (
    <section className="space-y-3 pt-2 border-t border-cad-border">
      <h4 className="eyebrow text-cad-text-heading">
        Optimized Candidates
      </h4>
      <p className="text-[10px] text-cad-text">Select a generated outcome to promote to topology review.</p>
      <div className="grid grid-cols-1 gap-3">
        {candidates.map((c: Candidate, idx: number) => (
          <button
            key={c.id}
            onClick={() => {
              setSelectedCandidate(c)
              setStep('topology')
            }}
            className={`text-left p-3 rounded border transition ${
              selectedCandidate?.id === c.id
                ? 'border-cad-accent bg-cad-accent/10'
                : 'border-cad-border bg-cad-bg/50 hover:border-cad-accent/50'
            }`}
          >
            <div className="flex items-center justify-between mb-2">
              <span className="text-xs font-medium text-cad-text-heading">
                {idx === 0 ? '🏆 ' : ''}
                {c.name}
              </span>
              <span className="text-[10px] px-1.5 py-0.5 rounded bg-cad-accent/20 text-cad-accent">
                Score {c.score}
              </span>
            </div>
            <div className="grid grid-cols-3 gap-2 text-[10px] text-cad-text">
              <div>
                <div className="text-cad-text-heading">{c.mass.toFixed(3)} kg</div>
                <div>Mass</div>
              </div>
              <div>
                <div className="text-cad-text-heading">{c.stiffness.toExponential(1)}</div>
                <div>Stiffness</div>
              </div>
              <div>
                <div className="text-cad-text-heading">{c.max_stress} MPa</div>
                <div>Stress</div>
              </div>
            </div>
          </button>
        ))}
      </div>
    </section>
  )
}

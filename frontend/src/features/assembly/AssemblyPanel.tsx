import { useState } from 'react'
import { Sparkles } from 'lucide-react'
import { useAppStore } from '../../store/useAppStore'
import { api } from '../../api/client'

export function AssemblyPanel() {
  const { assembly, setAssembly } = useAppStore()
  const [prompt, setPrompt] = useState('')
  const [generating, setGenerating] = useState(false)

  const generate = async () => {
    if (!prompt.trim()) return
    setGenerating(true)
    try {
      const result = await api.generateAssembly(prompt)
      setAssembly(result)
    } catch (err) {
      console.error(err)
    } finally {
      setGenerating(false)
    }
  }

  const totalPrintTime = assembly?.nodes.reduce((sum, n) => sum + n.print_time_minutes * n.quantity, 0) ?? 0

  return (
    <div className="space-y-5">
      <section className="p-3 rounded border border-cad-border bg-cad-bg/50">
        <h3 className="sec-head mb-2 flex items-center gap-1">
          <Sparkles size={12} className="text-cad-accent" /> Text-to-Assembly
        </h3>
        <textarea
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              generate()
            }
          }}
          placeholder="Describe an assembly, e.g. 'bridge span 3 m width 0.6 m' or 'space frame 2x1 m' or 'tower height 2 m 4 levels'"
          className="w-full h-20 px-2 py-1.5 rounded bg-cad-bg border border-cad-border text-cad-text-heading text-xs focus:border-cad-accent outline-none resize-none"
        />
        <div className="flex items-center justify-between mt-2">
          <div className="flex flex-wrap gap-1">
            {[
              'bridge span 3 m width 0.6 m',
              'space frame 2x1 m height 0.3 m',
              'tower height 2 m 4 levels',
              'truss span 2 m 5 bays',
              'portal frame span 2 m height 1.5 m',
              'radial frame diameter 1 m 12 segments',
              'gantry span 2 m height 1.2 m width 0.5 m',
              'crane height 2 m jib 1.5 m',
              'scissor lift span 1.2 m height 0.6 m',
              'platform shelf 1x0.6 m height 1.5 m 3 levels',
              'arch span 2 m height 0.8 m',
              'cantilever span 1 m height 0.6 m',
            ].map((example) => (
              <button
                key={example}
                onClick={() => {
                  setPrompt(example)
                  api.generateAssembly(example).then(setAssembly).catch(console.error)
                }}
                className="text-[10px] px-2 py-0.5 rounded border border-cad-border text-cad-text hover:border-cad-accent hover:text-cad-accent"
              >
                {example}
              </button>
            ))}
          </div>
          <button
            onClick={generate}
            disabled={generating || !prompt.trim()}
            className="flex items-center gap-1 px-2 py-1 rounded bg-cad-accent text-white text-xs disabled:opacity-50"
          >
            {generating ? 'Generating…' : 'Generate'}
            <Sparkles size={12} />
          </button>
        </div>
      </section>

      {!assembly ? (
        <div className="text-sm text-cad-text">Generate an assembly to see the bill of materials and strut layout.</div>
      ) : (
        <>
          <section>
            <h3 className="sec-head mb-2">Bill of Materials</h3>
            <div className="space-y-2">
              {assembly.nodes.map((node) => (
                <div key={node.id} className="p-2 rounded border border-cad-border bg-cad-bg/50 flex justify-between items-center">
                  <div>
                    <div className="text-xs text-cad-text-heading">{node.name}</div>
                    <div className="text-[10px] text-cad-text">{node.material}</div>
                  </div>
                  <div className="text-right">
                    <div className="text-xs text-cad-text-heading">×{node.quantity}</div>
                    <div className="text-[10px] text-cad-text">{node.print_time_minutes} min/ea</div>
                  </div>
                </div>
              ))}
            </div>
          </section>

          <section>
            <h3 className="sec-head mb-2">Strut Layout</h3>
            <div className="text-xs text-cad-text mb-2">{assembly.struts.length} structural struts connect the printed nodes.</div>
            <div className="grid grid-cols-2 gap-2">
              {assembly.struts.map((s) => (
                <div key={s.id} className="p-2 rounded border border-cad-border bg-cad-bg/50 text-[10px]">
                  <div className="text-cad-text-heading">{s.id}</div>
                  <div className="text-cad-text">⌀ {Math.round(s.diameter * 1000)} mm</div>
                </div>
              ))}
            </div>
          </section>

          <section>
            <h3 className="sec-head mb-2">Production Summary</h3>
            <div className="p-3 rounded border border-cad-border bg-cad-bg/50 text-xs space-y-2">
              <div className="flex justify-between">
                <span className="text-cad-text">Total printed parts</span>
                <span className="text-cad-text-heading">{assembly.nodes.reduce((s, n) => s + n.quantity, 0)}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-cad-text">Total print time</span>
                <span className="text-cad-text-heading">{totalPrintTime} min</span>
              </div>
              <div className="flex justify-between">
                <span className="text-cad-text">Detected kind</span>
                <span className="text-cad-text-heading capitalize">{assembly.kind.replace('_', ' ')}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-cad-text">Joining method</span>
                <span className="text-cad-text-heading">Structural adhesive bonding</span>
              </div>
            </div>
          </section>
        </>
      )}
    </div>
  )
}

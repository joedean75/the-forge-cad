import { useEffect, useState } from 'react'
import { Download, FileJson, FolderOpen } from 'lucide-react'
import { useAppStore } from '../../store/useAppStore'
import { api } from '../../api/client'
import type { Candidate, AssemblyResult } from '../../../../shared/types'

export function ExportPanel() {
  const { selectedCandidate, assembly, spec, simulation, setSelectedCandidate, setAssembly } = useAppStore()

  const [savedCandidates, setSavedCandidates] = useState<Candidate[]>([])
  const [savedAssemblies, setSavedAssemblies] = useState<AssemblyResult[]>([])

  useEffect(() => {
    api.listSavedCandidates().then(setSavedCandidates).catch(console.error)
    api.listSavedAssemblies().then(setSavedAssemblies).catch(console.error)
  }, [selectedCandidate, assembly])

  const downloadSTL = () => {
    if (!selectedCandidate) return
    const link = document.createElement('a')
    link.href = selectedCandidate.mesh_url
    link.download = `${spec.name.replace(/\s+/g, '_')}.stl`
    link.click()
  }

  const downloadSTEP = async () => {
    if (!selectedCandidate) return
    try {
      const stepText = await api.exportStep(selectedCandidate.id)
      const blob = new Blob([stepText], { type: 'application/step' })
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = `${spec.name.replace(/\s+/g, '_')}.step`
      link.click()
      URL.revokeObjectURL(url)
    } catch (err) {
      console.error('STEP export failed', err)
    }
  }

  const downloadAssemblySTEP = async () => {
    if (!assembly) return
    try {
      const stepText = await api.exportAssemblyStep(assembly.id)
      const blob = new Blob([stepText], { type: 'application/step' })
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = `${spec.name.replace(/\s+/g, '_')}_assembly.step`
      link.click()
      URL.revokeObjectURL(url)
    } catch (err) {
      console.error('Assembly STEP export failed', err)
    }
  }

  const downloadReport = () => {
    const report = {
      project: spec.name,
      material: spec.material_id,
      candidate: selectedCandidate,
      simulation,
      exported_at: new Date().toISOString(),
    }
    const blob = new Blob([JSON.stringify(report, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `${spec.name.replace(/\s+/g, '_')}_report.json`
    link.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div className="space-y-5">
      <section>
        <h3 className="sec-head mb-2">Export Geometry</h3>
        <div className="space-y-2">
          <button
            onClick={downloadSTL}
            disabled={!selectedCandidate}
            className="w-full flex items-center justify-center gap-2 px-3 py-2 rounded border border-cad-border bg-cad-bg/50 text-xs text-cad-text-heading hover:border-cad-accent disabled:opacity-40 disabled:cursor-not-allowed"
          >
            <Download size={14} />
            Download STL
          </button>
          <button
            onClick={downloadSTEP}
            disabled={!selectedCandidate?.cad}
            className="w-full flex items-center justify-center gap-2 px-3 py-2 rounded border border-cad-border bg-cad-bg/50 text-xs text-cad-text-heading hover:border-cad-accent disabled:opacity-40 disabled:cursor-not-allowed"
          >
            <Download size={14} />
            Download STEP
          </button>
          <button
            onClick={downloadAssemblySTEP}
            disabled={!assembly}
            className="w-full flex items-center justify-center gap-2 px-3 py-2 rounded border border-cad-border bg-cad-bg/50 text-xs text-cad-text-heading hover:border-cad-accent disabled:opacity-40 disabled:cursor-not-allowed"
          >
            <Download size={14} />
            Download Assembly STEP
          </button>
        </div>
      </section>

      <section>
        <h3 className="sec-head mb-2">Saved Designs</h3>
        <div className="space-y-3">
          <div>
            <div className="text-[10px] text-cad-text-heading mb-1">Parts ({savedCandidates.length})</div>
            <div className="space-y-1 max-h-28 overflow-auto">
              {savedCandidates.map((c) => (
                <button
                  key={c.id}
                  onClick={() => setSelectedCandidate(c)}
                  className="w-full text-left px-2 py-1 rounded border border-cad-border bg-cad-bg/50 text-[10px] text-cad-text hover:border-cad-accent flex items-center justify-between"
                >
                  <span className="truncate">{c.name}</span>
                  <FolderOpen size={10} />
                </button>
              ))}
              {savedCandidates.length === 0 && <div className="text-[10px] text-cad-text">No saved parts yet.</div>}
            </div>
          </div>

          <div>
            <div className="text-[10px] text-cad-text-heading mb-1">Assemblies ({savedAssemblies.length})</div>
            <div className="space-y-1 max-h-28 overflow-auto">
              {savedAssemblies.map((a) => (
                <button
                  key={a.id}
                  onClick={() => setAssembly(a)}
                  className="w-full text-left px-2 py-1 rounded border border-cad-border bg-cad-bg/50 text-[10px] text-cad-text hover:border-cad-accent flex items-center justify-between"
                >
                  <span className="truncate">{a.id.slice(0, 8)}… ({a.nodes.length} nodes)</span>
                  <FolderOpen size={10} />
                </button>
              ))}
              {savedAssemblies.length === 0 && <div className="text-[10px] text-cad-text">No saved assemblies yet.</div>}
            </div>
          </div>
        </div>
      </section>

      <section>
        <h3 className="sec-head mb-2">Export Report</h3>
        <div className="space-y-2">
          <button
            onClick={downloadReport}
            disabled={!selectedCandidate}
            className="w-full flex items-center justify-center gap-2 px-3 py-2 rounded border border-cad-border bg-cad-bg/50 text-xs text-cad-text-heading hover:border-cad-accent disabled:opacity-40 disabled:cursor-not-allowed"
          >
            <FileJson size={14} />
            Download JSON Report
          </button>
          <button
            disabled={!selectedCandidate}
            className="w-full flex items-center justify-center gap-2 px-3 py-2 rounded border border-cad-border bg-cad-bg/50 text-xs text-cad-text-heading hover:border-cad-accent disabled:opacity-40 disabled:cursor-not-allowed"
          >
            <FileJson size={14} />
            Download PDF Summary
          </button>
        </div>
      </section>

      <section>
        <h3 className="sec-head mb-2">Share</h3>
        <div className="p-3 rounded border border-cad-border bg-cad-bg/50 text-xs text-cad-text break-all">
          https://forge-design.local/share/{selectedCandidate?.id ?? '...'}
        </div>
      </section>
    </div>
  )
}

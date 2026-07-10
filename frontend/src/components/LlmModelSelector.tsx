import { useEffect, useState } from 'react'
import { Brain, Loader2 } from 'lucide-react'
import { api } from '../api/client'
import type { LlmConfig } from '../../../shared/types'

export function LlmModelSelector() {
  const [config, setConfig] = useState<LlmConfig | null>(null)
  const [loading, setLoading] = useState(false)
  const [updating, setUpdating] = useState(false)

  useEffect(() => {
    setLoading(true)
    api
      .getLlmConfig()
      .then(setConfig)
      .catch(console.error)
      .finally(() => setLoading(false))
  }, [])

  const changeModel = async (model: string) => {
    if (!config || model === config.model) return
    setUpdating(true)
    try {
      const updated = await api.setLlmModel(model)
      setConfig(updated)
    } catch (err) {
      console.error(err)
    } finally {
      setUpdating(false)
    }
  }

  if (loading) {
    return (
      <div className="flex items-center gap-2 text-[10px] text-cad-text">
        <Loader2 size={12} className="animate-spin" />
        Loading LLM config…
      </div>
    )
  }

  if (!config || !config.configured) {
    return (
      <div className="text-[10px] text-cad-text">
        No LLM key configured. Add <code className="text-cad-text-heading">LLM_API_KEY</code> to backend/.env.
      </div>
    )
  }

  return (
    <div className="flex items-center gap-2">
      <Brain size={12} className="text-cad-accent shrink-0" />
      <select
        value={config.model}
        onChange={(e) => changeModel(e.target.value)}
        disabled={updating}
        className="min-w-0 flex-1 px-2 py-1 rounded bg-cad-bg border border-cad-border text-cad-text-heading text-[10px] focus:border-cad-accent outline-none disabled:opacity-50"
        title={`Provider: ${config.provider}`}
      >
        {config.available_models.map((m) => (
          <option key={m} value={m}>
            {m}
          </option>
        ))}
      </select>
      {updating && <Loader2 size={12} className="animate-spin text-cad-text" />}
    </div>
  )
}

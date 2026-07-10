import type { ReactNode } from 'react'

interface PanelProps {
  title: string
  children: ReactNode
}

export function Panel({ title, children }: PanelProps) {
  return (
    <aside className="w-80 border-l border-cad-border bg-cad-panel flex flex-col shrink-0 z-10">
      <div className="h-11 border-b border-cad-border flex items-center gap-2.5 px-4">
        {/* datum tick — marks the active parameter set */}
        <span aria-hidden className="w-0.5 h-3.5 bg-cad-accent rounded-full" />
        <div className="leading-none">
          <span className="eyebrow text-cad-text/70">Inspector</span>
          <h2 className="font-display text-sm font-semibold text-cad-text-heading leading-none mt-1">{title}</h2>
        </div>
      </div>
      <div className="flex-1 overflow-y-auto p-4">{children}</div>
    </aside>
  )
}

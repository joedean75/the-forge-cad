import { useCallback, useEffect, useState } from 'react'
import { ChevronLeft, ChevronRight } from 'lucide-react'

/**
 * The gutter that collapses a side panel, and the state behind it.
 *
 * The editor is a feature tree, a viewport and an inspector at 288 and 320 px;
 * the assembly is a components list and an inspector. On a 1280 px screen that
 * is nearly half the width spent on panels, and the thing being modelled is in
 * the middle. `useNarrow` already answers the *small* screen — one column with a
 * switcher — but a wide screen has the opposite problem: three columns is right
 * most of the time and wrong exactly when you want to look at the part.
 *
 * Four decisions:
 *
 * - **The rail is always there, at both widths of the panel.** A control that
 *   only appears once the panel is hidden cannot be found before it is needed,
 *   and one that appears on hover is a control nobody knows about. Twenty
 *   pixels each side is the price of the affordance being discoverable, against
 *   the six hundred it gives back.
 * - **The panel is unmounted, not hidden.** Its state is in the stores, not in
 *   the component — the feature tree's selection, the inspector's mode, the
 *   assembly's tab all outlive it — so there is nothing to preserve, and
 *   `display: none` on a scrolled panel is a panel that comes back scrolled to
 *   somewhere nobody left it. What must *not* be unmounted is the viewport, and
 *   it is not: collapsing a sibling does not re-parent it, so the WebGL context
 *   and the camera survive (the same property `EditorScreen.Column` is built
 *   around).
 * - **The chevron points the way the panel will go**, not at the panel. A
 *   left-hand panel collapses leftwards, so its rail shows `‹` while open and
 *   `›` while closed — the arrow is the verb.
 * - **The choice is remembered per panel**, in `localStorage`, because it is a
 *   working preference and having to re-collapse on every reload is how a
 *   preference becomes an annoyance. Wrapped, because a browser with site data
 *   blocked throws on the accessor itself rather than returning null.
 */

const KEY = (id: string) => `forge.panel.${id}`

function stored(id: string): boolean {
  try {
    return window.localStorage.getItem(KEY(id)) === 'collapsed'
  } catch {
    return false
  }
}

/** Remembered collapse state for one panel: `[collapsed, toggle]`. */
export function usePanelCollapse(id: string): [boolean, () => void] {
  const [collapsed, setCollapsed] = useState(() => stored(id))
  useEffect(() => {
    try {
      window.localStorage.setItem(KEY(id), collapsed ? 'collapsed' : 'open')
    } catch {
      /* a preference that cannot be saved is still a preference for this session */
    }
  }, [id, collapsed])
  return [collapsed, useCallback(() => setCollapsed((v) => !v), [])]
}

export function PanelRail({ side, label, collapsed, onToggle }: {
  side: 'left' | 'right'
  /** Named in the tooltip and, when collapsed, down the rail itself. */
  label: string
  collapsed: boolean
  onToggle: () => void
}) {
  // Open: the panel is on the rail's `side`, so it collapses *towards* that
  // side. Closed: it comes back from there.
  const pointsLeft = side === 'left' ? !collapsed : collapsed
  const Chevron = pointsLeft ? ChevronLeft : ChevronRight
  return (
    <button
      type="button"
      onClick={onToggle}
      title={`${collapsed ? 'Show' : 'Hide'} ${label.toLowerCase()}`}
      aria-label={`${collapsed ? 'Show' : 'Hide'} ${label}`}
      aria-expanded={!collapsed}
      data-testid={`rail-${side}`}
      className={`group w-5 shrink-0 flex flex-col items-center justify-center gap-2
                  bg-cad-panel text-cad-text/50 hover:text-cad-accent-hot
                  hover:bg-cad-raised transition-colors z-10
                  ${side === 'left' ? 'border-r' : 'border-l'} border-cad-border`}
    >
      <Chevron size={13} className="shrink-0" />
      {/* The name only while collapsed: beside an open panel it would be a
          second, quieter title for something already titled. */}
      {collapsed && (
        <span
          className="eyebrow text-[9px] tracking-widest whitespace-nowrap select-none"
          style={{ writingMode: 'vertical-rl', transform: side === 'left' ? 'rotate(180deg)' : undefined }}
        >
          {label}
        </span>
      )}
    </button>
  )
}

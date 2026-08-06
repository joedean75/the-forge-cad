import { useEffect, useState } from 'react'

/** Below this the editor's three columns stop being three columns. */
export const NARROW_PX = 1024

/**
 * Whether the window is too narrow for the editor's three-column layout.
 *
 * The editor is a feature tree, a WebGL viewport and an inspector side by side,
 * and the two panels are 288 px and 320 px of dense readouts. Squeezing them
 * proportionally leaves a viewport a few hundred pixels wide with two unreadable
 * columns beside it — every one of the three ends up unusable to keep all three
 * on screen. So below `NARROW_PX` the answer is one column at a time with a
 * switcher, which is a layout rather than a compromise.
 *
 * `matchMedia` rather than a resize listener: it fires only when the answer
 * changes, so a drag across the breakpoint is one render instead of hundreds.
 */
export function useNarrow(px: number = NARROW_PX): boolean {
  const query = `(max-width: ${px - 1}px)`
  const [narrow, setNarrow] = useState(
    () => typeof window !== 'undefined' && window.matchMedia(query).matches,
  )
  useEffect(() => {
    if (typeof window === 'undefined') return
    const mql = window.matchMedia(query)
    const update = () => setNarrow(mql.matches)
    update()
    mql.addEventListener('change', update)
    return () => mql.removeEventListener('change', update)
  }, [query])
  return narrow
}

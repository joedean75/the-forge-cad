import type { View } from './PartBackdrop'
import { spanV } from './PartBackdrop'

/**
 * A ruled grid that means something at any zoom.
 *
 * The old grid was 10 mm, always, over a window that could not move — fine for
 * a 60 mm plate and useless either side of that. Spacing is now chosen from the
 * visible span so there are always roughly the same number of lines on screen,
 * and every fifth one is drawn heavier and labelled, so a distance can be read
 * off the canvas rather than measured.
 */

// Steps that a person reads without arithmetic. 1-2-5 per decade, which is the
// same ladder an oscilloscope or a chart axis uses, and for the same reason.
const STEPS = [0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000]

/** The finest step that keeps the visible line count sane. */
export function gridStep(span: number, target = 26): number {
  for (const step of STEPS) if (span / step <= target) return step
  return STEPS[STEPS.length - 1]
}

/** Format a grid label without trailing noise: 0.5, 12, 250. */
function label(value: number, step: number): string {
  const decimals = step < 0.1 ? 2 : step < 1 ? 1 : 0
  const text = value.toFixed(decimals)
  return text === '-0' ? '0' : text
}

export function SketchGrid({ view }: { view: View }) {
  const step = gridStep(view.span)
  const major = step * 5
  const k = view.span / 260          // screen-constant strokes and type

  const u0 = view.cx - view.span / 2
  const u1 = view.cx + view.span / 2
  const v0 = view.cy - spanV(view.span) / 2
  const v1 = view.cy + spanV(view.span) / 2

  const columns: number[] = []
  for (let u = Math.ceil(u0 / step) * step; u <= u1; u += step) columns.push(u)
  const rows: number[] = []
  for (let v = Math.ceil(v0 / step) * step; v <= v1; v += step) rows.push(v)

  const isMajor = (value: number) => Math.abs(value / major - Math.round(value / major)) < 1e-6

  // `userSelect` matters: without it, dragging across the canvas to pan or to
  // rubber-band highlights the grid figures like body text.
  return (
    <g pointerEvents="none" style={{ userSelect: 'none' }}>
      {columns.map((u) => (
        <line key={`c${u.toFixed(4)}`} x1={u} y1={-v0} x2={u} y2={-v1}
          stroke={isMajor(u) ? '#262c37' : '#1e232b'} strokeWidth={(isMajor(u) ? 0.5 : 0.25) * k} />
      ))}
      {rows.map((v) => (
        <line key={`r${v.toFixed(4)}`} x1={u0} y1={-v} x2={u1} y2={-v}
          stroke={isMajor(v) ? '#262c37' : '#1e232b'} strokeWidth={(isMajor(v) ? 0.5 : 0.25) * k} />
      ))}
      {/* Axes last so they sit over the grid. */}
      <line x1={u0} y1={0} x2={u1} y2={0} stroke="#39424f" strokeWidth={0.6 * k} />
      <line x1={0} y1={-v0} x2={0} y2={-v1} stroke="#39424f" strokeWidth={0.6 * k} />

      {/* Figures on the majors, pinned to the bottom-left of the view so they
          stay legible while panning rather than scrolling off with the grid. */}
      {columns.filter(isMajor).map((u) => (
        <text key={`cl${u.toFixed(4)}`} x={u} y={-v0 - 2.5 * k} fill="#4d5765"
          fontSize={3.4 * k} textAnchor="middle">{label(u, step)}</text>
      ))}
      {rows.filter(isMajor).map((v) => (
        <text key={`rl${v.toFixed(4)}`} x={u0 + 2 * k} y={-v} fill="#4d5765"
          fontSize={3.4 * k} dominantBaseline="middle">{label(v, step)}</text>
      ))}
    </g>
  )
}

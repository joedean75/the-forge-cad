/**
 * Whether a scrolling transcript is close enough to its end to follow new content.
 *
 * The copilot's transcript used to scroll to the bottom on every change, and a
 * running job changes it about once a second — so reading back through the
 * history was impossible: the panel yanked you to the end mid-sentence. Sticking
 * only when the reader is already at the end is the usual answer, and it needs a
 * tolerance rather than an equality: fractional device pixels and a smooth
 * scroll still in flight leave the numbers a little short of exact.
 */
export const NEAR_BOTTOM_PX = 64

export function isNearBottom(
  box: { scrollTop: number; scrollHeight: number; clientHeight: number },
  tolerance = NEAR_BOTTOM_PX,
): boolean {
  return box.scrollHeight - box.scrollTop - box.clientHeight <= tolerance
}

/**
 * Preparing a picture to go with a copilot goal.
 *
 * The image is sent to the planner on every turn, because the planner keeps no
 * memory between steps. So its size is paid once per step, not once per run,
 * and a 12 MP phone photo would also blow the server's 8 MB JSON body limit.
 * Downscaling here bounds both. A vision model reads a 1568 px image as well
 * as it reads the original.
 */

/** The longest side sent. Larger images are resized by the providers anyway. */
export const MAX_IMAGE_EDGE = 1568

/** How many images one goal may carry. Kept in step with `copilot.MAX_IMAGES`. */
export const MAX_IMAGES = 6

/** Decoded bytes across all of a goal's images. Kept in step with the server's
 *  `MAX_TOTAL_IMAGE_BYTES`, which exists to stay under the 8 MB JSON body limit. */
export const MAX_TOTAL_IMAGE_BYTES = 5_500_000

/** The size that fits within `max` on its longest side, keeping the aspect. */
export function fitWithin(width: number, height: number, max = MAX_IMAGE_EDGE): { width: number; height: number } {
  if (width <= 0 || height <= 0) return { width: 0, height: 0 }
  const scale = Math.min(1, max / Math.max(width, height))
  return { width: Math.max(1, Math.round(width * scale)), height: Math.max(1, Math.round(height * scale)) }
}

/** Decoded bytes a base64 data URI carries, without decoding it. */
export function dataUriBytes(uri: string): number {
  const comma = uri.indexOf(',')
  if (comma < 0) return 0
  const b64 = uri.slice(comma + 1)
  const padding = b64.endsWith('==') ? 2 : b64.endsWith('=') ? 1 : 0
  return Math.floor((b64.length * 3) / 4) - padding
}

/**
 * The file as a JPEG data URI no larger than `MAX_IMAGE_EDGE` on a side.
 *
 * JPEG because a photo is most of what gets attached, and PNG of a photo is
 * several times larger. A transparent PNG is painted onto white first, since
 * JPEG has no alpha and would turn the transparent area black.
 */
export async function prepareImage(file: File): Promise<string> {
  if (!file.type.startsWith('image/')) throw new Error('That file is not an image')
  const bitmap = await createImageBitmap(file)
  try {
    const { width, height } = fitWithin(bitmap.width, bitmap.height)
    const canvas = document.createElement('canvas')
    canvas.width = width
    canvas.height = height
    const ctx = canvas.getContext('2d')
    if (!ctx) throw new Error('This browser cannot resize images')
    ctx.fillStyle = '#ffffff'
    ctx.fillRect(0, 0, width, height)
    ctx.drawImage(bitmap, 0, 0, width, height)
    const uri = canvas.toDataURL('image/jpeg', 0.85)
    if (dataUriBytes(uri) > MAX_TOTAL_IMAGE_BYTES) throw new Error('That image is too large to send')
    return uri
  } finally {
    bitmap.close()
  }
}

import type { CopilotContext } from '../../../shared/types'

export type ViewCapture = {
  uri: string
  viewport: Omit<NonNullable<CopilotContext['viewport']>, 'image_index'>
}
const captures = new Map<string, () => ViewCapture>()

export function registerViewport(subject: string, id: string, capture: () => ViewCapture): () => void {
  const key = `${subject}:${id}`
  captures.set(key, capture)
  return () => { if (captures.get(key) === capture) captures.delete(key) }
}

export function captureViewport(subject: string, id: string): ViewCapture {
  const capture = captures.get(`${subject}:${id}`)
  if (!capture) throw new Error('Open the 3D viewport before attaching a view.')
  return capture()
}

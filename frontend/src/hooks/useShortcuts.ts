import { useEffect } from 'react'
import { useDocumentStore } from '../store/useDocumentStore'

/**
 * The editor's global keyboard layer.
 *
 * Four shortcuts existed — ⌘K, F, Esc, Delete — and undo was not one of them,
 * which is the odd part: since P3 the store restores `{tree, derived}`
 * together and the browser already holds the mesh, so undo is effectively
 * instant. The one thing it was missing is the key everybody presses.
 *
 * Two rules this has to keep:
 *
 * - **Never steal a key from a text field.** ⌘Z inside an input is the
 *   browser's, and so are the single letters. The existing handlers already
 *   check this; centralising them means checking it once.
 * - **Undo must await `historySyncSettled()`.** The store's write to the
 *   server runs in the background and only adopts its response if the tree it
 *   sent is still current. A held-down ⌘Z that skipped the await would land an
 *   undo *before* the edit it reverses. `undo()` does that await itself, so
 *   this must call it rather than reaching into the history arrays.
 */

export type Shortcut = { keys: string; label: string; group: string }

/** What the `?` sheet lists. Written here so the sheet cannot drift from the
 *  handler — a shortcut nobody can discover is a shortcut nobody has. */
export const SHORTCUTS: Shortcut[] = [
  { keys: '⌘K', label: 'Command palette', group: 'General' },
  { keys: '?', label: 'This list', group: 'General' },
  { keys: 'Esc', label: 'Cancel the current pick', group: 'General' },
  { keys: '⌘Z', label: 'Undo', group: 'Edit' },
  { keys: '⇧⌘Z', label: 'Redo', group: 'Edit' },
  { keys: 'Del', label: 'Delete the selected feature', group: 'Edit' },
  { keys: 'F', label: 'Fit the view to the part', group: 'View' },
  { keys: 'B', label: 'Box', group: 'Create' },
  { keys: 'C', label: 'Cylinder', group: 'Create' },
  { keys: 'S', label: 'New sketch', group: 'Create' },
  { keys: 'P', label: 'Datum plane', group: 'Create' },
]

/** True when the event came from somewhere the user is typing. */
export function isTyping(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null
  if (!el) return false
  return el.tagName === 'INPUT' || el.tagName === 'TEXTAREA'
    || el.tagName === 'SELECT' || el.isContentEditable
}

export function useShortcuts(handlers: {
  onHelp: () => void
  onCreate: (shape: 'box' | 'cylinder') => void
  onSketch: () => void
  onPlane: () => void
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (isTyping(e.target)) return
      const meta = e.metaKey || e.ctrlKey
      const key = e.key.toLowerCase()

      if (meta && key === 'z') {
        e.preventDefault()
        const store = useDocumentStore.getState()
        // `undo`/`redo` await the pending server write themselves.
        void (e.shiftKey ? store.redo() : store.undo())
        return
      }
      if (meta) return          // every other combination belongs to the browser

      if (e.key === '?') { e.preventDefault(); handlers.onHelp(); return }
      if (key === 'b') { handlers.onCreate('box'); return }
      if (key === 'c') { handlers.onCreate('cylinder'); return }
      if (key === 's') { handlers.onSketch(); return }
      if (key === 'p') { handlers.onPlane() }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [handlers])
}

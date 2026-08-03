import { lazy, Suspense, useEffect } from 'react'
import { Loader2 } from 'lucide-react'
import { useAuthStore } from '../store/useAuthStore'
import { useDocumentStore } from '../store/useDocumentStore'
import { useAssemblyStore } from '../store/useAssemblyStore'
import { useNavStore } from '../store/useNavStore'
import { SignInScreen } from '../screens/SignInScreen'

// Split at the screen boundary. Sign-in is the only screen a first visit is
// certain to reach, and it was paying for three.js, the slicer, the drawing
// viewer and the sketch canvas before it could draw a password field. It stays
// in the first chunk because it *is* the first paint; everything behind it
// loads when it is navigated to.
const LibraryScreen = lazy(() =>
  import('../screens/LibraryScreen').then((m) => ({ default: m.LibraryScreen })))
const EditorScreen = lazy(() =>
  import('../screens/EditorScreen').then((m) => ({ default: m.EditorScreen })))
const AssemblyScreen = lazy(() =>
  import('../screens/AssemblyScreen').then((m) => ({ default: m.AssemblyScreen })))

/** The same shell every loading state uses, so a lazy boundary reads as the app
 *  thinking rather than as a blank frame. */
export function ScreenFallback({ label = 'Loading…' }: { label?: string }) {
  return (
    <div className="h-full flex items-center justify-center bg-cad-bg text-cad-text gap-2 text-sm">
      <Loader2 size={16} className="animate-spin" /> {label}
    </div>
  )
}

/**
 * App shell / top-level router: sign-in → library → editor|assembly.
 * The rendered screen is gated by auth (never show app chrome signed-out) and by
 * whether the target document/assembly is actually loaded (so a stale hash falls
 * back to the library rather than an empty editor).
 */
export function Layout() {
  const { init, account, ready } = useAuthStore()
  const screen = useNavStore((s) => s.screen)
  const doc = useDocumentStore((s) => s.doc)
  const assembly = useAssemblyStore((s) => s.assembly)

  // Load the current session + plan catalog once on mount.
  useEffect(() => { init() }, [init])

  // Undo/redo shortcuts (guarded inside the store; no-op without a doc).
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return
      if (!(e.ctrlKey || e.metaKey)) return
      const k = e.key.toLowerCase()
      if (k === 'z' && !e.shiftKey) {
        e.preventDefault()
        useDocumentStore.getState().undo()
      } else if (k === 'y' || (k === 'z' && e.shiftKey)) {
        e.preventDefault()
        useDocumentStore.getState().redo()
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [])

  if (!ready) return <ScreenFallback label="Loading Forge…" />
  if (!account) return <SignInScreen />

  return (
    <Suspense fallback={<ScreenFallback label="Loading workspace…" />}>
      {screen === 'editor' && doc ? <EditorScreen />
        : screen === 'assembly' && assembly ? <AssemblyScreen />
        : <LibraryScreen />}
    </Suspense>
  )
}

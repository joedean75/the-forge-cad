import { useEffect } from 'react'
import { Loader2 } from 'lucide-react'
import { useAuthStore } from '../store/useAuthStore'
import { useDocumentStore } from '../store/useDocumentStore'
import { useAssemblyStore } from '../store/useAssemblyStore'
import { useNavStore } from '../store/useNavStore'
import { SignInScreen } from '../screens/SignInScreen'
import { LibraryScreen } from '../screens/LibraryScreen'
import { EditorScreen } from '../screens/EditorScreen'
import { AssemblyScreen } from '../screens/AssemblyScreen'

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

  if (!ready) {
    return (
      <div className="h-full flex items-center justify-center bg-cad-bg text-cad-text gap-2 text-sm">
        <Loader2 size={16} className="animate-spin" /> Loading DAPS…
      </div>
    )
  }

  if (!account) return <SignInScreen />
  if (screen === 'editor' && doc) return <EditorScreen />
  if (screen === 'assembly' && assembly) return <AssemblyScreen />
  return <LibraryScreen />
}

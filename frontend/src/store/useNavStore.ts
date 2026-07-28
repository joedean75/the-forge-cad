import { create } from 'zustand'

/**
 * Top-level screen router for the app shell: sign-in → library → editor/assembly.
 * Reflected in the URL hash so browser back/forward and refresh behave. Auth and
 * document state gate what's actually rendered (see Layout).
 */
export type Screen = 'signin' | 'library' | 'editor' | 'assembly'

const VALID: Screen[] = ['signin', 'library', 'editor', 'assembly']

function screenFromHash(): Screen | null {
  const h = window.location.hash.replace(/^#\/?/, '').split('/')[0]
  return (VALID as string[]).includes(h) ? (h as Screen) : null
}

/** Where to go back to, when a screen was opened from another one. */
export interface ReturnTo {
  screen: Screen
  /** What to call it in the back control — e.g. the assembly's name. */
  label: string
}

interface NavState {
  screen: Screen
  /** Set when the current screen was entered from elsewhere (assembly → editor). */
  returnTo: ReturnTo | null
  go: (screen: Screen) => void
  /** Navigate, remembering where to come back to. */
  goFrom: (screen: Screen, returnTo: ReturnTo) => void
  /** Follow the remembered return, if there is one. */
  goBack: () => void
  _sync: (screen: Screen) => void
}

export const useNavStore = create<NavState>((set, get) => ({
  screen: screenFromHash() ?? 'signin',
  returnTo: null,
  go: (screen) => {
    if (window.location.hash.replace(/^#\/?/, '') !== screen) {
      window.location.hash = `#${screen}`
    }
    // A plain navigation is a fresh start, so any pending return is stale.
    set({ screen, returnTo: null })
  },
  goFrom: (screen, returnTo) => {
    if (window.location.hash.replace(/^#\/?/, '') !== screen) {
      window.location.hash = `#${screen}`
    }
    set({ screen, returnTo })
  },
  goBack: () => {
    const back = get().returnTo
    if (back) get().go(back.screen)
  },
  _sync: (screen) => set({ screen }),
}))

// Keep the store in sync with browser back/forward.
if (typeof window !== 'undefined') {
  window.addEventListener('hashchange', () => {
    const s = screenFromHash()
    if (s) useNavStore.getState()._sync(s)
  })
}

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

interface NavState {
  screen: Screen
  go: (screen: Screen) => void
  _sync: (screen: Screen) => void
}

export const useNavStore = create<NavState>((set) => ({
  screen: screenFromHash() ?? 'signin',
  go: (screen) => {
    if (window.location.hash.replace(/^#\/?/, '') !== screen) {
      window.location.hash = `#${screen}`
    }
    set({ screen })
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

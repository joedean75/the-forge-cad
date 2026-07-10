import { create } from 'zustand'
import { api, setAuthToken, getAuthToken, type AccountView, type PlanSummary } from '../api/client'

interface AuthState {
  account: AccountView | null
  plans: PlanSummary[]
  /** True when a real payment provider (Stripe) is wired; false = local self-serve. */
  billingLive: boolean
  loading: boolean
  error: string | null
  /** Load the current session (if a token is stored) + the public plan catalog. */
  init: () => Promise<void>
  signup: (email: string, password: string) => Promise<void>
  login: (email: string, password: string) => Promise<void>
  logout: () => void
  subscribe: (planId: string) => Promise<string | null>
  openBillingPortal: () => Promise<void>
  createApiKey: (name: string) => Promise<string>
  revokeApiKey: (keyId: string) => Promise<void>
  refresh: () => Promise<void>
}

export const useAuthStore = create<AuthState>((set, get) => ({
  account: null,
  plans: [],
  billingLive: false,
  loading: false,
  error: null,

  init: async () => {
    try {
      const { plans, billing } = await api.listPlans()
      set({ plans, billingLive: billing.live })
    } catch {
      /* plans are non-critical */
    }
    if (getAuthToken()) {
      try {
        set({ account: await api.me() })
      } catch {
        setAuthToken(null)
        set({ account: null })
      }
    }
  },

  signup: async (email, password) => {
    set({ loading: true, error: null })
    try {
      const { token, account } = await api.signup(email, password)
      setAuthToken(token)
      set({ account, loading: false })
    } catch (e) {
      set({ error: e instanceof Error ? e.message : 'Signup failed', loading: false })
      throw e
    }
  },

  login: async (email, password) => {
    set({ loading: true, error: null })
    try {
      const { token, account } = await api.login(email, password)
      setAuthToken(token)
      set({ account, loading: false })
    } catch (e) {
      set({ error: e instanceof Error ? e.message : 'Login failed', loading: false })
      throw e
    }
  },

  logout: () => {
    setAuthToken(null)
    set({ account: null })
  },

  subscribe: async (planId) => {
    const res = await api.subscribe(planId)
    if (res.url) return res.url // hosted checkout (Stripe) — caller redirects
    await get().refresh()
    return null
  },

  openBillingPortal: async () => {
    const { url } = await api.billingPortal()
    if (url) window.location.href = url
  },

  createApiKey: async (name) => {
    const res = await api.createApiKey(name)
    set({ account: res.account })
    return res.api_key
  },

  revokeApiKey: async (keyId) => {
    const res = await api.revokeApiKey(keyId)
    set({ account: res.account })
  },

  refresh: async () => {
    try {
      set({ account: await api.me() })
    } catch {
      /* ignore */
    }
  },
}))

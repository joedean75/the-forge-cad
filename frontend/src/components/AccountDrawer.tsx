import { useEffect, useState } from 'react'
import { X, Check, KeyRound, Loader2, LogOut, Copy, Trash2, Zap, CreditCard } from 'lucide-react'
import { useAuthStore } from '../store/useAuthStore'

export function AccountDrawer({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { account, plans, billingLive, signup, login, logout, subscribe, openBillingPortal, createApiKey, revokeApiKey, refresh, error } = useAuthStore()
  const [mode, setMode] = useState<'login' | 'signup'>('login')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [newKey, setNewKey] = useState<string | null>(null)
  const [keyName, setKeyName] = useState('default')

  useEffect(() => {
    if (!open) { setNewKey(null); setBusy(false) }
  }, [open])

  // Returning from Stripe Checkout: the webhook has (or will shortly) grant the
  // plan — refresh the account and strip the query so a reload doesn't re-trigger.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    if (params.get('checkout') === 'success') {
      refresh()
      params.delete('checkout'); params.delete('plan')
      const qs = params.toString()
      window.history.replaceState({}, '', window.location.pathname + (qs ? `?${qs}` : ''))
    }
  }, [refresh])

  if (!open) return null

  const submit = async () => {
    setBusy(true)
    try {
      if (mode === 'signup') await signup(email, password)
      else await login(email, password)
    } catch {
      /* error surfaced from store */
    } finally {
      setBusy(false)
    }
  }

  const changePlan = async (planId: string) => {
    setBusy(true)
    try {
      const url = await subscribe(planId)
      if (url) window.location.href = url // hosted checkout
    } finally {
      setBusy(false)
    }
  }

  const usage = account?.usage
  const pct = usage ? Math.min(100, (usage.ops_used / Math.max(usage.monthly_op_quota, 1)) * 100) : 0

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-black/40" onClick={onClose}>
      <aside
        className="w-[420px] max-w-full h-full bg-cad-panel border-l border-cad-border flex flex-col overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="h-12 border-b border-cad-border flex items-center justify-between px-4 shrink-0">
          <div className="flex items-center gap-2.5">
            <span aria-hidden className="w-0.5 h-3.5 bg-cad-accent rounded-full" />
            <h2 className="font-display text-sm font-semibold text-cad-text-heading">
              {account ? 'Account & Subscription' : 'Sign in to DAPS'}
            </h2>
          </div>
          <button onClick={onClose} className="p-1.5 rounded-sm text-cad-text hover:text-cad-text-heading hover:bg-cad-raised"><X size={16} /></button>
        </div>

        {!account ? (
          <div className="p-5 space-y-3">
            <div className="flex items-center gap-1 p-0.5 rounded-sm bg-cad-bg border border-cad-border">
              {(['login', 'signup'] as const).map((m) => (
                <button key={m} onClick={() => setMode(m)}
                  className={`flex-1 py-1.5 rounded-[3px] text-[11px] font-medium ${mode === m ? 'bg-cad-accent/15 text-cad-accent-hot' : 'text-cad-text'}`}>
                  {m === 'login' ? 'Log in' : 'Create account'}
                </button>
              ))}
            </div>
            <input value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@company.com" type="email"
              className="w-full px-3 py-2 rounded-sm bg-cad-bg border border-cad-border text-cad-text-heading text-xs outline-none focus:border-cad-accent" />
            <input value={password} onChange={(e) => setPassword(e.target.value)} placeholder="password (≥ 8 chars)" type="password"
              onKeyDown={(e) => e.key === 'Enter' && submit()}
              className="w-full px-3 py-2 rounded-sm bg-cad-bg border border-cad-border text-cad-text-heading text-xs outline-none focus:border-cad-accent" />
            {error && <p className="text-[11px] text-cad-danger">{error}</p>}
            <button onClick={submit} disabled={busy || !email || !password}
              className="w-full flex items-center justify-center gap-1.5 py-2 rounded-sm bg-cad-accent text-cad-bg font-semibold text-[11px] hover:bg-cad-accent-hot disabled:opacity-40">
              {busy ? <Loader2 size={13} className="animate-spin" /> : null}
              {mode === 'login' ? 'Log in' : 'Create account'}
            </button>
          </div>
        ) : (
          <div className="p-5 space-y-5">
            {/* identity + plan */}
            <div>
              <p className="text-xs text-cad-text-heading font-medium">{account.email}</p>
              <p className="eyebrow text-cad-text/70 mt-0.5">{account.plan.name} plan</p>
              {billingLive && account.plan.id !== 'free' && (
                <button
                  onClick={async () => { setBusy(true); try { await openBillingPortal() } catch { /* surfaced */ } finally { setBusy(false) } }}
                  disabled={busy}
                  className="mt-2 flex items-center gap-1.5 px-2 py-1 rounded-sm border border-cad-border text-cad-text hover:text-cad-text-heading text-[10px] disabled:opacity-40"
                >
                  <CreditCard size={11} /> Manage billing
                </button>
              )}
            </div>

            {/* usage meter */}
            {usage && (
              <div>
                <div className="flex items-center justify-between mb-1">
                  <span className="eyebrow text-cad-text/70">Operations this period</span>
                  <span className="tabular text-[10px] text-cad-text">{usage.ops_used} / {usage.monthly_op_quota}</span>
                </div>
                <div className="h-1.5 rounded-full bg-cad-bg overflow-hidden">
                  <div className="h-full bg-cad-accent" style={{ width: `${pct}%` }} />
                </div>
              </div>
            )}

            {/* plans */}
            <div className="space-y-2">
              <span className="eyebrow text-cad-text/70">Plans</span>
              {plans.map((p) => {
                const current = p.id === account.plan.id
                return (
                  <div key={p.id} className={`rounded-sm border p-3 ${current ? 'border-cad-accent/60 bg-cad-accent/5' : 'border-cad-border'}`}>
                    <div className="flex items-center justify-between">
                      <div>
                        <span className="text-xs font-medium text-cad-text-heading">{p.name}</span>
                        <span className="tabular text-[10px] text-cad-text/70 ml-2">${p.price_usd_month}/mo</span>
                      </div>
                      {current ? (
                        <span className="flex items-center gap-1 text-[10px] text-cad-accent-hot"><Check size={12} /> Current</span>
                      ) : (
                        <button onClick={() => changePlan(p.id)} disabled={busy}
                          className="flex items-center gap-1 px-2 py-1 rounded-sm bg-cad-accent text-cad-bg text-[10px] font-semibold hover:bg-cad-accent-hot disabled:opacity-40">
                          <Zap size={11} /> {p.price_usd_month > account_price(plans, account.plan.id) ? 'Upgrade' : 'Switch'}
                        </button>
                      )}
                    </div>
                    <p className="tabular text-[10px] text-cad-text/60 mt-1.5">
                      {p.monthly_op_quota.toLocaleString()} ops · res ≤ {p.resolution_cap} · {p.features.length ? p.features.join(', ') : 'core modeling'}
                    </p>
                  </div>
                )
              })}
            </div>

            {/* API keys */}
            <div className="space-y-2">
              <span className="eyebrow text-cad-text/70 flex items-center gap-1"><KeyRound size={11} /> API keys (agent access)</span>
              {newKey && (
                <div className="rounded-sm border border-cad-accent/50 bg-cad-accent/5 p-2">
                  <p className="text-[10px] text-cad-text/80 mb-1">Copy now — shown once:</p>
                  <div className="flex items-center gap-1">
                    <code className="tabular flex-1 text-[10px] text-cad-accent-hot truncate">{newKey}</code>
                    <button onClick={() => navigator.clipboard?.writeText(newKey)} className="text-cad-text hover:text-cad-text-heading"><Copy size={12} /></button>
                  </div>
                </div>
              )}
              {account.api_keys.map((k) => (
                <div key={k.id} className="flex items-center justify-between text-[10px] text-cad-text">
                  <span className="tabular">{k.name} · {k.prefix}…</span>
                  <button onClick={() => revokeApiKey(k.id)} className="text-cad-text hover:text-cad-danger"><Trash2 size={11} /></button>
                </div>
              ))}
              <div className="flex items-center gap-1.5">
                <input value={keyName} onChange={(e) => setKeyName(e.target.value)} placeholder="key name"
                  className="flex-1 px-2 py-1 rounded-sm bg-cad-bg border border-cad-border text-cad-text-heading text-[10px] outline-none" />
                <button
                  onClick={async () => { setBusy(true); try { setNewKey(await createApiKey(keyName)) } catch { /* gated */ } finally { setBusy(false) } }}
                  disabled={busy}
                  className="px-2 py-1 rounded-sm border border-cad-border text-cad-text hover:text-cad-accent-hot text-[10px] disabled:opacity-40">
                  Create
                </button>
              </div>
              {!account.plan.features.includes('api_keys') && (
                <p className="text-[10px] text-cad-text/50">API keys require the Pro plan or above.</p>
              )}
            </div>

            <button onClick={logout} className="w-full flex items-center justify-center gap-1.5 py-2 rounded-sm border border-cad-border text-cad-text hover:text-cad-danger text-[11px]">
              <LogOut size={13} /> Log out
            </button>
          </div>
        )}
      </aside>
    </div>
  )
}

function account_price(plans: { id: string; price_usd_month: number }[], planId: string): number {
  return plans.find((p) => p.id === planId)?.price_usd_month ?? 0
}

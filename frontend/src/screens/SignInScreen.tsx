import { useEffect, useState } from 'react'
import { Loader2, ArrowLeft, ArrowRight } from 'lucide-react'
import { useAuthStore } from '../store/useAuthStore'
import { useNavStore } from '../store/useNavStore'

/** Pull ?oauth_code / ?oauth_error out of the URL (and scrub them from history). */
function takeOAuthParams(): { code: string | null; error: string | null } {
  const params = new URLSearchParams(window.location.search)
  const code = params.get('oauth_code')
  const error = params.get('oauth_error')
  if (code || error) {
    window.history.replaceState(null, '', window.location.pathname + window.location.hash)
  }
  return { code, error }
}

function TrussMark() {
  return (
    <svg width="34" height="34" viewBox="0 0 24 24" fill="none" aria-hidden>
      <path d="M4 19 L12 6 L20 19 M4 19 L20 19 M12 6 L12 19" stroke="var(--color-stress-void)" strokeWidth="1.25" strokeLinejoin="round" />
      <circle cx="4" cy="19" r="1.8" fill="var(--color-cad-text)" />
      <circle cx="20" cy="19" r="1.8" fill="var(--color-cad-text)" />
      <circle cx="12" cy="6" r="2.4" fill="var(--color-cad-accent)" />
    </svg>
  )
}

export function SignInScreen() {
  const { login, signup, completeOAuth, oauthProviders, error, loading } = useAuthStore()
  const go = useNavStore((s) => s.go)
  const [mode, setMode] = useState<'login' | 'signup'>('login')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [oauthError, setOauthError] = useState<string | null>(null)

  // Returning from an OAuth provider: redeem the one-time code for a session.
  useEffect(() => {
    const { code, error: cbError } = takeOAuthParams()
    if (cbError) setOauthError(cbError)
    if (code) {
      completeOAuth(code)
        .then(() => go('library'))
        .catch(() => {
          /* error surfaced from the store */
        })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const submit = async () => {
    if (!email || !password || loading) return
    try {
      if (mode === 'signup') await signup(email, password)
      else await login(email, password)
      go('library') // Layout also gates on auth; this keeps the URL tidy.
    } catch {
      /* error surfaced from the store */
    }
  }

  return (
    <div
      className="h-full w-full flex items-center justify-center p-6"
      style={{ background: 'radial-gradient(130% 130% at 50% 15%, #1a1f27 0%, var(--color-cad-bg) 55%, #0e1116 100%)' }}
    >
      <div className="w-full max-w-sm">
        <a href="/landing" className="flex items-center gap-2 text-cad-text hover:text-cad-text-heading text-[11px] mb-6 transition-colors w-fit">
          <ArrowLeft size={13} /> Back to home
        </a>

        <div className="flex items-center gap-3 mb-6">
          <TrussMark />
          <div className="leading-none">
            <h1 className="font-display text-cad-text-heading font-semibold text-xl tracking-tight">The Forge</h1>
            <p className="eyebrow text-cad-text mt-1.5">Generative CAD · CAM</p>
          </div>
        </div>

        <div className="bg-cad-panel border border-cad-border rounded-md p-6 shadow-2xl">
          <h2 className="font-display text-base font-semibold text-cad-text-heading text-balance">
            {mode === 'login' ? 'Sign in to your studio' : 'Create your studio'}
          </h2>
          <p className="text-[11px] text-cad-text/70 mt-1 mb-5">
            {mode === 'login' ? 'Access your parts and assemblies.' : 'Start designing in seconds — no card required.'}
          </p>

          <div className="flex items-center gap-1 p-0.5 rounded-sm bg-cad-bg border border-cad-border mb-4">
            {(['login', 'signup'] as const).map((m) => (
              <button
                key={m}
                onClick={() => setMode(m)}
                className={`flex-1 py-1.5 rounded-[3px] text-[11px] font-medium transition-colors ${
                  mode === m ? 'bg-cad-accent/15 text-cad-accent-hot' : 'text-cad-text hover:text-cad-text-heading'
                }`}
              >
                {m === 'login' ? 'Log in' : 'Create account'}
              </button>
            ))}
          </div>

          <div className="space-y-2.5">
            <div>
              <label className="eyebrow text-cad-text/60 block mb-1">Email</label>
              <input
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                type="email"
                autoComplete="email"
                placeholder="you@company.com"
                className="w-full px-3 py-2 rounded-sm bg-cad-bg border border-cad-border text-cad-text-heading text-xs outline-none focus:border-cad-accent transition-colors"
              />
            </div>
            <div>
              <label className="eyebrow text-cad-text/60 block mb-1">Password</label>
              <input
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && submit()}
                type="password"
                autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
                placeholder="at least 8 characters"
                className="w-full px-3 py-2 rounded-sm bg-cad-bg border border-cad-border text-cad-text-heading text-xs outline-none focus:border-cad-accent transition-colors"
              />
            </div>

            {(error || oauthError) && <p className="text-[11px] text-cad-danger">{error ?? oauthError}</p>}

            <button
              onClick={submit}
              disabled={loading || !email || !password}
              className="w-full flex items-center justify-center gap-1.5 py-2 mt-1 rounded-sm bg-cad-accent text-cad-bg font-semibold text-[11px] hover:bg-cad-accent-hot transition-colors disabled:opacity-40"
            >
              {loading ? <Loader2 size={13} className="animate-spin" /> : null}
              {mode === 'login' ? 'Sign in' : 'Create account'}
              {!loading && <ArrowRight size={13} />}
            </button>

            {oauthProviders.length > 0 && (
              <>
                <div className="flex items-center gap-2 pt-1">
                  <span className="flex-1 h-px bg-cad-border" />
                  <span className="text-[10px] text-cad-text/50">or</span>
                  <span className="flex-1 h-px bg-cad-border" />
                </div>
                {oauthProviders.map((p) => (
                  <button
                    key={p.id}
                    disabled={loading}
                    onClick={() => {
                      // Full-page navigation: the server 302s to the provider
                      // with state + PKCE minted per attempt.
                      window.location.href = `/api/auth/oauth/${p.id}/start`
                    }}
                    className="w-full py-2 rounded-sm border border-cad-border text-cad-text hover:text-cad-text-heading hover:border-cad-accent/50 text-[11px] font-medium transition-colors disabled:opacity-40"
                  >
                    Continue with {p.name}
                  </button>
                ))}
              </>
            )}
          </div>
        </div>

        <p className="text-[10px] text-cad-text/50 text-center mt-4">
          {mode === 'login' ? "New to Forge? " : 'Already have an account? '}
          <button onClick={() => setMode(mode === 'login' ? 'signup' : 'login')} className="text-cad-accent-hot hover:underline">
            {mode === 'login' ? 'Create an account' : 'Sign in'}
          </button>
        </p>
      </div>
    </div>
  )
}

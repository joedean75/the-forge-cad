import { AlertTriangle, X } from 'lucide-react'
import { useDocumentStore } from '../store/useDocumentStore'

/**
 * The last operation failure, said in full.
 *
 * Every failure this API produces already carries a `code` and a `remedy` —
 * what to do about it, generated per code so no handler has to remember.
 * Until this existed all of it went to one truncated span in the 32 px status
 * bar, and the structured ones did not even get that far: `detail` is an
 * object, and interpolating one into a template literal produced
 * `API error: 422 [object Object]`.
 *
 * Three decisions:
 *
 * - **It does not auto-dismiss.** The remedy is the useful half and it is the
 *   longer half; a toast that fades takes it away mid-sentence. Dismissal is
 *   the reader's, and the next operation clears it anyway.
 * - **It is not the status bar.** The status bar answers "is something wrong"
 *   at a glance and is also lit by `derived.error`, which is a *warning* about
 *   a tree that does not currently evaluate — a supported state, not a
 *   failure. Two questions, two surfaces.
 * - **The code is shown, not hidden.** `PRECONDITION` versus `BAD_ARG` is the
 *   difference between "fix the number" and "do a different step first", and
 *   it is the same vocabulary the copilot and the docs use.
 */
export function FailureNotice() {
  const failure = useDocumentStore((s) => s.failure)
  const dismiss = useDocumentStore((s) => s.dismissFailure)
  if (!failure) return null

  return (
    <div
      role="alert"
      className="absolute left-1/2 -translate-x-1/2 bottom-4 z-30 w-[min(34rem,calc(100%-2rem))]
                 rounded-md border border-cad-danger/50 bg-cad-panel/95 backdrop-blur-sm shadow-lg
                 px-3.5 py-3 flex items-start gap-2.5"
    >
      <AlertTriangle size={15} className="text-cad-danger shrink-0 mt-px" />
      <div className="min-w-0 flex-1 space-y-1.5">
        <div className="flex items-center gap-2">
          <span className="tabular text-[10px] text-cad-danger uppercase tracking-wide">
            {failure.code ?? 'error'}
          </span>
          <span className="tabular text-[10px] text-cad-text/50">{failure.op}</span>
          {failure.arg && (
            <span className="tabular text-[10px] text-cad-text/50">· {failure.arg}</span>
          )}
        </div>
        <p className="text-[12px] text-cad-text-heading leading-snug">{failure.message}</p>
        {failure.remedy && (
          <p className="text-[11px] text-cad-text leading-snug border-l-2 border-cad-border pl-2">
            {failure.remedy}
          </p>
        )}
      </div>
      <button
        onClick={dismiss}
        title="Dismiss"
        className="shrink-0 p-0.5 rounded-sm text-cad-text hover:text-cad-text-heading transition"
      >
        <X size={13} />
      </button>
    </div>
  )
}

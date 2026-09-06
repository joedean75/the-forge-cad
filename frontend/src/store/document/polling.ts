/**
 * One polling loop, with backoff, for every job the UI waits on.
 *
 * `runOptimize` had its own `while (status !== 'done') { sleep(500) }`, and the
 * release-package panel had another. Two problems with that shape, and both get
 * worse as more work moves off the request path:
 *
 * - **A fixed 500 ms interval is wrong at both ends.** A solve that finishes in
 *   two seconds is polled four times; one that takes four minutes is polled 480
 *   times, all but the last of which learn nothing. Backing off from a quick
 *   first check toward a ceiling costs a little latency on the long jobs and
 *   removes almost all of the requests.
 * - **A loop with no stop condition other than the terminal status never
 *   stops.** A job that vanishes — the API restarted, the id aged out of a
 *   process-local dict — left the panel spinning and the browser polling for as
 *   long as the tab stayed open. This gives up, and says so, which is a state a
 *   person can act on.
 */

export interface PollOptions {
  /** First gap, in ms. Short: most jobs finish quickly and feel instant. */
  initialMs?: number
  /** Ceiling for the gap. Long enough that a four-minute job is cheap. */
  maxMs?: number
  /** How much each gap grows. 1.6 reaches the ceiling in about eight polls. */
  factor?: number
  /** Give up after this long in total, so a lost job cannot poll forever. */
  timeoutMs?: number
  /** Consecutive transport failures tolerated before giving up. */
  tolerateErrors?: number
}

const DEFAULTS: Required<PollOptions> = {
  initialMs: 350,
  maxMs: 4000,
  factor: 1.6,
  timeoutMs: 20 * 60 * 1000,
  tolerateErrors: 3,
}

export class JobLostError extends Error {}

/**
 * Poll `read` until `done`, reporting each state to `onState`.
 *
 * A transient failure is tolerated a few times — a job status endpoint 502ing
 * once is not a reason to abandon a four-minute solve — and then surfaces as
 * itself. Anything permanent (a 404 for a job the server no longer knows about)
 * arrives on the first read and is not worth three retries, but telling the two
 * apart from here needs status codes the client does not surface uniformly, so
 * the count is the pragmatic answer.
 */
export async function pollJob<T>(
  read: () => Promise<T>,
  done: (state: T) => boolean,
  onState?: (state: T) => void,
  options: PollOptions = {},
): Promise<T> {
  const { initialMs, maxMs, factor, timeoutMs, tolerateErrors } = { ...DEFAULTS, ...options }
  const deadline = Date.now() + timeoutMs
  let gap = initialMs
  let failures = 0
  let last: T | null = null

  for (;;) {
    await sleep(gap)
    try {
      last = await read()
      failures = 0
      onState?.(last)
      if (done(last)) return last
    } catch (e) {
      failures += 1
      if (failures > tolerateErrors) {
        throw new JobLostError(
          e instanceof Error
            ? `lost track of the job: ${e.message}`
            : 'lost track of the job')
      }
    }
    if (Date.now() > deadline) {
      throw new JobLostError('the job did not finish in time; it may still be running')
    }
    gap = Math.min(maxMs, Math.round(gap * factor))
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

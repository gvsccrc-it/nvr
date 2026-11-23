/**
 * How often the interface asks the server for things in the background (#536).
 *
 * One open tab made 40 requests a minute standing idle, and more on Live; every poll asked at its normal rate while the server was failing
 * (two attempts each, with the retry), and when it came back every tab's requests landed together. Now:
 *
 * - a poll that is failing waits at least twice its normal interval, doubling with each failure up to ``MAX_FAILING_MS``, spread at random so
 *   tabs do not all ask at the same instant: never faster than normal, and slower the longer the server fails;
 * - a feature that is switched off (the multi-site console, failover) is looked at again only every ``OFF_RECHECK_MS``, in case it is turned on;
 * - a retry waits a random while too.
 *
 * Polls stop while the page is hidden (React Query's default; the pop-in poll follows it too), except on the video wall.
 */
export const OFF_RECHECK_MS = 5 * 60_000;
export const MAX_FAILING_MS = 60_000;

/** The wait before the next poll after ``failures`` failures in a row: at least twice ``normalMs``, at most ``MAX_FAILING_MS`` (or the normal
 * interval if that is longer), spread by a quarter either way. */
export function failingDelay(normalMs: number, failures: number, random: () => number = Math.random): number {
  if (failures <= 0) return normalMs;
  const doubled = normalMs * 2 ** Math.min(Math.max(1, failures), 10);
  const ceiling = Math.max(MAX_FAILING_MS, normalMs);
  const spread = 0.75 + 0.5 * Math.min(Math.max(random(), 0), 1);
  return Math.max(normalMs * 2, Math.round(Math.min(doubled, ceiling) * spread));
}

interface PollingQuery { state: { status: string; fetchFailureCount: number } }

/** A React Query ``refetchInterval``: ``normalMs`` while answers come, slowing as above while they do not. */
export function pollEvery(normalMs: number) {
  return (query: PollingQuery): number => (query.state.status === "error" || query.state.fetchFailureCount > 0
    ? failingDelay(normalMs, Math.max(1, query.state.fetchFailureCount))
    : normalMs);
}

/** A React Query ``retryDelay``: about a second, then two, spread at random so tabs do not retry together. */
export function retryDelay(attempt: number, random: () => number = Math.random): number {
  return Math.round(Math.min(30_000, 1_000 * 2 ** attempt) * (0.5 + random()));
}

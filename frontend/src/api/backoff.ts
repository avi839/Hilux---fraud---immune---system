/** Reconnect backoff (Req 22.12): 1 s after the first failure, doubling, capped at 30 s. */
export const BACKOFF_BASE_MS = 1_000;
export const BACKOFF_MAX_MS = 30_000;

/** Delay before reconnect attempt number `attempt` (0-based): min(30 s, 1 s · 2^attempt). */
export function backoffDelay(attempt: number): number {
  if (!Number.isFinite(attempt) || attempt < 0) return BACKOFF_BASE_MS;
  // 2 ** 15 already exceeds the cap; clamping the exponent avoids Infinity for huge attempts.
  const exp = Math.min(Math.floor(attempt), 15);
  return Math.min(BACKOFF_MAX_MS, BACKOFF_BASE_MS * 2 ** exp);
}

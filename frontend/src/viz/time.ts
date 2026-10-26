// Timestamp helpers shared by the network, map and replay views. Wire timestamps are ISO 8601
// with offset; JS Date keeps millisecond precision, so comparisons are done in epoch ms.

/** Epoch ms of an ISO timestamp, NaN when it does not parse. */
export function toMs(iso: string | null | undefined): number {
  return iso ? Date.parse(iso) : NaN;
}

/** True when ``iso`` is at or before ``t`` (null ``t`` = no replay filter). Unparseable
 *  timestamps are kept only when there is no filter. */
export function atOrBefore(iso: string, t: number | null): boolean {
  if (t === null) return true;
  const ms = toMs(iso);
  return Number.isFinite(ms) && ms <= t;
}

/** "YYYY-MM-DD HH:MM:SS UTC" for a replay position. */
export function formatUtc(ms: number): string {
  if (!Number.isFinite(ms)) return "n/a";
  const s = new Date(ms).toISOString();
  return `${s.slice(0, 10)} ${s.slice(11, 19)} UTC`;
}

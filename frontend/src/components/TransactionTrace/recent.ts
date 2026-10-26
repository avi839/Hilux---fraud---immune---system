// Which recent Transaction_Events the trace view offers as quick-pick chips, and plain-language
// hints for the sensors' vacuous reasons.

import type { FeedItem } from "../../store/reducers";

export const RECENT_CHIPS = 8;
/** Risk at or above which a transaction is "interesting" even without a Case. */
export const INTERESTING_RISK = 0.6;
/** Account history a behavioural sensor needs before it gives evidence (helix sensors' min_history). */
export const MIN_HISTORY = 5;

const interesting = (f: FeedItem) => f.case_id !== null || f.risk >= INTERESTING_RISK;

/** Up to ``n`` recent transactions, one per tx id, favouring the interesting ones: first those
 *  in a Case or with risk ≥ 0.6, then by highest risk, then newest. */
export function pickRecent(feed: readonly FeedItem[], n = RECENT_CHIPS): FeedItem[] {
  const seen = new Set<string>();
  const unique = feed.filter((f) => (seen.has(f.tx_id) ? false : (seen.add(f.tx_id), true)));
  return unique
    .slice()
    .sort((a, b) => Number(interesting(b)) - Number(interesting(a)) || b.risk - a.risk || b.seq - a.seq)
    .slice(0, n);
}

/** Plain-language hint for a sensor's vacuous reason, or null for an unknown one. */
export function vacuousHint(reason: string): string | null {
  const h = /^history:(\d+)$/.exec(reason);
  if (h) return `not enough history yet (needs ${MIN_HISTORY} events)`;
  if (reason.startsWith("missing:")) {
    const fields = reason.slice("missing:".length);
    return fields === "lat,lon" || fields === "lon,lat" ? "no location in this event" : `this event has no ${fields.replace(/,/g, ", ")}`;
  }
  return null;
}

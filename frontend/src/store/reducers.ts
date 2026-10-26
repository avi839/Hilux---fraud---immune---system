// Pure state transitions of the dashboard store, kept free of React/Zustand so they are
// directly unit-testable.

import type { CaseSummary, ScoredEventMessage, Tier } from "../api/types";

export const FEED_CAP = 200;

export interface FeedItem {
  key: string;
  seq: number;
  tx_id: string;
  ts: string;
  src: string;
  dst: string;
  amount: string;
  currency: string;
  risk: number;
  late: boolean;
  case_id: string | null;
}

export function toFeedItem(m: ScoredEventMessage): FeedItem {
  const e = m.event;
  return {
    key: `${m.seq}:${e.tx_id}`,
    seq: m.seq,
    tx_id: e.tx_id,
    ts: e.ts,
    src: e.src,
    dst: e.dst,
    amount: e.amount,
    currency: e.currency,
    risk: m.risk,
    late: m.late,
    case_id: m.case_id,
  };
}

/** Prepend `batch` (given in arrival order) so the newest is first; keep at most `cap`. */
export function pushEvents(feed: readonly FeedItem[], batch: readonly FeedItem[], cap = FEED_CAP): FeedItem[] {
  if (batch.length === 0) return feed.slice(0, cap);
  const newestFirst = batch.slice(-cap).reverse();
  return newestFirst.concat(feed.slice(0, Math.max(0, cap - newestFirst.length)));
}

const TIER_RANK: Record<Tier, number> = { LOW: 0, MEDIUM: 1, HIGH: 2, CRITICAL: 3 };

export function tierRank(t: Tier): number {
  return TIER_RANK[t] ?? 0;
}

function time(s: string | undefined): number {
  const t = s ? Date.parse(s) : NaN;
  return Number.isFinite(t) ? t : -Infinity;
}

/** Insert or replace a Case, ignoring an update older than the one already held. */
export function upsertCase(cases: Readonly<Record<string, CaseSummary>>, c: CaseSummary): Record<string, CaseSummary> {
  const prev = cases[c.case_id];
  if (prev && time(c.updated_at) < time(prev.updated_at)) return cases as Record<string, CaseSummary>;
  return { ...cases, [c.case_id]: c };
}

function priorityValue(p: string): number {
  const v = Number.parseFloat(p);
  return Number.isFinite(v) ? v : -Infinity;
}

/** Active_Queue: ACTIVE, not closed or merged, ordered by Case_Priority descending
 *  (ties: higher risk first, then case id) so the order is deterministic. */
export function activeQueue(cases: Readonly<Record<string, CaseSummary>>): string[] {
  return Object.values(cases)
    .filter((c) => c.placement === "ACTIVE" && c.status !== "CLOSED" && !c.merged_into)
    .sort(
      (a, b) =>
        priorityValue(b.priority) - priorityValue(a.priority) ||
        b.risk - a.risk ||
        (a.case_id < b.case_id ? -1 : a.case_id > b.case_id ? 1 : 0),
    )
    .map((c) => c.case_id);
}

/** A Case worth announcing: HIGH or CRITICAL and live in the Active_Queue. */
export function isAlert(c: CaseSummary | undefined): boolean {
  return !!c && tierRank(c.tier) >= TIER_RANK.HIGH && c.placement === "ACTIVE" && c.status !== "CLOSED" && !c.merged_into;
}

export interface Announcement {
  id: number;
  text: string;
  politeness: "polite" | "assertive";
}

/** Announcement for `next` if it has just become an alert (new, upgraded or admitted). */
export function announcementFor(prev: CaseSummary | undefined, next: CaseSummary, id: number): Announcement | null {
  if (!isAlert(next)) return null;
  if (isAlert(prev)) {
    if (prev!.tier === "CRITICAL" || next.tier !== "CRITICAL") return null;
    return { id, text: `Case ${next.case_id} is now CRITICAL: risk ${next.risk.toFixed(2)}.`, politeness: "assertive" };
  }
  const n = next.accounts.length;
  return {
    id,
    text: `New ${next.tier} case ${next.case_id}: risk ${next.risk.toFixed(2)}, ${n} account${n === 1 ? "" : "s"}.`,
    politeness: next.tier === "CRITICAL" ? "assertive" : "polite",
  };
}

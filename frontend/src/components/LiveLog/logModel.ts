// Live log model (Req 38.7): pure functions that turn store transitions (WebSocket events,
// cases and metrics frames) and polled REST documents (Antibodies, Playbook_Actions) into
// timestamped log entries, kept in timestamp order and capped.

import type { CaseSummary, MetricsMessage } from "../../api/types";
import type { FeedItem } from "../../store/reducers";

/** Retained lines; older entries are dropped first. */
export const LOG_CAP = 500;

export type LogKind =
  | "ingested"
  | "rejected"
  | "duplicate"
  | "case-opened"
  | "case-updated"
  | "label"
  | "antibody-created"
  | "antibody-retired"
  | "playbook";

export interface LogEntry {
  /** Unique and stable, used as the React key and to drop repeats. */
  id: string;
  ts: string;
  kind: LogKind;
  text: string;
}

export const KIND_LABEL: Record<LogKind, string> = {
  ingested: "INGESTED",
  rejected: "REJECTED",
  duplicate: "DUPLICATE",
  "case-opened": "CASE OPENED",
  "case-updated": "CASE UPDATED",
  label: "FEEDBACK",
  "antibody-created": "ANTIBODY",
  "antibody-retired": "ANTIBODY RETIRED",
  playbook: "PLAYBOOK",
};

/** The part of the dashboard state the log reads. */
export interface LogSource {
  feed: readonly FeedItem[];
  cases: Readonly<Record<string, CaseSummary>>;
  metrics: MetricsMessage | null;
}

function time(ts: string): number {
  const t = Date.parse(ts);
  return Number.isFinite(t) ? t : Number.POSITIVE_INFINITY;
}

/** Merge ``incoming`` into ``log`` (both any order), drop repeated ids, sort by timestamp
 *  (ties keep arrival order) and keep the newest ``cap``. */
export function mergeEntries(log: readonly LogEntry[], incoming: readonly LogEntry[], cap = LOG_CAP): LogEntry[] {
  if (incoming.length === 0) return log.length > cap ? log.slice(log.length - cap) : (log as LogEntry[]);
  const seen = new Set(log.map((e) => e.id));
  const fresh = incoming.filter((e) => !seen.has(e.id) && (seen.add(e.id), true));
  const all = [...log, ...fresh].map((e, i) => ({ e, i, t: time(e.ts) }));
  all.sort((a, b) => a.t - b.t || a.i - b.i);
  const out = all.map((x) => x.e);
  return out.length > cap ? out.slice(out.length - cap) : out;
}

export function eventEntry(f: FeedItem): LogEntry {
  const extra = `${f.late ? ", late" : ""}${f.case_id ? `, case ${f.case_id}` : ""}`;
  return {
    id: `ev:${f.key}`,
    ts: f.ts,
    kind: "ingested",
    text: `${f.tx_id}: ${f.src} → ${f.dst} ${f.amount} ${f.currency}, risk ${f.risk.toFixed(2)}${extra}`,
  };
}

function caseEntry(prev: CaseSummary | undefined, next: CaseSummary): LogEntry | null {
  const base = `${next.case_id}@${next.updated_at}`;
  if (!prev) {
    const n = next.accounts.length;
    return {
      id: `case:${base}`,
      ts: next.updated_at,
      kind: "case-opened",
      text: `${next.case_id}: ${next.tier}, risk ${next.risk.toFixed(2)}, ${n} account${n === 1 ? "" : "s"}, ${next.tx_count} transaction${next.tx_count === 1 ? "" : "s"}`,
    };
  }
  if (prev.updated_at === next.updated_at) return null; // a REST refresh of an unchanged Case
  if (next.label && next.label !== prev.label) {
    return { id: `label:${base}`, ts: next.updated_at, kind: "label", text: `${next.label} recorded on ${next.case_id}` };
  }
  const changes: string[] = [];
  if (prev.status !== next.status) changes.push(`status ${prev.status} → ${next.status}`);
  if (prev.tier !== next.tier) changes.push(`tier ${prev.tier} → ${next.tier}`);
  if (prev.placement !== next.placement) changes.push(`${next.placement.toLowerCase()}`);
  if (next.merged_into && next.merged_into !== prev.merged_into) changes.push(`merged into ${next.merged_into}`);
  if (next.assignee && next.assignee !== prev.assignee) changes.push(`opened by ${next.assignee}`);
  if (prev.tx_count !== next.tx_count) changes.push(`${next.tx_count} transactions`);
  changes.push(`risk ${next.risk.toFixed(2)}`);
  return { id: `case:${base}`, ts: next.updated_at, kind: "case-updated", text: `${next.case_id}: ${changes.join(", ")}` };
}

function ingestCounters(m: MetricsMessage | null): { malformed: number; duplicate: number } | null {
  const ing = m?.ingest;
  if (typeof ing !== "object" || ing === null) return null;
  const r = ing as Record<string, unknown>;
  const num = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? v : null);
  const malformed = num(r.malformed);
  const duplicate = num(r.duplicate);
  return malformed === null || duplicate === null ? null : { malformed, duplicate };
}

/** Entries implied by one store transition. Metrics frames carry cumulative ingest counters,
 *  so rejected / duplicate entries are their increase over the previous frame. */
export function transitionEntries(prev: LogSource, next: LogSource): LogEntry[] {
  const out: LogEntry[] = [];
  if (next.feed !== prev.feed) {
    const had = new Set(prev.feed.map((f) => f.key));
    const fresh = next.feed.filter((f) => !had.has(f.key));
    for (let i = fresh.length - 1; i >= 0; i--) out.push(eventEntry(fresh[i]!)); // feed is newest first
  }
  if (next.cases !== prev.cases) {
    for (const [id, c] of Object.entries(next.cases)) {
      if (c === prev.cases[id]) continue;
      const e = caseEntry(prev.cases[id], c);
      if (e) out.push(e);
    }
  }
  if (next.metrics !== prev.metrics && next.metrics) {
    const a = ingestCounters(prev.metrics);
    const b = ingestCounters(next.metrics);
    const ts = typeof next.metrics.ts === "string" ? next.metrics.ts : new Date().toISOString();
    const tag = `${next.metrics.seq ?? ts}`;
    if (a && b) {
      const rej = b.malformed - a.malformed;
      const dup = b.duplicate - a.duplicate;
      if (rej > 0) out.push({ id: `rej:${tag}`, ts, kind: "rejected", text: `${rej} event${rej === 1 ? "" : "s"} rejected as invalid` });
      if (dup > 0) out.push({ id: `dup:${tag}`, ts, kind: "duplicate", text: `${dup} duplicate event${dup === 1 ? "" : "s"} dropped` });
    }
  }
  return out;
}

/** Antibodies in GET /immune ``recent_antibodies``: one entry for creation, one for retirement. */
export function antibodyEntries(doc: unknown): LogEntry[] {
  const list = (doc as { recent_antibodies?: unknown } | null)?.recent_antibodies;
  if (!Array.isArray(list)) return [];
  const out: LogEntry[] = [];
  for (const raw of list) {
    if (typeof raw !== "object" || raw === null) continue;
    const a = raw as Record<string, unknown>;
    const id = typeof a.ab_id === "string" ? a.ab_id : null;
    if (!id) continue;
    if (typeof a.created_at === "string") {
      const origin = typeof a.origin === "string" ? ` from ${a.origin}` : "";
      out.push({ id: `ab:${id}:new`, ts: a.created_at, kind: "antibody-created", text: `${id} created${origin}` });
    }
    if (typeof a.retired_at === "string") {
      const why = typeof a.retired_reason === "string" ? `: ${a.retired_reason}` : "";
      out.push({ id: `ab:${id}:ret`, ts: a.retired_at, kind: "antibody-retired", text: `${id} retired${why}` });
    }
  }
  return out;
}

const EXECUTED = new Set(["PERFORMED", "DRY_RUN"]);

/** Executed Playbook_Actions (PERFORMED or DRY_RUN) in GET /soc/playbook-actions. */
export function playbookEntries(doc: unknown): LogEntry[] {
  const list = (doc as { actions?: unknown } | null)?.actions;
  if (!Array.isArray(list)) return [];
  const out: LogEntry[] = [];
  for (const raw of list) {
    if (typeof raw !== "object" || raw === null) continue;
    const a = raw as Record<string, unknown>;
    if (typeof a.action_id !== "string" || typeof a.status !== "string" || !EXECUTED.has(a.status)) continue;
    const ts = [a.completed_at, a.decided_at, a.scheduled_at].find((x): x is string => typeof x === "string");
    if (!ts) continue;
    const targets = Array.isArray(a.targets) && a.targets.length ? ` on ${a.targets.join(", ")}` : "";
    const dry = a.status === "DRY_RUN" ? " (dry run)" : "";
    out.push({
      id: `pb:${a.action_id}`,
      ts,
      kind: "playbook",
      text: `${String(a.type)}${targets} for ${String(a.case_id)}${dry}`,
    });
  }
  return out;
}

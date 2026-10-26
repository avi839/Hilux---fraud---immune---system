// Replay_Timeline position helpers (Req 22.6, 17.4-17.7): the slider span, event steps,
// the ``t`` sent to /timeline/at and the client-side "at or before T" filters.

import type { EvidenceDoc, ExplanationItemDoc, TimelineDoc, TimelineEntryDoc } from "../api/types";
import { atOrBefore, toMs } from "./time";

export interface Span {
  startMs: number;
  endMs: number;
}

export function timelineSpan(tl: Pick<TimelineDoc, "start" | "end">): Span | null {
  const startMs = toMs(tl.start);
  const endMs = toMs(tl.end);
  return Number.isFinite(startMs) && Number.isFinite(endMs) && endMs >= startMs ? { startMs, endMs } : null;
}

export function clampT(t: number, span: Span): number {
  return Math.min(span.endMs, Math.max(span.startMs, t));
}

/** Distinct event times (epoch ms) inside the span, ascending: the stops of the step
 *  buttons, the arrow keys and playback. */
export function eventStops(entries: readonly TimelineEntryDoc[], span: Span): number[] {
  const s = new Set<number>();
  for (const e of entries) {
    const ms = toMs(e.event.ts);
    if (Number.isFinite(ms) && ms >= span.startMs && ms <= span.endMs) s.add(ms);
  }
  return [...s].sort((a, b) => a - b);
}

/** The next stop strictly after (dir 1) or before (dir -1) ``t``; the span end/start when
 *  there is none. */
export function stepT(stops: readonly number[], t: number, dir: 1 | -1, span: Span): number {
  if (dir === 1) return stops.find((s) => s > t) ?? span.endMs;
  for (let i = stops.length - 1; i >= 0; i--) if (stops[i]! < t) return stops[i]!;
  return span.startMs;
}

/** ``t`` for GET /timeline/at. JS dates drop sub-millisecond digits, so the span ends are
 *  sent as the server's own strings: a truncated start would fall before the timeline and
 *  be rejected with 422. */
export function frameParam(t: number, tl: Pick<TimelineDoc, "start" | "end">): string {
  const span = timelineSpan(tl);
  if (span) {
    if (t <= span.startMs) return tl.start;
    if (t >= span.endMs) return tl.end;
  }
  return new Date(t).toISOString();
}

export function entriesAt(entries: readonly TimelineEntryDoc[], t: number | null): TimelineEntryDoc[] {
  return t === null ? [...entries] : entries.filter((e) => atOrBefore(e.event.ts, t));
}

/**
 * Evidence as of ``t``: chain entries with ts <= t, and explanation items/notes that cite at
 * least one transaction still in the filtered chain. The summary line is left as the Case's
 * current state (the server does not provide historical summaries); the panel says so.
 */
export function evidenceAt(ev: EvidenceDoc, t: number | null): { evidence: EvidenceDoc; kept: number; total: number } {
  const total = ev.chain?.entries.length ?? 0;
  if (t === null || !ev.chain) return { evidence: ev, kept: total, total };
  const entries = ev.chain.entries.filter((e) => atOrBefore(e.ts, t));
  const txs = new Set(entries.map((e) => e.tx_id));
  const keep = (it: ExplanationItemDoc) => it.tx_ids.length === 0 || it.tx_ids.some((tx) => txs.has(tx));
  const ex = ev.explanation;
  const allTx = new Set(ev.chain.entries.map((e) => e.tx_id));
  const notes = ex.notes.filter((n) => {
    const tx = n.split(":")[0] ?? "";
    return !allTx.has(tx) || txs.has(tx);
  });
  return {
    evidence: {
      ...ev,
      chain: { ...ev.chain, entries },
      explanation: {
        ...ex,
        reasons: ex.reasons.filter(keep),
        mitigating: ex.mitigating.filter(keep),
        insufficient: ex.insufficient.filter(keep),
        notes,
      },
    },
    kept: entries.length,
    total,
  };
}

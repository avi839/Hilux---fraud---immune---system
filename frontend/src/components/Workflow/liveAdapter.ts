// Live_Adapter (Req 43.8): maps the dashboard store (the /ws/events feed, /ws/cases Cases and
// their Feedback_Labels) to simulation events. Pure: the caller passes the store snapshot,
// the current time and the number of active particles, and keeps the returned state.
//
// Rate: at most MAX_SPAWN_PER_S spawns per second (token bucket) and never more than
// PARTICLE_CAP active particles. Frames that do not fit are counted in ``dropped`` and shown
// as "frames not drawn". When a batch has to be sampled, suspicious frames go first.

import type { CaseSummary, FeedbackLabel, Tier } from "../../api/types";
import type { FeedItem } from "../../store/reducers";
import { ACT_INDEX, RECEIVERS, SENDERS } from "./layout";
import { PARTICLE_CAP, bandTier, outcomeFor, type SimEvent } from "./simModel";

export const MAX_SPAWN_PER_S = 20;

interface SeenCase {
  updated: string;
  tier: Tier;
  label: FeedbackLabel | null;
}

export interface LiveAdapterState {
  primed: boolean;
  lastSeq: number;
  tokens: number;
  lastNow: number | null;
  cases: Record<string, SeenCase>;
  weights: Record<string, number> | null;
  /** Frames not drawn because of the rate or particle cap. */
  dropped: number;
  /** Frames drawn. */
  drawn: number;
}

export function initialLiveState(): LiveAdapterState {
  return { primed: false, lastSeq: -1, tokens: MAX_SPAWN_PER_S, lastNow: null, cases: {}, weights: null, dropped: 0, drawn: 0 };
}

export interface LiveInput {
  /** The store feed, newest first. */
  feed: readonly FeedItem[];
  cases: Readonly<Record<string, CaseSummary>>;
  /** Current Sensor_Weights if the server reports them (GET /metrics "weights"), else null. */
  weights?: Readonly<Record<string, number>> | null;
  /** Wall-clock time in ms. */
  now: number;
  /** Particles currently active in the simulation. */
  active: number;
}

export interface LiveOutput {
  state: LiveAdapterState;
  events: SimEvent[];
}

/** Stable small hash of an account id, to pick a sender or receiver figure. */
export function figureFor(account: string, n: number): number {
  let h = 2166136261;
  for (let i = 0; i < account.length; i++) h = Math.imul(h ^ account.charCodeAt(i), 16777619);
  return (h >>> 0) % n;
}

/** A transaction id short enough for a 3D tag: long generator ids such as
 *  GEN-42-000000001442 keep their last six characters behind an ellipsis. */
export function shortTxId(id: string): string {
  return id.length > 10 ? `…${id.slice(-6)}` : id;
}

export function spawnFor(item: FeedItem, cases: Readonly<Record<string, CaseSummary>>): SimEvent {
  const outcome = outcomeFor(item.risk);
  const suspicious = outcome === "suspicious";
  const tier = suspicious ? ((item.case_id ? cases[item.case_id]?.tier : undefined) ?? bandTier(item.risk)) : null;
  return {
    type: "spawn",
    outcome,
    risk: item.risk,
    tier,
    txId: item.tx_id,
    caseId: suspicious ? item.case_id : null,
    sender: figureFor(item.src, SENDERS.length),
    receiver: figureFor(item.dst, RECEIVERS.length),
    tag: suspicious ? `${shortTxId(item.tx_id)} · risk ${item.risk.toFixed(2)}` : null,
    flagAt: ACT_INDEX,
  };
}

function weightChange(prev: Record<string, number> | null, next: Readonly<Record<string, number>> | null | undefined) {
  if (!prev || !next) return null;
  const changed = Object.keys(next).filter((k) => prev[k] !== undefined && Math.abs(next[k]! - prev[k]!) > 1e-9);
  if (!changed.length) return { delta: 0, note: "Sensor weights unchanged" };
  const delta = changed.reduce((s, k) => s + (next[k]! - prev[k]!), 0) / changed.length;
  const note = changed
    .slice(0, 3)
    .map((k) => `${k} ${prev[k]!.toFixed(2)}→${next[k]!.toFixed(2)}`)
    .join(", ");
  return { delta, note: `Weights: ${note}${changed.length > 3 ? ", …" : ""}` };
}

const labelText = (l: FeedbackLabel) => (l === "CONFIRMED_FRAUD" ? "confirmed fraud" : "false positive");

/** One adapter tick: new feed frames -> spawns, new/updated Cases -> cards, labels -> pulses. */
export function adaptLive(state: LiveAdapterState, input: LiveInput, cap = PARTICLE_CAP): LiveOutput {
  const events: SimEvent[] = [];
  const s: LiveAdapterState = { ...state, cases: { ...state.cases } };
  const newest = input.feed.reduce((m, f) => Math.max(m, f.seq), -1);

  // Refill the token bucket.
  if (s.lastNow !== null) s.tokens = Math.min(MAX_SPAWN_PER_S, s.tokens + (Math.max(0, input.now - s.lastNow) / 1000) * MAX_SPAWN_PER_S);
  s.lastNow = input.now;

  if (!s.primed) {
    // Entering Live_Mode: what is already in the store is history, not new traffic.
    s.primed = true;
    s.lastSeq = newest;
    for (const c of Object.values(input.cases)) s.cases[c.case_id] = { updated: c.updated_at, tier: c.tier, label: c.label ?? null };
    s.weights = input.weights ? { ...input.weights } : null;
    return { state: s, events };
  }

  // Feed frames. A lower sequence than seen means the server restarted: follow it silently.
  if (newest < s.lastSeq) s.lastSeq = newest;
  const fresh = input.feed.filter((f) => f.seq > s.lastSeq).sort((a, b) => a.seq - b.seq);
  if (fresh.length) {
    s.lastSeq = newest;
    const room = Math.max(0, Math.min(Math.floor(s.tokens), cap - input.active));
    let chosen = fresh;
    if (fresh.length > room) {
      const sus = fresh.filter((f) => f.risk >= 0.6);
      const legit = fresh.filter((f) => f.risk < 0.6);
      const keep = new Set([...sus, ...legit].slice(0, room));
      chosen = fresh.filter((f) => keep.has(f));
    }
    s.tokens -= chosen.length;
    s.dropped += fresh.length - chosen.length;
    s.drawn += chosen.length;
    for (const f of chosen) events.push(spawnFor(f, input.cases));
  }

  // Cases and Feedback_Labels.
  const wc = weightChange(s.weights, input.weights);
  if (input.weights) s.weights = { ...input.weights };
  for (const c of Object.values(input.cases)) {
    if (c.merged_into) continue;
    const seen = s.cases[c.case_id];
    const label = c.label ?? null;
    if (!seen) {
      events.push({ type: "case", caseId: c.case_id, tier: c.tier, risk: c.risk });
    } else if (seen.updated !== c.updated_at && seen.tier !== c.tier) {
      events.push({ type: "case", caseId: c.case_id, tier: c.tier, risk: c.risk });
    }
    if (label && seen?.label !== label) {
      events.push({
        type: "label",
        caseId: c.case_id,
        label,
        weightDelta: wc ? wc.delta : null,
        note: wc ? `${c.case_id} ${labelText(label)}. ${wc.note}` : `${c.case_id} ${labelText(label)}: sensor weights re-learned (change not reported by this server)`,
      });
    }
    s.cases[c.case_id] = { updated: c.updated_at, tier: c.tier, label };
  }
  return { state: s, events };
}

// Pure stepper of the Workflow simulation (Req 43.2-43.5, 43.15). No React, no three.js:
// ``step(state, dt, events)`` returns a new state, so the story can be fast-forwarded or
// replayed deterministically and every rule is unit-testable in jsdom.
//
// Particle life: inbound arc (sender -> SENSE ring) -> stages (SENSE..ACT rings, in order)
// -> outbound arc to a Receiver (legit) or a merge arc into a Case_Card (suspicious).
// Conservation: spawned = active + delivered + cased + blocked. A spawn refused because
// PARTICLE_CAP particles are active is counted in ``dropped`` and never spawned.

import type { FeedbackLabel, Tier } from "../../api/types";
import {
  ACT_INDEX,
  ATTACKER_POS,
  CANARY_POS,
  CARD_LIFT,
  CASE_FORM_POS,
  INBOUND_LIFT,
  LEARN_INDEX,
  MERGE_LIFT,
  OUTBOUND_LIFT,
  PERCEIVE_INDEX,
  PROBE_LIFT,
  RECEIVERS,
  RING_POS,
  SENDERS,
  STAGES,
  arcPoint,
  chest,
  deskSlotPos,
  pulsePoint,
  stagePoint,
  DESK_SLOTS,
  type Vec3,
} from "./layout";

/** Most Sim_Particles alive at once (Req 43.15). */
export const PARTICLE_CAP = 400;
/** risk.alert_threshold: at or above it a transaction takes the suspicious path. */
export const ALERT_THRESHOLD = 0.6;
export const MAX_CARDS = 12;
export const MAX_PULSES = 6;

export const INBOUND_S = 1.8;
export const ATTACK_INBOUND_S = 2.2;
export const PROBE_S = 2.4;
/** Rings per second through the core; featured (story) particles move a little slower. */
export const STAGE_SPEED = 2.2;
export const FEATURED_SPEED = 1.6;
export const OUTBOUND_S = 1.5;
export const MERGE_S = 1.0;
export const FORM_S = 1.0;
export const FLY_S = 2.0;
export const FLIP_S = 0.9;
export const LABELLED_KEEP_S = 7;
export const PULSE_S = 2.6;
/** Longest internal sub-step: a particle advances at most 0.22 rings per sub-step, so it
 *  never skips a ring however large ``dt`` is. */
export const MAX_SUBSTEP = 0.1;
export const WEIGHT_MIN = 0.1;
export const WEIGHT_MAX = 1.0;

export type Outcome = "legit" | "suspicious";
export type ParticleKind = "tx" | "attack" | "probe";
export type Phase = "inbound" | "stages" | "outbound" | "merging" | "probe";

export interface Particle {
  id: number;
  kind: ParticleKind;
  outcome: Outcome;
  risk: number;
  tier: Tier | null;
  txId: string | null;
  caseId: string | null;
  /** Story group ("alice", "bob", "mule"): the particle holds at ``target`` until advanced. */
  group: string | null;
  /** Short text drawn next to the particle, e.g. "T007 · 4,800 EUR". */
  tag: string | null;
  sender: number;
  receiver: number;
  phase: Phase;
  /** Progress 0..1 along the current arc (inbound, outbound, merging, probe). */
  t: number;
  /** Stage coordinate 0 (SENSE ring) .. 4 (ACT ring) while in "stages". */
  s: number;
  /** Ring to hold at; above ACT_INDEX the particle is released. Infinity when not held. */
  target: number;
  /** Stage index from which a suspicious particle is drawn as flagged (red, spiked). */
  flagAt: number;
  /** Highest ring reached so far (-1 before SENSE). */
  visited: number;
  age: number;
}

export type CardPhase = "forming" | "flying" | "desk" | "flipping" | "labelled";

export interface PendingLabel {
  label: FeedbackLabel;
  weightDelta: number | null;
  note: string;
}

export interface SimCase {
  id: string;
  tier: Tier;
  risk: number | null;
  phase: CardPhase;
  /** Progress 0..1 (flying, flipping) or seconds in phase (forming, labelled). */
  t: number;
  age: number;
  particles: number;
  label: FeedbackLabel | null;
  pending: PendingLabel | null;
  hold: boolean;
  slot: number;
  seq: number;
}

export interface Pulse {
  id: number;
  caseId: string;
  t: number;
  weightDelta: number | null;
  note: string;
}

export interface SimCounts {
  spawned: number;
  delivered: number;
  cased: number;
  blocked: number;
  dropped: number;
}

export interface SimState {
  time: number;
  nextId: number;
  particles: Particle[];
  cases: SimCase[];
  pulses: Pulse[];
  counts: SimCounts;
  /** Glow 0..1 of each of the six Stage_Rings. */
  glow: number[];
  /** Displayed sensor-weight scale on the PERCEIVE ring (Req 43.5). */
  weightScale: number;
  weightFlash: number;
  weightNote: string;
  antibodies: number;
  shieldFlash: number;
  canaryFlash: number;
  receiverFlash: number[];
  cardSeq: number;
}

export type SimEvent =
  | {
      type: "spawn";
      kind?: ParticleKind;
      outcome: Outcome;
      risk: number;
      tier?: Tier | null;
      txId?: string | null;
      caseId?: string | null;
      sender?: number;
      receiver?: number;
      group?: string | null;
      tag?: string | null;
      flagAt?: number;
      target?: number;
    }
  | { type: "advance"; group: string; target: number }
  | { type: "case"; caseId: string; tier: Tier; risk?: number | null; hold?: boolean }
  | { type: "label"; caseId: string; label: FeedbackLabel; weightDelta: number | null; note: string }
  | { type: "antibody" };

export function initialSim(): SimState {
  return {
    time: 0,
    nextId: 1,
    particles: [],
    cases: [],
    pulses: [],
    counts: { spawned: 0, delivered: 0, cased: 0, blocked: 0, dropped: 0 },
    glow: STAGES.map(() => 0),
    weightScale: 1,
    weightFlash: 0,
    weightNote: "Sensor weights 1.00 (start)",
    antibodies: 0,
    shieldFlash: 0,
    canaryFlash: 0,
    receiverFlash: RECEIVERS.map(() => 0),
    cardSeq: 0,
  };
}

/** Severity_Tier of a risk score with the default ``risk.bands``; null below the threshold. */
export function bandTier(risk: number): Tier | null {
  if (!(risk >= ALERT_THRESHOLD)) return null;
  if (risk < 0.7) return "LOW";
  if (risk < 0.8) return "MEDIUM";
  if (risk < 0.9) return "HIGH";
  return "CRITICAL";
}

export function outcomeFor(risk: number): Outcome {
  return risk >= ALERT_THRESHOLD ? "suspicious" : "legit";
}

const clamp01 = (x: number) => Math.max(0, Math.min(1, x));
const clampW = (x: number) => Math.max(WEIGHT_MIN, Math.min(WEIGHT_MAX, x));
const mod = (n: number, m: number) => ((n % m) + m) % m;

/** Stage index a particle is at for the caption (null outside the core). */
export function stageOf(p: Particle): number | null {
  if (p.phase === "stages") return Math.min(ACT_INDEX, Math.floor(p.s + 1e-9));
  if (p.phase === "outbound" || p.phase === "merging") return ACT_INDEX;
  return null;
}

/** Drawn as a flagged (red, spiked) particle. */
export function isFlagged(p: Particle): boolean {
  if (p.outcome !== "suspicious" || p.kind !== "tx") return false;
  return p.phase === "merging" || (p.phase === "stages" && p.s >= p.flagAt - 1e-9);
}

export function activeCount(s: SimState): number {
  return s.particles.length;
}

// --- events --------------------------------------------------------------------------

function findCase(s: SimState, id: string): SimCase | undefined {
  return s.cases.find((c) => c.id === id);
}

function ensureCase(s: SimState, id: string, tier: Tier, risk: number | null): SimCase | null {
  const found = findCase(s, id);
  if (found) return found;
  if (s.cases.length >= MAX_CARDS) {
    // Make room by retiring the oldest card that has already reached the desk.
    const old = s.cases
      .filter((c) => c.phase === "labelled" || c.phase === "desk")
      .sort((a, b) => (a.phase === b.phase ? a.seq - b.seq : a.phase === "labelled" ? -1 : 1))[0];
    if (!old) return null;
    s.cases = s.cases.filter((c) => c !== old);
  }
  const c: SimCase = {
    id,
    tier,
    risk,
    phase: "forming",
    t: 0,
    age: 0,
    particles: 0,
    label: null,
    pending: null,
    hold: false,
    slot: -1,
    seq: ++s.cardSeq,
  };
  s.cases = [...s.cases, c];
  return c;
}

function applyEvent(s: SimState, e: SimEvent): void {
  switch (e.type) {
    case "spawn": {
      if (s.particles.length >= PARTICLE_CAP) {
        s.counts.dropped++;
        return;
      }
      const id = s.nextId++;
      const kind = e.kind ?? "tx";
      const p: Particle = {
        id,
        kind,
        outcome: e.outcome,
        risk: e.risk,
        tier: e.tier ?? (e.outcome === "suspicious" ? bandTier(e.risk) : null),
        txId: e.txId ?? null,
        caseId: e.caseId ?? null,
        group: e.group ?? null,
        tag: e.tag ?? null,
        sender: mod(e.sender ?? id, SENDERS.length),
        receiver: mod(e.receiver ?? id * 7, RECEIVERS.length),
        phase: kind === "probe" ? "probe" : "inbound",
        t: 0,
        s: 0,
        target: e.group ? (e.target ?? 0) : Infinity,
        flagAt: e.flagAt ?? ACT_INDEX,
        visited: -1,
        age: 0,
      };
      s.particles = [...s.particles, p];
      s.counts.spawned++;
      return;
    }
    case "advance":
      s.particles = s.particles.map((p) => (p.group === e.group ? { ...p, target: e.target } : p));
      return;
    case "case": {
      const c = ensureCase(s, e.caseId, e.tier, e.risk ?? null);
      if (!c) return;
      c.tier = e.tier;
      if (e.risk !== undefined) c.risk = e.risk;
      if (e.hold !== undefined) c.hold = e.hold;
      return;
    }
    case "label": {
      const c = findCase(s, e.caseId) ?? ensureCase(s, e.caseId, "MEDIUM", null);
      if (!c || c.label || c.pending) return;
      const pending = { label: e.label, weightDelta: e.weightDelta, note: e.note };
      if (c.phase === "desk") startFlip(c, pending);
      else if (c.phase === "forming" || c.phase === "flying") c.pending = pending;
      return;
    }
    case "antibody":
      s.antibodies = Math.min(6, s.antibodies + 1);
      s.shieldFlash = 1;
      return;
  }
}

function startFlip(c: SimCase, l: PendingLabel): void {
  c.phase = "flipping";
  c.t = 0;
  c.label = l.label;
  c.pending = l;
}

// --- motion --------------------------------------------------------------------------

function receiveIntoCase(s: SimState, p: Particle): void {
  s.counts.cased++;
  if (!p.caseId) return;
  const c = ensureCase(s, p.caseId, p.tier ?? bandTier(p.risk) ?? "LOW", p.risk);
  if (c) c.particles++;
}

/** Advance one particle by ``h`` seconds. Returns false once it has left the scene. */
function moveParticle(s: SimState, p: Particle, h: number): boolean {
  p.age += h;
  switch (p.phase) {
    case "probe":
      p.t += h / PROBE_S;
      if (p.t >= 1) {
        s.counts.blocked++;
        s.canaryFlash = 1;
        return false;
      }
      return true;
    case "inbound":
      p.t += h / (p.kind === "attack" ? ATTACK_INBOUND_S : INBOUND_S);
      if (p.t >= 1) {
        p.phase = "stages";
        p.t = 0;
        p.s = 0;
        p.visited = 0;
        s.glow[0] = 1;
      }
      return true;
    case "stages": {
      const speed = p.group ? FEATURED_SPEED : STAGE_SPEED;
      const stop = p.target > ACT_INDEX ? ACT_INDEX : Math.max(0, p.target);
      if (p.s < stop) p.s = Math.min(stop, p.s + speed * h);
      const ring = Math.min(ACT_INDEX, Math.floor(p.s + 1e-9));
      if (ring > p.visited) {
        p.visited = ring;
        s.glow[ring] = 1;
      }
      // An Antibody on the PERCEIVE ring stops a repeated attack pattern.
      if (p.kind === "attack" && s.antibodies > 0 && p.s >= PERCEIVE_INDEX) {
        s.counts.blocked++;
        s.shieldFlash = 1;
        s.glow[PERCEIVE_INDEX] = 1;
        return false;
      }
      if (p.s >= ACT_INDEX && p.target > ACT_INDEX) {
        p.phase = p.outcome === "suspicious" && p.kind === "tx" ? "merging" : "outbound";
        p.t = 0;
      }
      return true;
    }
    case "outbound":
      p.t += h / OUTBOUND_S;
      if (p.t >= 1) {
        s.counts.delivered++;
        s.receiverFlash[p.receiver] = 1;
        return false;
      }
      return true;
    case "merging":
      p.t += h / MERGE_S;
      if (p.t >= 1) {
        receiveIntoCase(s, p);
        return false;
      }
      return true;
  }
}

function freeSlot(s: SimState, self: SimCase): number {
  const placed = s.cases.filter((c) => c !== self && c.slot >= 0);
  const used = new Set(placed.map((c) => c.slot));
  for (let k = 0; k < DESK_SLOTS; k++) if (!used.has(k)) return k;
  // Desk full: retire the oldest labelled card, else the oldest unlabelled one.
  const victim =
    placed.filter((c) => c.phase === "labelled").sort((a, b) => a.seq - b.seq)[0] ??
    placed.filter((c) => c.phase === "desk").sort((a, b) => a.seq - b.seq)[0];
  if (!victim) return DESK_SLOTS - 1;
  s.cases = s.cases.filter((c) => c !== victim);
  return victim.slot;
}

function moveCards(s: SimState, h: number): void {
  const waiting = new Set(
    s.particles.filter((p) => p.caseId && p.kind === "tx" && p.outcome === "suspicious").map((p) => p.caseId as string),
  );
  for (const c of [...s.cases]) {
    c.age += h;
    switch (c.phase) {
      case "forming":
        c.t += h;
        if (c.t >= FORM_S && !waiting.has(c.id)) {
          c.phase = "flying";
          c.t = 0;
          c.slot = freeSlot(s, c);
        }
        break;
      case "flying":
        c.t += h / FLY_S;
        if (c.t >= 1) {
          c.phase = "desk";
          c.t = 0;
          if (c.pending) startFlip(c, c.pending);
        }
        break;
      case "desk":
        c.t += h;
        break;
      case "flipping":
        c.t += h / FLIP_S;
        if (c.t >= 1) {
          c.phase = "labelled";
          c.t = 0;
          const l = c.pending;
          c.pending = null;
          if (l && s.pulses.length < MAX_PULSES) {
            s.pulses = [...s.pulses, { id: s.nextId++, caseId: c.id, t: 0, weightDelta: l.weightDelta, note: l.note }];
          }
        }
        break;
      case "labelled":
        c.t += h;
        if (c.t >= LABELLED_KEEP_S) s.cases = s.cases.filter((x) => x !== c);
        break;
    }
  }
}

function movePulses(s: SimState, h: number): void {
  const keep: Pulse[] = [];
  for (const p of s.pulses) {
    p.t += h / PULSE_S;
    if (p.t >= 0.42 && p.t <= 0.58) s.glow[LEARN_INDEX] = 1;
    if (p.t >= 1) {
      s.weightScale = clampW(s.weightScale + (p.weightDelta ?? 0));
      s.weightFlash = 1;
      s.weightNote = p.note;
      s.glow[PERCEIVE_INDEX] = 1;
    } else keep.push(p);
  }
  s.pulses = keep;
}

function decay(s: SimState, h: number): void {
  const k = h * 1.4;
  s.glow = s.glow.map((g) => Math.max(0, g - k));
  s.receiverFlash = s.receiverFlash.map((g) => Math.max(0, g - k));
  s.weightFlash = Math.max(0, s.weightFlash - h * 0.5);
  s.shieldFlash = Math.max(0, s.shieldFlash - k);
  s.canaryFlash = Math.max(0, s.canaryFlash - h * 0.8);
}

function clone(s: SimState): SimState {
  return {
    ...s,
    particles: s.particles.map((p) => ({ ...p })),
    cases: s.cases.map((c) => ({ ...c, pending: c.pending ? { ...c.pending } : null })),
    pulses: s.pulses.map((p) => ({ ...p })),
    counts: { ...s.counts },
    glow: [...s.glow],
    receiverFlash: [...s.receiverFlash],
  };
}

/** Apply ``events`` (in order), then advance the simulation by ``dt`` seconds. Pure. */
export function step(state: SimState, dt: number, events: readonly SimEvent[] = []): SimState {
  const s = clone(state);
  for (const e of events) applyEvent(s, e);
  let left = Number.isFinite(dt) && dt > 0 ? dt : 0;
  while (left > 1e-12) {
    const h = Math.min(MAX_SUBSTEP, left);
    left -= h;
    s.time += h;
    s.particles = s.particles.filter((p) => moveParticle(s, p, h));
    moveCards(s, h);
    movePulses(s, h);
    decay(s, h);
  }
  return s;
}

// --- geometry for the renderer ------------------------------------------------------

const ease = (t: number) => {
  const u = clamp01(t);
  return u * u * (3 - 2 * u);
};

/** Small per-particle lane offset so particles on the same path do not overlap. */
function lane(id: number): number {
  return ((mod(id * 37, 9) - 4) / 4) * 0.16;
}

/** World position of a particle. */
export function particlePosition(p: Particle): Vec3 {
  const o = lane(p.id);
  let v: Vec3;
  switch (p.phase) {
    case "probe":
      v = arcPoint(chest(ATTACKER_POS), CANARY_POS, PROBE_LIFT, p.t);
      break;
    case "inbound": {
      const from = p.kind === "attack" ? chest(ATTACKER_POS) : chest(SENDERS[p.sender]!);
      v = arcPoint(from, RING_POS[0]!, INBOUND_LIFT, p.t);
      break;
    }
    case "stages": {
      const b = stagePoint(p.s);
      // A held particle hovers in its ring.
      const hold = p.group && p.s >= Math.min(ACT_INDEX, p.target) - 1e-6 ? 0.08 * Math.sin(p.age * 3 + p.id) : 0;
      v = [b[0], b[1] + hold, b[2]];
      break;
    }
    case "outbound":
      v = arcPoint(RING_POS[ACT_INDEX]!, chest(RECEIVERS[p.receiver]!), OUTBOUND_LIFT, p.t);
      break;
    case "merging":
      v = arcPoint(RING_POS[ACT_INDEX]!, CASE_FORM_POS, MERGE_LIFT, p.t);
      break;
  }
  return [v[0], v[1] + o, v[2] + o * 0.6];
}

export interface CardPose {
  pos: Vec3;
  /** Rotation about y: 0 shows the front (Case id, tier), PI the back (the label). */
  flip: number;
  opacity: number;
  scale: number;
}

export function cardPose(c: SimCase): CardPose {
  const slot = deskSlotPos(Math.max(0, c.slot));
  switch (c.phase) {
    case "forming":
      return { pos: CASE_FORM_POS, flip: 0, opacity: clamp01(0.35 + c.t), scale: 0.6 + 0.4 * clamp01(c.t / FORM_S) };
    case "flying":
      return { pos: arcPoint(CASE_FORM_POS, slot, CARD_LIFT, ease(c.t)), flip: 0, opacity: 1, scale: 1 };
    case "desk":
      return { pos: slot, flip: 0, opacity: 1, scale: 1 };
    case "flipping":
      return { pos: slot, flip: Math.PI * ease(c.t), opacity: 1, scale: 1 + 0.12 * Math.sin(Math.PI * clamp01(c.t)) };
    case "labelled":
      return { pos: slot, flip: Math.PI, opacity: clamp01(LABELLED_KEEP_S - c.t), scale: 1 };
  }
}

export function pulsePosition(p: Pulse): Vec3 {
  return pulsePoint(clamp01(p.t));
}

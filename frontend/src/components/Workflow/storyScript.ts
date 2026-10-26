// Story_Mode script (Req 43.6, 43.7, 43.14): a deterministic ~76 s loop that needs no API_Server.
// Every number in the captions comes from the README walkthrough ("How one transaction is
// processed, step by step"): Alice's T007, Bob's structuring T010-T012 and the mule chain
// T013-T015. ``STORY_CUES`` are the simulation events that make the scene match the captions.

import type { Tier } from "../../api/types";
import { SENDER_INDEX, STAGES, type StageId } from "./layout";
import { initialSim, step, type SimEvent, type SimState } from "./simModel";

/** Length of one loop at 1x (Req 43.6: 60-90 s). */
export const LOOP_S = 76;

export interface StoryCase {
  id: string;
  tier: Tier;
  risk: number;
}

export interface StoryStep {
  /** Seconds from the loop start. */
  t: number;
  stage: StageId;
  title: string;
  text: string;
  /** The transaction(s) this step is about. */
  tx?: string;
  case?: StoryCase;
}

const C1: StoryCase = { id: "C-1", tier: "CRITICAL", risk: 0.973 };
const C2: StoryCase = { id: "C-2", tier: "HIGH", risk: 0.8 };
const C3: StoryCase = { id: "C-3", tier: "HIGH", risk: 0.85 };

export const STORY_STEPS: readonly StoryStep[] = [
  {
    t: 0,
    stage: "SENSE",
    title: "Normal payments flow in",
    text: "Customers, a bank and a merchant send payments. Each one enters HELIX at SENSE, which checks that it is valid, not a duplicate and on time.",
  },
  {
    t: 4.5,
    stage: "PERCEIVE",
    title: "Everyday payments pass",
    text: "Sensors score every payment. Ordinary ones, like Alice's grocery, café and gym payments of 8.90 to 51.20 EUR, stay below the 0.6 alert threshold and go straight on to their receivers.",
  },
  {
    t: 10,
    stage: "SENSE",
    title: "Alice's T007 arrives",
    tx: "T007",
    text: "4,800.00 EUR from Lagos, on a new device DEV-X9, to ACC-UNKNOWN-77, an account Alice has never paid. SENSE accepts it: valid, new and on time.",
  },
  {
    t: 14,
    stage: "PERCEIVE",
    title: "The sensors flag T007",
    tx: "T007",
    text: "Amount sensor 0.70 fraud (4800.00 vs her mean of 37.69, z 346.64). Geo sensor 0.638 (5196.1 km in 1.17 h, 4454 km/h). Counterparty 0.35, timing 0.30. Fused belief 0.949.",
  },
  {
    t: 18,
    stage: "REMEMBER",
    title: "An abrupt break",
    tx: "T007",
    text: "Alice's Behavioural_DNA says this is not her normal life changing: the change-point detector reports an ABRUPT_BREAK starting at T007.",
  },
  {
    t: 21,
    stage: "REASON",
    title: "No laundering shape",
    tx: "T007",
    text: "The payment graph finds no motif for T007, so the second stage keeps belief 0.949.",
  },
  {
    t: 24,
    stage: "ACT",
    title: "Case C-1, CRITICAL",
    tx: "T007",
    case: C1,
    text: "Risk = belief + half the uncertainty = 0.949 + 0.5 × 0.049 = 0.973, tier CRITICAL. The particle turns into Case C-1 and flies to the investigator's desk.",
  },
  {
    t: 30,
    stage: "LEARN",
    title: "The investigator confirms fraud",
    case: C1,
    text: "The investigator labels C-1 CONFIRMED FRAUD. A learning pulse runs back through LEARN to PERCEIVE. The four sensors that blamed Alice stay at weight 1.00, the ceiling; a false positive would have lowered them to 0.95.",
  },
  {
    t: 36,
    stage: "SENSE",
    title: "Bob's three transfers",
    tx: "T010-T012",
    text: "Bob sends 9,800, 9,650 and 9,900 EUR to three shell accounts within 24 hours, each just under a 10,000 EUR reporting threshold.",
  },
  {
    t: 40,
    stage: "PERCEIVE",
    title: "The sensors cannot tell",
    tx: "T010-T012",
    text: "Bob has fewer than 5 events, so every sensor says \"can't tell\". On sensors alone his risk would be 0.5, below the alert threshold.",
  },
  {
    t: 44,
    stage: "REASON",
    title: "STRUCTURING motif",
    tx: "T010-T012",
    text: "At T012 the graph sees the pattern: STRUCTURING, motif mass 0.6 fraud and 0.4 unknown. The second-stage belief rises to 0.60.",
  },
  {
    t: 48,
    stage: "ACT",
    title: "Case C-2, HIGH",
    tx: "T010-T012",
    case: C2,
    text: "Risk 0.80, tier HIGH. Case C-2 groups the three transfers, 29,350.00 EUR at risk, and joins the investigator's queue.",
  },
  {
    t: 54,
    stage: "SENSE",
    title: "Money hops between accounts",
    tx: "T013-T015",
    text: "Carl → Dave → Erin → Finn: 5,000, then 4,600, then 4,300 EUR, 25 and 23 minutes apart.",
  },
  {
    t: 59,
    stage: "REASON",
    title: "MULE_CHAIN motif",
    tx: "T013-T015",
    text: "Money passes straight through with a cut at each hop. MULE_CHAIN fires at T014 and T015, motif mass 0.7. The second-stage belief is 0.70. Only the graph caught it.",
  },
  {
    t: 64,
    stage: "ACT",
    title: "Case C-3, HIGH",
    tx: "T013-T015",
    case: C3,
    text: "Risk 0.85, tier HIGH. Case C-3 holds 13,900.00 EUR at risk and goes to the investigator.",
  },
  {
    t: 70,
    stage: "LEARN",
    title: "The loop closes",
    text: "Every label moves the sensors that pointed at fraud by the learning rate 0.05, kept between 0.1 and 1.0. HELIX improves with each decision, and the loop starts again.",
  },
];

/** Plain-words description of each stage for the caption panel and legend (Req 43.12). */
export const STAGE_INFO: Record<StageId, string> = {
  SENSE: "Checks that each payment is valid, not a duplicate and on time.",
  PERCEIVE: "Seven sensors each say fraud, legit or can't tell, and their opinions are combined.",
  REMEMBER: "Compares the payment with the account's own habits and spots abrupt breaks.",
  REASON: "Looks at who paid whom to find laundering shapes such as structuring and mule chains.",
  ACT: "Turns the evidence into a risk score and tier, and opens a Case at risk 0.6 or more.",
  LEARN: "The investigator's label re-weights the sensors, so HELIX improves.",
};

export function stageIndex(id: StageId): number {
  return STAGES.indexOf(id);
}

/** Index of the step showing at time ``t`` (seconds into the loop). */
export function stepIndexAt(t: number, steps: readonly StoryStep[] = STORY_STEPS): number {
  let i = 0;
  for (let k = 0; k < steps.length; k++) if (steps[k]!.t <= t + 1e-9) i = k;
  return i;
}

// --- overlays ------------------------------------------------------------------------

export type OverlayId = "redBlue" | "soc";
export interface Overlays {
  redBlue: boolean;
  soc: boolean;
}
export const NO_OVERLAYS: Overlays = { redBlue: false, soc: false };

export interface OverlayNote {
  t: number;
  overlay: OverlayId;
  text: string;
}

export const OVERLAY_NOTES: readonly OverlayNote[] = [
  { t: 4, overlay: "redBlue", text: "The Attacker_Agent injects a mutated attack pattern. Nothing flags it, so it slips through." },
  { t: 9.5, overlay: "redBlue", text: "The Immune_System grows an Antibody from the miss: a shield appears on the PERCEIVE ring." },
  { t: 31, overlay: "redBlue", text: "The attacker tries the same pattern again. The Antibody blocks it at PERCEIVE." },
  { t: 26, overlay: "soc", text: "A probe touches a Canary decoy account. Nobody real uses it, so the canary raises an alert." },
  { t: 64, overlay: "soc", text: "A Playbook_Action places a HOLD on Case C-3 while the investigator reviews it." },
];

export function overlayNoteAt(t: number, overlays: Overlays): OverlayNote | null {
  let best: OverlayNote | null = null;
  for (const n of OVERLAY_NOTES) if (overlays[n.overlay] && n.t <= t && (!best || n.t >= best.t)) best = n;
  return best;
}

// --- cues ----------------------------------------------------------------------------

export interface StoryCue {
  t: number;
  overlay?: OverlayId;
  event: SimEvent;
}

/** Deterministic PRNG (mulberry32) so the background traffic is the same on every loop. */
function prng(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function backgroundCues(): StoryCue[] {
  const rnd = prng(43);
  const out: StoryCue[] = [];
  let t = 0.2;
  while (t < LOOP_S - 4) {
    out.push({
      t: Math.round(t * 100) / 100,
      event: {
        type: "spawn",
        outcome: "legit",
        risk: Math.round((0.05 + rnd() * 0.4) * 1000) / 1000,
        sender: Math.floor(rnd() * 7),
        receiver: Math.floor(rnd() * 4),
      },
    });
    t += t < 8 ? 0.4 + rnd() * 0.2 : 0.8 + rnd() * 0.6;
  }
  return out;
}

const featured = (
  t: number,
  txId: string,
  sender: string,
  group: string,
  c: StoryCase,
  tag: string,
  flagAt: number,
): StoryCue => ({
  t,
  event: {
    type: "spawn",
    outcome: "suspicious",
    risk: c.risk,
    tier: c.tier,
    txId,
    caseId: c.id,
    sender: SENDER_INDEX[sender] ?? 0,
    group,
    tag,
    flagAt,
    target: 0,
  },
});

const advance = (t: number, group: string, target: number): StoryCue => ({ t, event: { type: "advance", group, target } });
const openCase = (t: number, c: StoryCase, hold?: boolean, overlay?: OverlayId): StoryCue => ({
  t,
  overlay,
  event: { type: "case", caseId: c.id, tier: c.tier, risk: c.risk, ...(hold === undefined ? {} : { hold }) },
});
const attack = (t: number): StoryCue => ({
  t,
  overlay: "redBlue",
  event: { type: "spawn", kind: "attack", outcome: "legit", risk: 0.3, tag: "attack" },
});

const SCRIPTED: StoryCue[] = [
  // Alice's account takeover: flagged from PERCEIVE on.
  featured(8.6, "T007", "Alice", "alice", C1, "T007 · 4,800 EUR", 1),
  advance(14, "alice", 1),
  advance(18, "alice", 2),
  advance(21, "alice", 3),
  advance(24, "alice", 4),
  openCase(24, C1),
  advance(25.6, "alice", 5),
  {
    t: 30,
    event: {
      type: "label",
      caseId: "C-1",
      label: "CONFIRMED_FRAUD",
      weightDelta: 0,
      note: "C-1 confirmed fraud: weights stay 1.00 (ceiling)",
    },
  },
  // Bob's structuring: only REASON catches it.
  featured(34.4, "T010", "Bob", "bob", C2, "T010 · 9,800 EUR", 3),
  featured(35, "T011", "Bob", "bob", C2, "T011 · 9,650 EUR", 3),
  featured(35.6, "T012", "Bob", "bob", C2, "T012 · 9,900 EUR", 3),
  advance(40, "bob", 1),
  advance(42.5, "bob", 3),
  advance(48, "bob", 4),
  openCase(48, C2),
  advance(49.4, "bob", 5),
  // The mule chain Carl -> Dave -> Erin -> Finn.
  featured(52.6, "T013", "Carl", "mule", C3, "T013 · 5,000 EUR", 3),
  featured(53.2, "T014", "Dave", "mule", C3, "T014 · 4,600 EUR", 3),
  featured(53.8, "T015", "Erin", "mule", C3, "T015 · 4,300 EUR", 3),
  advance(56, "mule", 3),
  advance(63.5, "mule", 4),
  openCase(64, C3),
  advance(65, "mule", 5),
  // Red vs Blue: a missed wave, an Antibody, then a blocked wave.
  attack(4),
  attack(4.35),
  attack(4.7),
  { t: 9.5, overlay: "redBlue", event: { type: "antibody" } },
  attack(31),
  attack(31.35),
  attack(31.7),
  // SOC: a probe trips the canary; a playbook holds C-3.
  { t: 26, overlay: "soc", event: { type: "spawn", kind: "probe", outcome: "suspicious", risk: 0.9, tag: "probe" } },
  openCase(64.2, C3, true, "soc"),
];

/** Every cue of one loop, ordered by time (stable for equal times). */
export const STORY_CUES: readonly StoryCue[] = [...SCRIPTED, ...backgroundCues()]
  .map((c, i) => ({ c, i }))
  .sort((a, b) => a.c.t - b.c.t || a.i - b.i)
  .map(({ c }) => c);

/** Simulation events with ``from < t <= to`` for the enabled overlays. */
export function cuesBetween(from: number, to: number, overlays: Overlays = NO_OVERLAYS): SimEvent[] {
  const out: SimEvent[] = [];
  for (const c of STORY_CUES) {
    if (c.t <= from) continue;
    if (c.t > to) break;
    if (!c.overlay || overlays[c.overlay]) out.push(c.event);
  }
  return out;
}

/** Advance the story simulation from ``(state, t)`` by ``dt``; wraps to a fresh loop at LOOP_S. */
export function advanceStory(state: SimState, t: number, dt: number, overlays: Overlays): { state: SimState; t: number } {
  const to = t + dt;
  if (to >= LOOP_S) return { state: initialSim(), t: 0 };
  const from = t === 0 && state.time === 0 ? -1 : t;
  return { state: step(state, dt, cuesBetween(from, to, overlays)), t: to };
}

/** The story state at ``t`` seconds, computed by replaying the loop from 0 (for seeking). */
export function simulateStory(t: number, overlays: Overlays = NO_OVERLAYS, dt = 1 / 30): SimState {
  let s = initialSim();
  let now = 0;
  let first = true;
  const end = Math.max(0, Math.min(LOOP_S - 1e-6, t));
  while (now < end - 1e-9) {
    const h = Math.min(dt, end - now);
    s = step(s, h, cuesBetween(first ? -1 : now, now + h, overlays));
    first = false;
    now += h;
  }
  return s;
}

/** Story time to seek to for step ``i``: a moment after it starts, so its scene has settled. */
export function seekTimeFor(i: number, steps: readonly StoryStep[] = STORY_STEPS): number {
  const s = steps[Math.max(0, Math.min(steps.length - 1, i))]!;
  const next = steps[i + 1]?.t ?? LOOP_S;
  return Math.min(s.t + 2.5, next - 0.05);
}

// --- view defaults -------------------------------------------------------------------

export type Speed = 0.5 | 1 | 2;
export const SPEEDS: readonly Speed[] = [0.5, 1, 2];

export interface ViewDefaults {
  mode: "story";
  playing: boolean;
  speed: Speed;
}

/** First state of the view (Req 43.7, 43.14): Story_Mode, playing unless reduced motion. */
export function viewDefaults(reducedMotion: boolean): ViewDefaults {
  return { mode: "story", playing: !reducedMotion, speed: 1 };
}

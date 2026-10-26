// Scene layout of the Workflow simulation (Req 43.2): where every figure, ring and path sits.
// Pure numbers only (no three.js), so the simulation and its tests run without WebGL.

export type Vec3 = readonly [number, number, number];

export const STAGES = ["SENSE", "PERCEIVE", "REMEMBER", "REASON", "ACT", "LEARN"] as const;
export type StageId = (typeof STAGES)[number];
/** Index of the ACT ring: transactions pass SENSE..ACT, LEARN is reached by Learning_Pulses. */
export const ACT_INDEX = 4;
export const LEARN_INDEX = 5;
export const PERCEIVE_INDEX = 1;

/** Height of the HELIX core and its ring loop above the floor. */
export const CORE_Y = 1.7;
/** Radius of the loop the six Stage_Rings sit on. */
export const RING_R = 3;
/** Ring angles (degrees, counter-clockwise seen from above, 0 = +x). SENSE on the left,
 *  round the back to ACT at the front right; LEARN closes the loop at the front left. */
const RING_DEG = [180, 120, 60, 0, -60, -120] as const;

const rad = (d: number) => (d * Math.PI) / 180;

/** Point on the ring loop at angle ``deg``; +z is towards the default camera. */
export function loopPoint(deg: number, y = CORE_Y): Vec3 {
  const a = rad(deg);
  return [RING_R * Math.cos(a), y, -RING_R * Math.sin(a)];
}

export const RING_POS: readonly Vec3[] = RING_DEG.map((d) => loopPoint(d));
/** Rotation about +y that turns a torus (axis +z) so its axis is the loop tangent there. */
export const RING_ROT_Y: readonly number[] = RING_DEG.map((d) => rad(d));

/** Position on the SENSE..ACT arc for stage coordinate ``s`` (0 = SENSE ring, 4 = ACT ring). */
export function stagePoint(s: number): Vec3 {
  const c = Math.max(0, Math.min(ACT_INDEX, s));
  return loopPoint(RING_DEG[0] - 60 * c);
}

export type FigureKind = "person" | "bank" | "merchant";

export interface Figure {
  name: string;
  kind: FigureKind;
  pos: Vec3;
}

/** Senders on the left. Alice..Erin are the README walkthrough's accounts. */
export const SENDERS: readonly Figure[] = [
  { name: "Alice", kind: "person", pos: [-7.2, 0, -3.4] },
  { name: "Bob", kind: "person", pos: [-7.9, 0, -1.4] },
  { name: "Carl", kind: "person", pos: [-8.2, 0, 0.6] },
  { name: "Dave", kind: "person", pos: [-7.9, 0, 2.6] },
  { name: "Erin", kind: "person", pos: [-7.2, 0, 4.6] },
  { name: "Bank", kind: "bank", pos: [-10.4, 0, -2.4] },
  { name: "Merchant", kind: "merchant", pos: [-10.4, 0, 2.6] },
];
export const SENDER_INDEX = Object.fromEntries(SENDERS.map((f, i) => [f.name, i])) as Record<string, number>;

/** Receivers on the right: legit payments end here. */
export const RECEIVERS: readonly Figure[] = [
  { name: "Shop", kind: "merchant", pos: [8.4, 0, -4] },
  { name: "Payee", kind: "person", pos: [7.4, 0, -2] },
  { name: "Payee", kind: "person", pos: [8.0, 0, 0] },
  { name: "Bank", kind: "bank", pos: [10.2, 0, -1.4] },
];

export const INVESTIGATOR_POS: Vec3 = [6.6, 0, 7.2];
export const DESK_POS: Vec3 = [6.6, 0, 5.9];
/** The investigator's "CASE QUEUE" screen, at the left end of the desk so the
 *  investigator's name label does not cover it. */
export const SCREEN_POS: Vec3 = [4.9, 1.55, 5.6];
export const ATTACKER_POS: Vec3 = [-6.8, 0, -6.6];
export const CANARY_POS: Vec3 = [6.2, 1.3, -6];
/** Where suspicious particles gather into a Case_Card, just past the ACT ring. */
export const CASE_FORM_POS: Vec3 = [4.4, CORE_Y + 0.4, 4.2];
/** World-size multiplier of a Case_Card (its base plane is 1.3 x 0.8125). */
export const CARD_SIZE = 1.5;
/** Desk slots for Case_Cards, three per row, two rows. */
export const DESK_SLOTS = 6;
export function deskSlotPos(slot: number): Vec3 {
  const k = Math.max(0, Math.min(DESK_SLOTS - 1, slot));
  const col = k % 3;
  const row = Math.floor(k / 3);
  return [DESK_POS[0] - 2.05 + col * 2.05, 2.9 + row * 1.35, DESK_POS[2] - 0.9];
}

/** Chest height of a figure: where its particles start or end. */
export const CHEST_Y = 1.05;
export function chest(f: Figure | Vec3): Vec3 {
  const p = "pos" in f ? f.pos : f;
  return [p[0], CHEST_Y, p[2]];
}

/** Quadratic Bezier from ``a`` to ``b`` lifted by ``lift`` at the middle. */
export function arcPoint(a: Vec3, b: Vec3, lift: number, t: number): Vec3 {
  const u = Math.max(0, Math.min(1, t));
  const c: Vec3 = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2 + lift, (a[2] + b[2]) / 2];
  const w0 = (1 - u) * (1 - u);
  const w1 = 2 * (1 - u) * u;
  const w2 = u * u;
  return [w0 * a[0] + w1 * c[0] + w2 * b[0], w0 * a[1] + w1 * c[1] + w2 * b[1], w0 * a[2] + w1 * c[2] + w2 * b[2]];
}

/** ``n + 1`` points along an arc, for drawing the static path lines. */
export function arcPoints(a: Vec3, b: Vec3, lift: number, n = 24): Vec3[] {
  return Array.from({ length: n + 1 }, (_, i) => arcPoint(a, b, lift, i / n));
}

export const INBOUND_LIFT = 2.2;
export const OUTBOUND_LIFT = 1.8;
export const MERGE_LIFT = 0.6;
export const CARD_LIFT = 1.2;
export const PROBE_LIFT = 5;
export const PULSE_LIFT = 2.4;

/** Learning_Pulse route: Investigator -> LEARN ring -> PERCEIVE ring (Req 43.5). */
export function pulsePoint(t: number): Vec3 {
  const head: Vec3 = [INVESTIGATOR_POS[0], 1.9, INVESTIGATOR_POS[2]];
  const learn = RING_POS[LEARN_INDEX]!;
  const perceive = RING_POS[PERCEIVE_INDEX]!;
  return t < 0.5 ? arcPoint(head, learn, PULSE_LIFT, t * 2) : arcPoint(learn, perceive, 1.2, (t - 0.5) * 2);
}

import fc from "fast-check";
import { describe, expect, it } from "vitest";
import { ACT_INDEX, LEARN_INDEX, PERCEIVE_INDEX } from "./layout";
import {
  PARTICLE_CAP,
  bandTier,
  initialSim,
  isFlagged,
  outcomeFor,
  particlePosition,
  step,
  type SimEvent,
  type SimState,
} from "./simModel";

const conserved = (s: SimState) => s.counts.spawned === s.particles.length + s.counts.delivered + s.counts.cased + s.counts.blocked;

/** Run ``seconds`` in ``dt`` steps. */
function run(s: SimState, seconds: number, dt = 0.05): SimState {
  let out = s;
  for (let t = 0; t < seconds - 1e-9; t += dt) out = step(out, dt);
  return out;
}

const caseId = fc.constantFrom("C-1", "C-2", "C-3");
const spawn = fc.record({
  type: fc.constant("spawn" as const),
  kind: fc.constantFrom("tx" as const, "attack" as const, "probe" as const),
  outcome: fc.constantFrom("legit" as const, "suspicious" as const),
  risk: fc.double({ min: 0, max: 1, noNaN: true }),
  caseId: fc.option(caseId, { nil: null }),
  group: fc.option(fc.constant("g"), { nil: null }),
  target: fc.integer({ min: 0, max: 6 }),
});
const event: fc.Arbitrary<SimEvent> = fc.oneof(
  { weight: 6, arbitrary: spawn },
  { weight: 2, arbitrary: fc.record({ type: fc.constant("advance" as const), group: fc.constant("g"), target: fc.integer({ min: 0, max: 6 }) }) },
  { weight: 1, arbitrary: fc.record({ type: fc.constant("case" as const), caseId, tier: fc.constantFrom("LOW" as const, "HIGH" as const, "CRITICAL" as const) }) },
  {
    weight: 1,
    arbitrary: fc.record({
      type: fc.constant("label" as const),
      caseId,
      label: fc.constantFrom("CONFIRMED_FRAUD" as const, "FALSE_POSITIVE" as const),
      weightDelta: fc.option(fc.double({ min: -0.1, max: 0.1, noNaN: true }), { nil: null }),
      note: fc.constant("n"),
    }),
  },
  { weight: 1, arbitrary: fc.constant({ type: "antibody" as const }) },
);
const ops = (maxDt: number) => fc.array(fc.record({ dt: fc.double({ min: 0, max: maxDt, noNaN: true }), events: fc.array(event, { maxLength: 6 }) }), { maxLength: 60 });

describe("simModel", () => {
  it("conserves particles: spawned = active + delivered + cased + blocked, and drains completely", () => {
    // **Validates: Requirements 43.2, 43.15**
    fc.assert(
      fc.property(ops(0.8), (seq) => {
        let s = initialSim();
        for (const o of seq) {
          s = step(s, o.dt, o.events);
          expect(conserved(s)).toBe(true);
          expect(s.particles.length).toBeLessThanOrEqual(PARTICLE_CAP);
        }
        // Release every held particle and let the scene empty.
        s = run(step(s, 0, [{ type: "advance", group: "g", target: 9 }]), 15, 0.25);
        expect(s.particles).toHaveLength(0);
        expect(conserved(s)).toBe(true);
      }),
      { numRuns: 120 },
    );
  });

  it("moves every particle through SENSE, PERCEIVE, REMEMBER, REASON and ACT in order", () => {
    // **Validates: Requirements 43.2**
    fc.assert(
      fc.property(
        fc.array(
          fc.record({
            dt: fc.double({ min: 0.001, max: 0.1, noNaN: true }),
            n: fc.integer({ min: 0, max: 3 }),
            risk: fc.double({ min: 0, max: 1, noNaN: true }),
          }),
          { minLength: 1, maxLength: 80 },
        ),
        (seq) => {
          let s = initialSim();
          const seen = new Map<number, number[]>();
          const record = () => {
            for (const p of s.particles) {
              const v = seen.get(p.id) ?? [];
              if (p.visited >= 0 && v[v.length - 1] !== p.visited) v.push(p.visited);
              seen.set(p.id, v);
              if (p.phase === "outbound" || p.phase === "merging") expect(p.visited).toBe(ACT_INDEX);
              if (p.phase === "stages") expect(p.s).toBeGreaterThanOrEqual(0);
            }
          };
          for (const o of seq) {
            const events: SimEvent[] = Array.from({ length: o.n }, () => ({ type: "spawn", outcome: outcomeFor(o.risk), risk: o.risk }));
            s = step(s, o.dt, events);
            record();
          }
          for (let i = 0; i < 80; i++) {
            s = step(s, 0.1);
            record();
          }
          for (const v of seen.values()) {
            // Rings are reached one at a time, in order, with none skipped.
            v.forEach((ring, k) => expect(ring).toBe(k));
            expect(v.length).toBeLessThanOrEqual(ACT_INDEX + 1);
          }
        },
      ),
      { numRuns: 80 },
    );
  });

  it("caps active particles at PARTICLE_CAP and counts the rest as dropped", () => {
    const burst: SimEvent[] = Array.from({ length: PARTICLE_CAP + 50 }, () => ({ type: "spawn", outcome: "legit", risk: 0.1 }));
    const s = step(initialSim(), 0.01, burst);
    expect(s.particles).toHaveLength(PARTICLE_CAP);
    expect(s.counts.dropped).toBe(50);
    expect(conserved(s)).toBe(true);
  });

  it("delivers a legit particle to its receiver and turns a suspicious one into a Case card", () => {
    let s = step(initialSim(), 0, [
      { type: "spawn", outcome: "legit", risk: 0.2, receiver: 2 },
      { type: "spawn", outcome: "suspicious", risk: 0.973, caseId: "C-1", tier: "CRITICAL" },
    ]);
    s = run(s, 3.7);
    const sus = s.particles.find((p) => p.outcome === "suspicious")!;
    expect(isFlagged(sus)).toBe(true);
    s = run(s, 1.5);
    expect(s.counts.delivered).toBe(1);
    expect(s.receiverFlash[2]).toBeGreaterThan(0); // the ✓ on receiver 2 lights up
    expect(s.counts.cased).toBe(1);
    expect(s.cases).toHaveLength(1);
    expect(s.cases[0]).toMatchObject({ id: "C-1", tier: "CRITICAL", particles: 1 });
    s = run(s, 4.5);
    expect(s.cases[0]!.phase).toBe("desk");
  });

  it("flips a labelled card, sends a Learning_Pulse through LEARN and changes the PERCEIVE weight scale", () => {
    let s = step(initialSim(), 0, [{ type: "case", caseId: "C-9", tier: "HIGH", risk: 0.85 }]);
    s = run(s, 3.2);
    expect(s.cases[0]!.phase).toBe("desk");
    s = step(s, 0.05, [{ type: "label", caseId: "C-9", label: "FALSE_POSITIVE", weightDelta: -0.05, note: "down" }]);
    expect(s.cases[0]).toMatchObject({ phase: "flipping", label: "FALSE_POSITIVE" });
    s = run(s, 1);
    expect(s.cases[0]!.phase).toBe("labelled");
    expect(s.pulses).toHaveLength(1);
    let learnLit = false;
    for (let i = 0; i < 60 && s.pulses.length; i++) {
      s = step(s, 0.05);
      learnLit ||= (s.glow[LEARN_INDEX] ?? 0) > 0.9;
    }
    expect(learnLit).toBe(true);
    expect(s.weightScale).toBeCloseTo(0.95, 9);
    expect(s.weightNote).toBe("down");
    expect(s.glow[PERCEIVE_INDEX]).toBeGreaterThan(0.9);
  });

  it("lets an attack through without an Antibody and blocks it at PERCEIVE with one", () => {
    const attack: SimEvent = { type: "spawn", kind: "attack", outcome: "legit", risk: 0.3 };
    let s = run(step(initialSim(), 0, [attack]), 7);
    expect(s.counts.delivered).toBe(1);
    s = run(step(s, 0, [{ type: "antibody" }, attack]), 7);
    expect(s.counts.blocked).toBe(1);
    expect(s.antibodies).toBe(1);
  });

  it("maps risk to outcome and tier with the default bands", () => {
    expect(outcomeFor(0.59)).toBe("legit");
    expect(outcomeFor(0.6)).toBe("suspicious");
    expect([0.5, 0.6, 0.7, 0.8, 0.85, 0.973].map(bandTier)).toEqual([null, "LOW", "MEDIUM", "HIGH", "HIGH", "CRITICAL"]);
  });

  it("gives every particle a finite position", () => {
    let s = step(initialSim(), 0, [
      { type: "spawn", outcome: "legit", risk: 0.1 },
      { type: "spawn", kind: "probe", outcome: "suspicious", risk: 0.9 },
      { type: "spawn", kind: "attack", outcome: "legit", risk: 0.3 },
    ]);
    for (let i = 0; i < 40; i++) {
      s = step(s, 0.1);
      for (const p of s.particles) expect(particlePosition(p).every(Number.isFinite)).toBe(true);
    }
  });
});

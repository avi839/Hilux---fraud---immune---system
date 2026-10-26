import fc from "fast-check";
import { describe, expect, it } from "vitest";
import { STAGES } from "./layout";
import {
  LOOP_S,
  STORY_CUES,
  STORY_STEPS,
  cuesBetween,
  overlayNoteAt,
  simulateStory,
  stageIndex,
  stepIndexAt,
  viewDefaults,
} from "./storyScript";

const textOf = (from: number, to: number) =>
  STORY_STEPS.filter((s) => s.t >= from && s.t < to)
    .map((s) => s.text)
    .join(" ");

describe("storyScript", () => {
  it("is a monotonic timeline inside a 60-90 s loop", () => {
    expect(LOOP_S).toBeGreaterThanOrEqual(60);
    expect(LOOP_S).toBeLessThanOrEqual(90);
    expect(STORY_STEPS[0]!.t).toBe(0);
    for (let i = 1; i < STORY_STEPS.length; i++) expect(STORY_STEPS[i]!.t).toBeGreaterThan(STORY_STEPS[i - 1]!.t);
    expect(STORY_STEPS.at(-1)!.t).toBeLessThan(LOOP_S);
    for (let i = 1; i < STORY_CUES.length; i++) expect(STORY_CUES[i]!.t).toBeGreaterThanOrEqual(STORY_CUES[i - 1]!.t);
  });

  it("covers all six stages, first reached in order SENSE..LEARN, and Alice's walk is in order", () => {
    const first = STAGES.map((s) => STORY_STEPS.findIndex((x) => x.stage === s));
    expect(first.every((i) => i >= 0)).toBe(true);
    for (let k = 1; k < first.length; k++) expect(first[k]!).toBeGreaterThan(first[k - 1]!);
    const alice = STORY_STEPS.filter((s) => s.tx === "T007").map((s) => stageIndex(s.stage));
    expect(alice).toEqual([0, 1, 2, 3, 4]);
  });

  it("maps time to steps monotonically", () => {
    // **Validates: Requirements 43.6, 43.12**
    fc.assert(
      fc.property(fc.double({ min: 0, max: LOOP_S, noNaN: true }), fc.double({ min: 0, max: LOOP_S, noNaN: true }), (a, b) => {
        const [lo, hi] = a <= b ? [a, b] : [b, a];
        expect(stepIndexAt(lo)).toBeLessThanOrEqual(stepIndexAt(hi));
        expect(STORY_STEPS[stepIndexAt(lo)]!.t).toBeLessThanOrEqual(lo + 1e-9);
      }),
    );
  });

  it("uses the README walkthrough's numbers", () => {
    const alice = textOf(10, 36);
    for (const s of ["4,800.00 EUR", "Lagos", "0.70", "0.638", "0.949", "0.973", "CRITICAL", "C-1", "ABRUPT_BREAK"]) expect(alice).toContain(s);
    const bob = textOf(36, 54);
    for (const s of ["9,800", "9,650", "9,900", "STRUCTURING", "0.80", "HIGH", "C-2", "29,350.00"]) expect(bob).toContain(s);
    const mule = textOf(54, LOOP_S);
    for (const s of ["Carl → Dave → Erin → Finn", "MULE_CHAIN", "0.85", "HIGH", "C-3", "13,900.00"]) expect(mule).toContain(s);
    expect(STORY_STEPS.map((s) => s.case?.id).filter(Boolean)).toEqual(["C-1", "C-1", "C-2", "C-3"]);
    expect(STORY_STEPS.find((s) => s.case?.id === "C-1")!.case).toEqual({ id: "C-1", tier: "CRITICAL", risk: 0.973 });
  });

  it("plays the scripted loop: three Cases, C-1 labelled with a learning pulse, particles conserved", () => {
    const at34 = simulateStory(34);
    expect(at34.counts.cased).toBe(1);
    expect(at34.weightNote).toMatch(/C-1 confirmed fraud/);
    expect(at34.weightScale).toBe(1); // already at the 1.00 ceiling (README)
    const c1 = simulateStory(31.5).cases.find((c) => c.id === "C-1")!;
    expect(c1).toMatchObject({ tier: "CRITICAL", label: "CONFIRMED_FRAUD" });

    const end = simulateStory(LOOP_S - 0.1);
    expect(end.cases.map((c) => [c.id, c.tier, c.phase])).toEqual([
      ["C-2", "HIGH", "desk"],
      ["C-3", "HIGH", "desk"],
    ]);
    expect(end.counts.cased).toBe(7); // T007 + T010-T012 + T013-T015
    expect(end.counts.spawned).toBe(end.particles.length + end.counts.delivered + end.counts.cased + end.counts.blocked);
  });

  it("adds the overlay events only when the overlay is on", () => {
    const off = simulateStory(40);
    const on = simulateStory(40, { redBlue: true, soc: true });
    expect(off.antibodies).toBe(0);
    expect(on.antibodies).toBe(1);
    expect(on.counts.blocked).toBe(4); // three attacks stopped at PERCEIVE + the canary probe
    expect(simulateStory(67, { redBlue: false, soc: true }).cases.find((c) => c.id === "C-3")!.hold).toBe(true);
    expect(simulateStory(67).cases.find((c) => c.id === "C-3")!.hold).toBe(false);
    expect(cuesBetween(-1, LOOP_S).length).toBeLessThan(cuesBetween(-1, LOOP_S, { redBlue: true, soc: true }).length);
    expect(overlayNoteAt(32, { redBlue: true, soc: false })!.text).toMatch(/blocks/);
    expect(overlayNoteAt(32, { redBlue: false, soc: false })).toBeNull();
  });

  it("starts in Story_Mode and never autoplays under reduced motion", () => {
    // **Validates: Requirements 43.7, 43.14**
    fc.assert(
      fc.property(fc.boolean(), (reduced) => {
        const d = viewDefaults(reduced);
        expect(d.mode).toBe("story");
        expect(d.playing).toBe(!reduced);
      }),
    );
  });
});

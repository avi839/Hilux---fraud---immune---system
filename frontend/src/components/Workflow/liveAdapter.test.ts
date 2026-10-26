import fc from "fast-check";
import { describe, expect, it } from "vitest";
import { caseSummary, scored } from "../../test/fixtures";
import { toFeedItem, type FeedItem } from "../../store/reducers";
import { MAX_SPAWN_PER_S, adaptLive, initialLiveState, shortTxId, type LiveAdapterState } from "./liveAdapter";
import { PARTICLE_CAP } from "./simModel";

const item = (seq: number, risk: number, case_id: string | null = null): FeedItem => ({ ...toFeedItem(scored(seq, {}, risk)), case_id });
const newestFirst = (items: FeedItem[]) => [...items].sort((a, b) => b.seq - a.seq);

function primed(now = 0): LiveAdapterState {
  return adaptLive(initialLiveState(), { feed: [], cases: {}, now, active: 0 }).state;
}

describe("liveAdapter", () => {
  it("shortens long generator ids for the 3D tags and keeps short ids", () => {
    expect(shortTxId("GEN-42-000000001442")).toBe("…001442");
    expect(shortTxId("T007")).toBe("T007");
  });

  it("treats what is already in the store as history when Live_Mode starts", () => {
    const out = adaptLive(initialLiveState(), { feed: [item(1, 0.9)], cases: { "C-1": caseSummary("C-1") }, now: 0, active: 0 });
    expect(out.events).toEqual([]);
    expect(out.state.lastSeq).toBe(1);
  });

  it("never exceeds the rate or particle cap and sends risk >= 0.6 down the suspicious path", () => {
    // **Validates: Requirements 43.8, 43.15**
    fc.assert(
      fc.property(
        fc.array(
          fc.record({
            gapMs: fc.integer({ min: 0, max: 2000 }),
            risks: fc.array(fc.double({ min: 0, max: 1, noNaN: true }), { maxLength: 80 }),
            active: fc.integer({ min: 0, max: PARTICLE_CAP }),
          }),
          { maxLength: 30 },
        ),
        (ticks) => {
          let s = primed();
          let now = 0;
          let seq = 0;
          let feed: FeedItem[] = [];
          let total = 0;
          const spawnsAt: number[] = [];
          for (const t of ticks) {
            now += t.gapMs;
            const batch = t.risks.map((r) => item(++seq, r));
            feed = newestFirst([...batch, ...feed]).slice(0, 200);
            const out = adaptLive(s, { feed, cases: {}, now, active: t.active });
            const spawns = out.events.filter((e) => e.type === "spawn");
            expect(t.active + spawns.length).toBeLessThanOrEqual(PARTICLE_CAP);
            for (const e of spawns) {
              if (e.type !== "spawn") continue;
              expect(e.outcome).toBe(e.risk >= 0.6 ? "suspicious" : "legit");
              spawnsAt.push(now);
            }
            // Every new frame is either drawn or counted as not drawn.
            expect(out.state.drawn + out.state.dropped - (s.drawn + s.dropped)).toBe(batch.length);
            total += batch.length;
            s = out.state;
          }
          expect(s.drawn + s.dropped).toBe(total);
          // Token bucket: in any window of w ms at most capacity + rate * w spawns.
          for (let i = 0; i < spawnsAt.length; i++) {
            let j = i;
            while (j < spawnsAt.length && spawnsAt[j]! - spawnsAt[i]! <= 1000) j++;
            expect(j - i).toBeLessThanOrEqual(2 * MAX_SPAWN_PER_S);
          }
        },
      ),
      { numRuns: 150 },
    );
  });

  it("samples a burst, drawing suspicious frames first and reporting the rest as not drawn", () => {
    let s = primed(0);
    const burst = Array.from({ length: 50 }, (_, i) => item(i + 1, i % 10 === 0 ? 0.95 : 0.2));
    const out = adaptLive(s, { feed: newestFirst(burst), cases: {}, now: 0, active: 0 });
    const spawns = out.events.filter((e) => e.type === "spawn");
    expect(spawns).toHaveLength(MAX_SPAWN_PER_S);
    expect(spawns.filter((e) => e.type === "spawn" && e.outcome === "suspicious")).toHaveLength(5);
    expect(out.state.dropped).toBe(30);
    s = out.state;
    // A second later the bucket has refilled.
    const next = adaptLive(s, { feed: newestFirst([...burst, item(51, 0.1)]), cases: {}, now: 1000, active: 0 });
    expect(next.events).toHaveLength(1);
  });

  it("uses the event's risk and its Case tier for a suspicious particle", () => {
    const s = primed();
    const cases = { "C-7": caseSummary("C-7", { tier: "CRITICAL" }) };
    s.cases["C-7"] = { updated: cases["C-7"].updated_at, tier: "CRITICAL", label: null };
    const out = adaptLive(s, { feed: [item(5, 0.91, "C-7")], cases, now: 100, active: 0 });
    expect(out.events).toEqual([
      expect.objectContaining({ type: "spawn", outcome: "suspicious", risk: 0.91, tier: "CRITICAL", caseId: "C-7", txId: "T-5" }),
    ]);
    const legit = adaptLive(out.state, { feed: [item(6, 0.3, "C-7"), item(5, 0.91, "C-7")], cases, now: 200, active: 0 });
    expect(legit.events).toEqual([expect.objectContaining({ outcome: "legit", tier: null, caseId: null })]);
  });

  it("creates a card for each new Case and a pulse for each new Feedback_Label", () => {
    let s = primed();
    const c = caseSummary("C-4", { tier: "HIGH" });
    let out = adaptLive(s, { feed: [], cases: { "C-4": c }, now: 10, active: 0 });
    expect(out.events).toEqual([{ type: "case", caseId: "C-4", tier: "HIGH", risk: c.risk }]);
    s = out.state;
    out = adaptLive(s, { feed: [], cases: { "C-4": c }, now: 20, active: 0 });
    expect(out.events).toEqual([]);
    const labelled = { ...c, label: "FALSE_POSITIVE" as const, status: "CLOSED" as const, updated_at: "2025-03-14T10:30:00+00:00" };
    out = adaptLive(out.state, { feed: [], cases: { "C-4": labelled }, now: 30, active: 0 });
    expect(out.events).toEqual([expect.objectContaining({ type: "label", caseId: "C-4", label: "FALSE_POSITIVE", weightDelta: null })]);
    out = adaptLive(out.state, { feed: [], cases: { "C-4": labelled }, now: 40, active: 0 });
    expect(out.events).toEqual([]);
  });

  it("reports the weight change of a label when the server reports Sensor_Weights", () => {
    const s = adaptLive(initialLiveState(), { feed: [], cases: {}, weights: { amount: 1, geo: 1, velocity: 1 }, now: 0, active: 0 }).state;
    const c = caseSummary("C-1", { label: "FALSE_POSITIVE" });
    const out = adaptLive(s, { feed: [], cases: { "C-1": c }, weights: { amount: 0.95, geo: 0.95, velocity: 1 }, now: 10, active: 0 });
    const label = out.events.find((e) => e.type === "label");
    expect(label).toMatchObject({ weightDelta: expect.closeTo(-0.05, 9) });
    expect(label && label.type === "label" ? label.note : "").toContain("amount 1.00→0.95");
  });
});

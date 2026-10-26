import { describe, expect, it } from "vitest";
import { aliceEvidence, bobEvidence } from "../test/sampleRun";
import { aliceTimeline } from "../test/vizFixtures";
import { clampT, entriesAt, eventStops, evidenceAt, frameParam, stepT, timelineSpan } from "./replay";
import { toMs } from "./time";

const span = timelineSpan(aliceTimeline)!;
const stops = eventStops(aliceTimeline.entries, span);

describe("replay position", () => {
  it("spans the timeline and stops at each event", () => {
    expect(span).toEqual({ startMs: toMs(aliceTimeline.start), endMs: toMs(aliceTimeline.end) });
    expect(stops).toEqual(aliceTimeline.entries.map((e) => toMs(e.event.ts)));
    expect(timelineSpan({ start: "2024-01-02T00:00:00Z", end: "2024-01-01T00:00:00Z" })).toBeNull();
    expect(clampT(0, span)).toBe(span.startMs);
  });

  it("steps to the neighbouring event, then to the span ends", () => {
    expect(stepT(stops, span.startMs, 1, span)).toBe(stops[0]);
    expect(stepT(stops, stops[1]!, 1, span)).toBe(stops[2]);
    expect(stepT(stops, stops[1]!, -1, span)).toBe(stops[0]);
    expect(stepT(stops, stops[0]!, -1, span)).toBe(span.startMs);
    expect(stepT([], span.startMs, 1, span)).toBe(span.endMs);
  });

  it("sends the server's own strings at the span ends so sub-millisecond digits are not lost", () => {
    const tl = { start: "2024-06-04T11:20:00.000123+00:00", end: "2024-06-05T11:20:00.999999+00:00" };
    const s = timelineSpan(tl)!;
    expect(frameParam(s.startMs, tl)).toBe(tl.start);
    expect(frameParam(s.endMs, tl)).toBe(tl.end);
    expect(frameParam(s.startMs + 1000, tl)).toBe("2024-06-04T11:20:01.000Z");
  });

  it("filters timeline entries to ts at or before T", () => {
    expect(entriesAt(aliceTimeline.entries, null)).toHaveLength(4);
    expect(entriesAt(aliceTimeline.entries, toMs("2024-06-05T10:40:00+00:00")).map((e) => e.event.tx_id)).toEqual(["T004", "T005", "T006"]);
    expect(entriesAt(aliceTimeline.entries, span.startMs)).toEqual([]);
  });
});

describe("evidenceAt", () => {
  it("returns the evidence unchanged without a replay position", () => {
    expect(evidenceAt(aliceEvidence, null).evidence).toBe(aliceEvidence);
  });

  it("hides chain entries, reasons and notes of transactions after T", () => {
    const r = evidenceAt(aliceEvidence, toMs("2024-06-05T11:00:00+00:00"));
    expect(r.kept).toBe(0);
    expect(r.total).toBe(aliceEvidence.chain!.entries.length);
    expect(r.evidence.explanation.reasons).toEqual([]);
    expect(r.evidence.explanation.notes).toEqual([]);
    expect(r.evidence.explanation.summary).toBe(aliceEvidence.explanation.summary);
  });

  it("keeps Bob's T010-T011 evidence at T011 but not the motif recorded at T012", () => {
    const r = evidenceAt(bobEvidence, toMs("2024-06-05T15:30:00+00:00"));
    const txs = new Set(r.evidence.chain!.entries.map((e) => e.tx_id));
    expect([...txs].sort()).toEqual(["T010", "T011"]);
    expect(r.evidence.chain!.entries.some((e) => e.kind === "MOTIF")).toBe(false);
    for (const e of r.evidence.chain!.entries) expect(toMs(e.ts)).toBeLessThanOrEqual(toMs("2024-06-05T15:30:00+00:00"));
  });
});

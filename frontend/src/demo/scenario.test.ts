import fc from "fast-check";
import { describe, expect, it } from "vitest";
import { caseSummary } from "../test/fixtures";
import { independentGroups, prepareScenario, runTag, SAMPLE_JSONL, scenarioCases } from "./scenario";

const NOW = Date.parse("2026-10-25T10:00:00.000Z");
const TAG = "-DTEST";

describe("prepareScenario on docs/sample_transactions.jsonl", () => {
  const lines = prepareScenario(SAMPLE_JSONL, NOW, TAG);
  const docs = lines.map((l) => JSON.parse(l.body) as Record<string, string>);

  it("keeps all 15 lines in file order, including the duplicate and the invalid one", () => {
    expect(lines.map((l) => l.line)).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]);
    expect(lines.filter((l) => l.txId === `T007${TAG}`)).toHaveLength(2);
    const invalid = docs.find((d) => d.tx_id === `T009${TAG}`)!;
    expect(invalid.amount).toBe("-20.00");
    expect(invalid.currency).toBe("euro");
  });

  it("suffixes tx_id, src and dst and leaves every other field unchanged", () => {
    const t007 = docs[6]!;
    expect(t007).toMatchObject({
      tx_id: `T007${TAG}`,
      src: `ACC-ALICE${TAG}`,
      dst: `ACC-UNKNOWN-77${TAG}`,
      amount: "4800.00",
      currency: "EUR",
      channel: "online",
      lat: 6.52,
      lon: 3.38,
      device_id: "DEV-X9",
    });
  });

  it("shifts time so the newest line is now and the gaps are unchanged", () => {
    const ts = docs.map((d) => Date.parse(d.ts!));
    expect(Math.max(...ts)).toBe(NOW);
    // Alice: T005 Berlin 10:10, T007 Lagos 11:20 -> 70 minutes apart, as in the file.
    expect(ts[6]! - ts[4]!).toBe(70 * 60_000);
    // Mule chain T013 -> T015: 48 minutes.
    expect(ts[14]! - ts[12]!).toBe(48 * 60_000);
  });
});

describe("independentGroups", () => {
  it("splits the sample into its three stories, each in file order", () => {
    const groups = independentGroups(prepareScenario(SAMPLE_JSONL, NOW, TAG)).map((g) => g.map((l) => l.line));
    expect(groups).toEqual([
      [1, 2, 3, 4, 5, 6, 7, 8, 9],
      [10, 11, 12],
      [13, 14, 15],
    ]);
  });

  it("joins lines transitively through shared accounts and isolates non-JSON lines", () => {
    const text = ['{"src":"A","dst":"B"}', "oops", '{"src":"C","dst":"D"}', '{"src":"B","dst":"C"}'].join("\n");
    const groups = independentGroups(prepareScenario(text, NOW, "")).map((g) => g.map((l) => l.line));
    expect(groups).toEqual([[1, 3, 4], [2]]);
  });
});

describe("prepareScenario edge cases", () => {
  it("skips blank lines and sends a non-JSON line verbatim", () => {
    const out = prepareScenario('\n{"tx_id":"A","ts":"2024-01-01T00:00:00Z"}\n\nnot json\n', NOW, TAG);
    expect(out).toEqual([
      { line: 2, txId: `A${TAG}`, body: JSON.stringify({ tx_id: `A${TAG}`, ts: new Date(NOW).toISOString() }) },
      { line: 4, txId: null, body: "not json" },
    ]);
  });

  it("runTag is -D plus 4 upper-case base-36 characters", () => {
    expect(runTag(NOW)).toMatch(/^-D[0-9A-Z]{4}$/);
  });
});

describe("prepareScenario properties", () => {
  // Timestamps within ±20 years of 2024, at whole milliseconds.
  const ms = fc.integer({ min: Date.parse("2004-01-01T00:00:00Z"), max: Date.parse("2044-01-01T00:00:00Z") });
  const event = fc.record({ tx_id: fc.stringMatching(/^[A-Z0-9-]{1,12}$/), ts: ms, amount: fc.constantFrom("1.00", "9800.00") });

  it("shifts every timestamp by one constant so the newest is now, and suffixes every id", () => {
    fc.assert(
      fc.property(fc.array(event, { minLength: 1, maxLength: 30 }), ms, (events, now) => {
        const text = events.map((e) => JSON.stringify({ ...e, src: "S", dst: "D", ts: new Date(e.ts).toISOString() })).join("\n");
        const out = prepareScenario(text, now, TAG).map((l) => JSON.parse(l.body) as Record<string, string>);
        expect(out).toHaveLength(events.length);
        const shift = now - Math.max(...events.map((e) => e.ts));
        out.forEach((d, i) => {
          expect(Date.parse(d.ts!)).toBe(events[i]!.ts + shift);
          expect(d.tx_id).toBe(events[i]!.tx_id + TAG);
          expect([d.src, d.dst]).toEqual([`S${TAG}`, `D${TAG}`]);
          expect(d.amount).toBe(events[i]!.amount);
        });
      }),
    );
  });
});

describe("scenarioCases", () => {
  it("finds one Case per story for this run's accounts, in story order, ignoring other runs", () => {
    const cases = {
      "C-1": caseSummary("C-1", { accounts: [`ACC-BOB${TAG}`, `ACC-SHELL-1${TAG}`], risk: 0.8 }),
      "C-2": caseSummary("C-2", { accounts: [`ACC-ALICE${TAG}`], risk: 0.97 }),
      "C-3": caseSummary("C-3", { accounts: [`ACC-CARL${TAG}`, `ACC-DAVE${TAG}`], risk: 0.85 }),
      "C-4": caseSummary("C-4", { accounts: ["ACC-ALICE-DOLD1"], risk: 0.99 }),
    };
    expect(scenarioCases(cases, TAG).map((c) => c.caseId)).toEqual(["C-2", "C-1", "C-3"]);
    expect(scenarioCases(cases, "-DNONE")).toEqual([]);
  });
});

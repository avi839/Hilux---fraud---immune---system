import { describe, expect, it } from "vitest";
import type { EvidenceChainDoc } from "../../api/types";
import { aliceEvidence, bobEvidence } from "../../test/sampleRun";
import { contributorBars, describeVacuousReason, discount, segmentPercents, uncertaintyConditions } from "./evidence";

/** Small deterministic PRNG so the randomised check is reproducible. */
function lcg(seed: number) {
  let s = seed >>> 0;
  return () => {
    s = (Math.imul(s, 1664525) + 1013904223) >>> 0;
    return s / 2 ** 32;
  };
}

describe("discounted masses", () => {
  it("f' = w·f, l' = w·l, Θ' = 1 − f' − l'", () => {
    const d = discount({ f: 0.6, l: 0.1, t: 0.3 }, 0.5);
    expect(d.f).toBeCloseTo(0.3, 12);
    expect(d.l).toBeCloseTo(0.05, 12);
    expect(d.theta).toBeCloseTo(0.65, 12);
  });

  it("segments always span exactly 100% for valid and malformed masses (2,000 random cases)", () => {
    const rnd = lcg(42);
    for (let i = 0; i < 2_000; i++) {
      const f = rnd();
      const l = rnd() * (1 - f);
      // Every 10th case is deliberately malformed (f + l > 1, weight out of range).
      const bad = i % 10 === 0;
      const mass = bad ? { f: rnd() * 2, l: rnd() * 2, t: -1 } : { f, l, t: 1 - f - l };
      const w = bad ? rnd() * 3 - 1 : rnd();
      const [pf, pl, pt] = segmentPercents(discount(mass, w));
      expect(pf).toBeGreaterThanOrEqual(0);
      expect(pl).toBeGreaterThanOrEqual(0);
      expect(pt).toBeGreaterThanOrEqual(0);
      expect(pf + pl + pt).toBeCloseTo(100, 9);
    }
  });
});

describe("contributorBars", () => {
  it("one bar per contributor in the explanation's order (Alice T007)", () => {
    const bars = contributorBars(aliceEvidence.chain, aliceEvidence.explanation);
    expect(bars.map((b) => [b.name, b.bucket])).toEqual([
      ["amount", "reason"],
      ["geo", "reason"],
      ["counterparty", "reason"],
      ["timing", "reason"],
      ["velocity", "mitigating"],
      ["drift", "insufficient"],
    ]);
    // Each reason's fraud segment equals the F' the explanation cites.
    for (const it of aliceEvidence.explanation.reasons) {
      expect(bars.find((b) => b.name === it.name)!.f).toBeCloseTo(it.f, 12);
    }
  });

  it("uses the representative entry: largest f' for a reason, first on ties", () => {
    const chain: EvidenceChainDoc = {
      schema: 1,
      case_id: "C-9",
      entries: [
        { ts: "2024-06-05T10:00:00+00:00", tx_id: "T1", contributor_id: "amount", kind: "CONTRIBUTOR", ctype: "SENSOR", mass: { f: 0.2, l: 0.5, t: 0.3 }, weight: 1, features: {}, detail: {} },
        { ts: "2024-06-05T11:00:00+00:00", tx_id: "T2", contributor_id: "amount", kind: "CONTRIBUTOR", ctype: "SENSOR", mass: { f: 0.8, l: 0.0, t: 0.2 }, weight: 0.5, features: {}, detail: {} },
        { ts: "2024-06-05T12:00:00+00:00", tx_id: "T3", contributor_id: "amount", kind: "CONTRIBUTOR", ctype: "SENSOR", mass: { f: 0.6, l: 0.0, t: 0.4 }, weight: 1, features: {}, detail: {} },
      ],
    };
    // F' = max(0.2, 0.4, 0.6) = 0.6 (T3); L' = max(0.5, 0, 0) = 0.5 → reason, shown by T3.
    const [bar] = contributorBars(chain);
    expect(bar).toMatchObject({ name: "amount", bucket: "reason", txId: "T3", txCount: 3, label: "Amount" });
    expect(bar!.f + bar!.l + bar!.theta).toBeCloseTo(1, 12);
  });
});

describe("uncertaintyConditions", () => {
  it("is empty when nothing was missing (Alice T007)", () => {
    expect(uncertaintyConditions(aliceEvidence.chain, aliceEvidence.explanation)).toEqual([]);
  });

  it("pairs missing fields and insufficient history with the sensor (Bob T010-T012)", () => {
    const conds = uncertaintyConditions(bobEvidence.chain, bobEvidence.explanation);
    expect(conds.filter((c) => c.sensor === "geo")).toEqual([
      { sensor: "geo", label: "Geo", reason: "missing field lat, lon", txIds: ["T010", "T011", "T012"] },
    ]);
    expect(conds.filter((c) => c.sensor === "amount").map((c) => [c.reason, c.txIds])).toEqual([
      ["insufficient history (0 prior events)", ["T010"]],
      ["insufficient history (1 prior events)", ["T011"]],
      ["insufficient history (2 prior events)", ["T012"]],
    ]);
    expect(new Set(conds.map((c) => c.sensor))).toEqual(new Set(["amount", "counterparty", "drift", "geo", "timing", "velocity"]));
  });

  it("falls back to MISSING_FIELD records and insufficient items when contributor details are absent", () => {
    const chain: EvidenceChainDoc = {
      schema: 1,
      case_id: "C-9",
      entries: [{ ts: "2024-06-05T10:00:00+00:00", tx_id: "T1", contributor_id: "~missing:lat", kind: "MISSING_FIELD", ctype: null, mass: null, weight: null, features: {}, detail: { field: "lat", sensors: "geo" } }],
    };
    const conds = uncertaintyConditions(chain, {
      case_id: "C-9",
      summary: "",
      reasons: [],
      mitigating: [],
      notes: [],
      message: null,
      insufficient: [
        { name: "timing", label: "Timing", ctype: "SENSOR", weight: 1, tx_ids: ["T1"], f: 0, l: 0, comparison: "insufficient history (2 prior events)", generic: false, text: "" },
        { name: "drift", label: "Drift", ctype: "SENSOR", weight: 1, tx_ids: ["T1"], f: 0, l: 0, comparison: "no decisive evidence (F' = L')", generic: false, text: "" },
      ],
    });
    expect(conds.map((c) => [c.label, c.reason, c.txIds])).toEqual([
      ["Geo", "missing field lat", ["T1"]],
      ["Timing", "insufficient history (2 prior events)", ["T1"]],
    ]);
  });

  it("words vacuous reasons like the Explanation_Engine", () => {
    expect(describeVacuousReason("missing:lat,lon")).toEqual(["missing field lat, lon"]);
    expect(describeVacuousReason("missing:dna.iat")).toEqual(["missing history for iat"]);
    expect(describeVacuousReason("history:3")).toEqual(["insufficient history (3 prior events)"]);
    expect(describeVacuousReason("timeout")).toEqual([]);
  });
});

import { describe, expect, it } from "vitest";
import { aliceEvidence, bobEvidence } from "../test/sampleRun";
import { aliceTimeline, bobTimeline } from "../test/vizFixtures";
import { buildGeo, eventsFromChain, eventsFromTimeline, formatSpeed, haversineKm, impliedSpeedKmh, type GeoEvent } from "./geo";
import { toMs } from "./time";

const geoFeatures = aliceEvidence.chain!.entries.find((e) => e.contributor_id === "geo")!.features as Record<string, number>;

describe("buildGeo", () => {
  it("maps Alice's located events, counts the one without location and joins them in ts order", () => {
    const g = buildGeo(eventsFromTimeline(aliceTimeline.entries, new Set(["T007"])));
    expect(g.points.map((p) => p.tx_id)).toEqual(["T004", "T005", "T007"]);
    expect(g.omitted).toBe(1);
    expect(g.total).toBe(4);
    expect(g.paths).toEqual([
      {
        account: "ACC-ALICE",
        txIds: ["T004", "T005", "T007"],
        path: [
          [13.41, 52.53],
          [13.4, 52.52],
          [3.38, 6.52],
        ],
      },
    ]);
    expect(g.points.find((p) => p.tx_id === "T007")?.inCase).toBe(true);
    expect(g.points.find((p) => p.tx_id === "T005")?.inCase).toBe(false);
  });

  it("computes the hop distance and implied speed exactly as the Geo sensor did for T007", () => {
    const g = buildGeo(eventsFromTimeline(aliceTimeline.entries));
    const hop = g.hops.find((h) => h.toTx === "T007")!;
    expect(hop.fromTx).toBe("T005");
    // The map line for this hop runs Berlin → Lagos as [lon, lat].
    expect([hop.from, hop.to]).toEqual([
      [13.4, 52.52],
      [3.38, 6.52],
    ]);
    expect(hop.distanceKm).toBeCloseTo(geoFeatures.distance_km!, 6);
    expect(hop.dtH).toBeCloseTo(geoFeatures.dt_h!, 9);
    expect(hop.speedKmh).toBeCloseTo(geoFeatures.speed_kmh!, 4);
    expect(formatSpeed(hop.speedKmh)).toBe("4454 km/h");
  });

  it("keeps one path per source account, sorted by ts then tx id, regardless of input order", () => {
    const ev: GeoEvent[] = [
      { tx_id: "B2", ts: "2024-01-01T02:00:00Z", account: "B", lat: 1, lon: 1 },
      { tx_id: "A2", ts: "2024-01-01T01:00:00Z", account: "A", lat: 0, lon: 1 },
      { tx_id: "B1", ts: "2024-01-01T02:00:00Z", account: "B", lat: 1, lon: 0 },
      { tx_id: "A1", ts: "2024-01-01T00:00:00Z", account: "A", lat: 0, lon: 0 },
      { tx_id: "C1", ts: "2024-01-01T00:00:00Z", account: "C", lat: 5, lon: 5 },
    ];
    const g = buildGeo(ev);
    expect(g.paths.map((p) => [p.account, p.txIds])).toEqual([
      ["A", ["A1", "A2"]],
      ["B", ["B1", "B2"]],
    ]);
    // Same timestamp, different place: infinite implied speed, as in the sensor.
    expect(g.hops.find((h) => h.account === "B")?.speedKmh).toBe(Infinity);
    expect(g.hops.filter((h) => h.account === "C")).toEqual([]);
  });

  it("omits invalid coordinates and excludes events after the replay position from every count", () => {
    const ev: GeoEvent[] = [
      { tx_id: "X1", ts: "2024-01-01T00:00:00Z", account: "A", lat: 91, lon: 0 },
      { tx_id: "X2", ts: "2024-01-01T01:00:00Z", account: "A", lat: Number.NaN, lon: 0 },
      { tx_id: "X3", ts: "2024-01-01T02:00:00Z", account: "A", lat: 10, lon: 10 },
      { tx_id: "X4", ts: "2024-01-01T03:00:00Z", account: "A" },
    ];
    expect(buildGeo(ev)).toMatchObject({ omitted: 3, total: 4 });
    const atT = buildGeo(ev, toMs("2024-01-01T02:00:00Z"));
    expect(atT).toMatchObject({ omitted: 2, total: 3 });
    expect(atT.points.map((p) => p.tx_id)).toEqual(["X3"]);
    const before = buildGeo(eventsFromTimeline(aliceTimeline.entries), toMs("2024-06-05T11:00:00+00:00"));
    expect(before.points.map((p) => p.tx_id)).toEqual(["T004", "T005"]);
    expect(before.omitted).toBe(1);
  });

  it("reports every event of Bob's case as omitted (no event has a location)", () => {
    expect(buildGeo(eventsFromTimeline(bobTimeline.entries))).toMatchObject({ omitted: 3, total: 3, points: [] });
  });
});

describe("eventsFromChain (timeline unavailable)", () => {
  it("lists each Case transaction once, located only when chain features carry lat/lon", () => {
    const ev = eventsFromChain(bobEvidence.chain);
    expect(ev.map((e) => e.tx_id)).toEqual(["T010", "T011", "T012"]);
    expect(buildGeo(ev)).toMatchObject({ omitted: 3, total: 3 });
    const located = eventsFromChain({
      schema: 1,
      case_id: "X",
      entries: [
        { ts: "2024-01-01T00:00:00Z", tx_id: "T1", contributor_id: "geo", kind: "CONTRIBUTOR", ctype: "SENSOR", mass: null, weight: 1, features: { lat: 1.5, lon: 2.5, src: "ACC-1" }, detail: {} },
      ],
    });
    expect(located).toEqual([{ tx_id: "T1", ts: "2024-01-01T00:00:00Z", account: "ACC-1", inCase: true, lat: 1.5, lon: 2.5 }]);
  });
});

describe("haversine and speed", () => {
  it("matches reference distances and the sensor's speed rules", () => {
    expect(haversineKm(0, 0, 0, 1)).toBeCloseTo(111.195, 2);
    expect(haversineKm(10, 10, 10, 10)).toBe(0);
    expect(impliedSpeedKmh(0, 0)).toBe(0);
    expect(impliedSpeedKmh(10, 0)).toBe(Infinity);
    expect(impliedSpeedKmh(100, -2)).toBe(50);
  });
});

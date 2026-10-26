import { describe, expect, it } from "vitest";
import type { SubgraphDoc, SubgraphEdgeDoc, SubgraphNodeDoc } from "../api/types";
import { bobEvidence } from "../test/sampleRun";
import { bobSubgraph, muleNeighbourhood } from "../test/vizFixtures";
import { MAX_GRAPH_NODES, mergeNeighbourhoods, motifEdgeIndex, parseTxIds, toGraph } from "./graph";
import { toMs } from "./time";

/** Deterministic PRNG (mulberry32) for the randomised checks. */
function rng(seed: number) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function randomSubgraph(r: () => number, nNodes: number, nEdges: number, nCase: number): SubgraphDoc {
  const nodes: SubgraphNodeDoc[] = Array.from({ length: nNodes }, (_, i) => ({
    account: `A${String(i).padStart(4, "0")}`,
    is_case_account: i < nCase,
    hop: i < nCase ? 0 : 1 + Math.floor(r() * 4),
  }));
  const edges: SubgraphEdgeDoc[] = Array.from({ length: nEdges }, (_, i) => ({
    tx_id: `T${i}`,
    src: nodes[Math.floor(r() * nNodes)]!.account,
    dst: nodes[Math.floor(r() * nNodes)]!.account,
    ts: new Date(Date.UTC(2024, 5, 1) + Math.floor(r() * 86_400_000)).toISOString(),
    amount: "1.00",
    currency: "EUR",
  }));
  return { nodes, edges, truncated: false, omitted: 0 };
}

describe("motifEdgeIndex", () => {
  it("maps each MOTIF transaction of the Evidence_Chain to its motif type", () => {
    const idx = motifEdgeIndex(bobEvidence.chain);
    expect([...idx.keys()].sort()).toEqual(["T010", "T011", "T012"]);
    expect(idx.get("T011")).toEqual(["STRUCTURING"]);
  });

  it("falls back to the contributor id prefix and the entry's own tx", () => {
    const idx = motifEdgeIndex({
      schema: 1,
      case_id: "X",
      entries: [
        { ts: "2024-01-01T00:00:00+00:00", tx_id: "T9", contributor_id: "FAN_OUT:ab", kind: "MOTIF", ctype: "MOTIF", mass: null, weight: 1, features: {}, detail: {} },
      ],
    });
    expect(idx.get("T9")).toEqual(["FAN_OUT"]);
    expect(motifEdgeIndex(null).size).toBe(0);
  });

  it("parses pipe, comma and list tx id features", () => {
    expect(parseTxIds("T1|T2")).toEqual(["T1", "T2"]);
    expect(parseTxIds("T1, T2")).toEqual(["T1", "T2"]);
    expect(parseTxIds(["T1", 3, ""])).toEqual(["T1"]);
    expect(parseTxIds(undefined)).toEqual([]);
  });
});

describe("toGraph", () => {
  it("marks motif edges, keeps the server truncation flag and the omitted count", () => {
    const g = toGraph(bobSubgraph, motifEdgeIndex(bobEvidence.chain));
    expect(g.nodes.map((n) => n.id)).toEqual(["ACC-BOB", "ACC-SHELL-1", "ACC-SHELL-2", "ACC-SHELL-3", "ACC-MULE-9"]);
    expect(g.nodes.filter((n) => n.isCase)).toHaveLength(4);
    const motif = g.links.filter((l) => l.motifs.length > 0).map((l) => l.id);
    expect(motif).toEqual(["T010", "T011", "T012"]);
    expect(g.links.find((l) => l.id === "T020")?.motifs).toEqual([]);
    expect(g.motifTypes).toEqual(["STRUCTURING"]);
    expect(g.truncated).toBe(true);
    expect(g.omitted).toBe(7);
  });

  it("caps at 500 nodes: case accounts first, then nearest hop, and counts the rest as omitted", () => {
    const sub = randomSubgraph(rng(7), 620, 900, 12);
    const g = toGraph(sub, new Map());
    expect(g.nodes).toHaveLength(MAX_GRAPH_NODES);
    expect(g.truncated).toBe(true);
    expect(g.omitted).toBe(120);
    expect(g.nodes.slice(0, 12).every((n) => n.isCase)).toBe(true);
    const hops = g.nodes.slice(12).map((n) => n.hop);
    expect(hops).toEqual([...hops].sort((a, b) => a - b));
    const maxKept = Math.max(...hops);
    const dropped = sub.nodes.filter((n) => !g.nodes.some((k) => k.id === n.account));
    expect(dropped.every((n) => n.hop >= maxKept)).toBe(true);
  });

  it("is not truncated when the server did not truncate and the cap is not reached", () => {
    const g = toGraph(randomSubgraph(rng(1), 40, 60, 3), new Map());
    expect(g.truncated).toBe(false);
    expect(g.omitted).toBe(0);
  });

  it("holds the cap, case-account and edge-endpoint invariants for random subgraphs", () => {
    const r = rng(42);
    for (let i = 0; i < 60; i++) {
      const nNodes = 1 + Math.floor(r() * 800);
      const nCase = 1 + Math.floor(r() * Math.min(nNodes, 20));
      const sub = randomSubgraph(r, nNodes, Math.floor(r() * 1200), nCase);
      const max = 50 + Math.floor(r() * 451);
      const t = r() < 0.5 ? null : Date.UTC(2024, 5, 1) + Math.floor(r() * 86_400_000);
      const g = toGraph(sub, new Map(), { maxNodes: max, t });
      const ids = new Set(g.nodes.map((n) => n.id));
      expect(g.nodes.length).toBeLessThanOrEqual(Math.max(max, nCase));
      for (const n of sub.nodes) if (n.is_case_account) expect(ids.has(n.account)).toBe(true);
      for (const l of g.links) {
        expect(ids.has(l.source) && ids.has(l.target)).toBe(true);
        if (t !== null) expect(toMs(l.ts)).toBeLessThanOrEqual(t);
      }
      expect(g.truncated).toBe(g.omitted > 0);
    }
  });

  it("drops edges after the replay position and accounts only reachable through them", () => {
    const t = toMs("2024-06-05T15:30:00+00:00");
    const g = toGraph(bobSubgraph, motifEdgeIndex(bobEvidence.chain), { t });
    expect(g.links.map((l) => l.id)).toEqual(["T010", "T020", "T011"]);
    expect(g.hiddenByReplay).toBe(1);
    const early = toGraph(bobSubgraph, new Map(), { t: toMs("2024-06-05T10:00:00+00:00") });
    expect(early.links.map((l) => l.id)).toEqual(["T010"]);
    // ACC-MULE-9 is only connected by T020 (12:00), so it is hidden; case accounts stay.
    expect(early.nodes.map((n) => n.id)).toEqual(["ACC-BOB", "ACC-SHELL-1", "ACC-SHELL-2", "ACC-SHELL-3"]);
  });
});

describe("mergeNeighbourhoods", () => {
  it("adds new accounts and edges once, keeping (ts, tx_id) order and the smaller hop", () => {
    const merged = mergeNeighbourhoods(bobSubgraph, [muleNeighbourhood, muleNeighbourhood]);
    expect(merged.nodes.map((n) => n.account)).toEqual([...bobSubgraph.nodes.map((n) => n.account), "ACC-CASHOUT"]);
    expect(merged.edges.map((e) => e.tx_id)).toEqual(["T010", "T020", "T021", "T011", "T012"]);
    expect(merged.nodes.find((n) => n.account === "ACC-SHELL-1")?.hop).toBe(0);
    expect(mergeNeighbourhoods(bobSubgraph, [])).toBe(bobSubgraph);
  });
});

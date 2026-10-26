// Related_Subgraph → force-graph data (Req 22.4, 17.1-17.3). Pure and React-free so the
// transforms are unit-testable; the component memoises the result per input.

import type { EvidenceChainDoc, NeighbourhoodDoc, SubgraphDoc, SubgraphEdgeDoc, SubgraphNodeDoc } from "../api/types";
import { atOrBefore } from "./time";

/** Most nodes the dashboard draws (Req 22.4); the server caps at replay.max_nodes <= 500. */
export const MAX_GRAPH_NODES = 500;

export interface GraphNode {
  id: string;
  isCase: boolean;
  hop: number;
  /** Node was expanded by the investigator (POST /expand). */
  expanded: boolean;
}

export interface GraphLink {
  id: string; // tx_id
  source: string;
  target: string;
  ts: string;
  amount: string;
  currency: string;
  /** Motif types whose transactions include this edge (empty for ordinary edges). */
  motifs: string[];
}

export interface GraphModel {
  nodes: GraphNode[];
  links: GraphLink[];
  /** Server truncation or the client-side 500-node cap. */
  truncated: boolean;
  /** Accounts left out: the server's omitted count plus any removed by the client cap. */
  omitted: number;
  /** Motif types present on drawn edges, sorted. */
  motifTypes: string[];
  /** Edges hidden because their ts is after the replay position. */
  hiddenByReplay: number;
}

/** Transaction ids of a MOTIF feature: "T1|T2" (chain features are scalars), a list, or
 *  a comma-separated string. */
export function parseTxIds(v: unknown): string[] {
  if (Array.isArray(v)) return v.filter((x): x is string => typeof x === "string" && x.length > 0);
  if (typeof v === "string") return v.split(/[|,]/).map((s) => s.trim()).filter(Boolean);
  return [];
}

/** tx_id → motif types, from the Evidence_Chain MOTIF entries (features.tx_ids and
 *  features.motif_type; the contributor id "TYPE:hash" is the fallback for the type). */
export function motifEdgeIndex(chain: EvidenceChainDoc | null | undefined): Map<string, string[]> {
  const out = new Map<string, Set<string>>();
  for (const e of chain?.entries ?? []) {
    if (e.kind !== "MOTIF" && e.ctype !== "MOTIF") continue;
    const mt = e.features["motif_type"];
    const type = typeof mt === "string" && mt ? mt : e.contributor_id.split(":")[0] ?? e.contributor_id;
    const txs = parseTxIds(e.features["tx_ids"]);
    for (const tx of txs.length ? txs : [e.tx_id]) {
      const s = out.get(tx);
      if (s) s.add(type);
      else out.set(tx, new Set([type]));
    }
  }
  return new Map([...out].map(([tx, s]) => [tx, [...s].sort()]));
}

const byTsTx = (a: SubgraphEdgeDoc, b: SubgraphEdgeDoc) =>
  a.ts < b.ts ? -1 : a.ts > b.ts ? 1 : a.tx_id < b.tx_id ? -1 : a.tx_id > b.tx_id ? 1 : 0;

/** The subgraph plus investigator expansions: new nodes appended (keeping the smaller hop),
 *  edges deduplicated by tx_id and re-sorted by (ts, tx_id). */
export function mergeNeighbourhoods(base: SubgraphDoc, extra: readonly NeighbourhoodDoc[]): SubgraphDoc {
  if (extra.length === 0) return base;
  const nodes = new Map<string, SubgraphNodeDoc>(base.nodes.map((n) => [n.account, n]));
  const edges = new Map<string, SubgraphEdgeDoc>(base.edges.map((e) => [e.tx_id, e]));
  for (const nb of extra) {
    for (const n of nb.nodes) {
      const prev = nodes.get(n.account);
      if (!prev) nodes.set(n.account, n);
      else if (n.hop < prev.hop) nodes.set(n.account, { ...prev, hop: n.hop });
    }
    for (const e of nb.edges) if (!edges.has(e.tx_id)) edges.set(e.tx_id, e);
  }
  return { ...base, nodes: [...nodes.values()], edges: [...edges.values()].sort(byTsTx) };
}

export interface GraphOptions {
  maxNodes?: number;
  /** Replay position (epoch ms); only edges with ts <= t are kept. */
  t?: number | null;
  expanded?: ReadonlySet<string>;
}

/**
 * Graph data for the 3D view and the list fallback.
 * - Replay: edges after ``t`` are dropped, and so are non-case accounts left without any
 *   edge (they were only reachable through later transactions). Case accounts always stay.
 * - Cap: at most ``maxNodes`` accounts, Case accounts first, then by hop distance and
 *   account id (the server's truncation order); only edges between kept accounts remain.
 */
export function toGraph(sub: SubgraphDoc, motifs: ReadonlyMap<string, string[]>, opts: GraphOptions = {}): GraphModel {
  const { maxNodes = MAX_GRAPH_NODES, t = null, expanded } = opts;
  const timed = sub.edges.filter((e) => atOrBefore(e.ts, t));
  const hiddenByReplay = sub.edges.length - timed.length;

  let candidates = sub.nodes;
  if (t !== null) {
    const touched = new Set<string>();
    for (const e of timed) {
      touched.add(e.src);
      touched.add(e.dst);
    }
    candidates = sub.nodes.filter((n) => n.is_case_account || touched.has(n.account));
  }
  const ordered = [...candidates].sort(
    (a, b) =>
      Number(b.is_case_account) - Number(a.is_case_account) ||
      a.hop - b.hop ||
      (a.account < b.account ? -1 : a.account > b.account ? 1 : 0),
  );
  const caseCount = ordered.filter((n) => n.is_case_account).length;
  const kept = ordered.slice(0, Math.max(maxNodes, caseCount));
  const clientOmitted = ordered.length - kept.length;
  const keptIds = new Set(kept.map((n) => n.account));

  const links: GraphLink[] = [];
  const types = new Set<string>();
  for (const e of timed) {
    if (!keptIds.has(e.src) || !keptIds.has(e.dst)) continue;
    const m = motifs.get(e.tx_id) ?? [];
    m.forEach((x) => types.add(x));
    links.push({ id: e.tx_id, source: e.src, target: e.dst, ts: e.ts, amount: e.amount, currency: e.currency, motifs: m });
  }
  return {
    nodes: kept.map((n) => ({ id: n.account, isCase: n.is_case_account, hop: n.hop, expanded: expanded?.has(n.account) ?? false })),
    links,
    truncated: sub.truncated || clientOmitted > 0,
    omitted: (sub.truncated ? sub.omitted : 0) + clientOmitted,
    motifTypes: [...types].sort(),
    hiddenByReplay,
  };
}

// --- visual encoding (shared by the 3D view and its legend) ---------------------------

export const NODE_COLOUR = { case: "#ff5d5d", other: "#6ea8ff", expanded: "#4cd690" } as const;
export const LINK_COLOUR = "#55617d";
const MOTIF_PALETTE = ["#ffd166", "#ff9f43", "#f78fb3", "#c792ea", "#7fdbca"];

export function motifColour(type: string, types: readonly string[]): string {
  const i = Math.max(0, types.indexOf(type));
  return MOTIF_PALETTE[i % MOTIF_PALETTE.length]!;
}

export function nodeLabel(n: GraphNode): string {
  return `${n.id}${n.isCase ? " (case account)" : ""}, hop ${n.hop}${n.expanded ? ", expanded" : ""}`;
}

export function linkLabel(l: GraphLink): string {
  const motif = l.motifs.length ? ` [${l.motifs.join(", ")}]` : "";
  return `${l.id}${motif}: ${l.source} → ${l.target}, ${l.currency} ${l.amount}, ${l.ts}`;
}

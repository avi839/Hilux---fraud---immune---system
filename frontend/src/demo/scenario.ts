// "Load demo scenario": the lines of docs/sample_transactions.jsonl, prepared for submission
// through the real POST /api/v1/events. Nothing here scores or invents anything; the backend
// ingests, scores and opens Cases exactly as for any other client.
//
// Two rewrites make the file usable against a running server, any number of times:
// - Timestamps are shifted by one constant so the newest line is "now". The file is dated
//   2024, and the live stream (generator) runs at the current time, so unshifted events fall
//   outside the Graph_Window and the structuring / mule-chain motifs are never found. The
//   constant shift keeps every gap between events (Alice's 40 minutes Berlin -> Lagos,
//   Bob's three transfers within 24 h, the mule chain's ~25 minute hops) unchanged.
// - tx_id, src and dst get a per-run suffix (e.g. "-D3K9"). Without it a second load is
//   acknowledged as duplicates and nothing new happens, and the accounts would carry the
//   previous run's history.
// Every other field, including the deliberately invalid line (negative amount, currency
// "euro") and the duplicate T007, is sent unchanged, so the server's validation and dedup
// are part of the demo.

import sampleText from "../../../docs/sample_transactions.jsonl?raw";
import type { CaseSummary } from "../api/types";

export const SAMPLE_JSONL: string = sampleText;

export interface PreparedLine {
  /** 1-based line number in the source file. */
  line: number;
  /** tx_id as sent (with the run suffix), or null when the line is not a JSON object. */
  txId: string | null;
  /** JSON text to POST. */
  body: string;
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/** Run suffix from a clock reading: "-D" + 4 base-36 characters. */
export function runTag(nowMs: number): string {
  const n = Math.floor(Math.abs(nowMs) / 1000) % 36 ** 4;
  return `-D${n.toString(36).toUpperCase().padStart(4, "0")}`;
}

/** Parse the JSONL text and apply the time shift and id suffix described above. Blank lines
 *  are skipped; a line that is not a JSON object is sent verbatim (the server rejects it). */
export function prepareScenario(text: string, nowMs: number, tag: string): PreparedLine[] {
  const rows = text
    .split(/\r?\n/)
    .map((raw, i) => ({ line: i + 1, raw: raw.trim() }))
    .filter((r) => r.raw.length > 0)
    .map((r) => {
      let doc: unknown;
      try {
        doc = JSON.parse(r.raw);
      } catch {
        doc = null;
      }
      return { ...r, doc: isRecord(doc) ? doc : null };
    });
  const times = rows
    .map((r) => (r.doc && typeof r.doc.ts === "string" ? Date.parse(r.doc.ts) : NaN))
    .filter((t) => Number.isFinite(t));
  const shift = times.length ? nowMs - Math.max(...times) : 0;
  return rows.map(({ line, raw, doc }) => {
    if (!doc) return { line, txId: null, body: raw };
    const out: Record<string, unknown> = { ...doc };
    for (const k of ["tx_id", "src", "dst"] as const) if (typeof out[k] === "string") out[k] = `${out[k] as string}${tag}`;
    if (typeof out.ts === "string") {
      const t = Date.parse(out.ts);
      if (Number.isFinite(t)) out.ts = new Date(t + shift).toISOString();
    }
    return { line, txId: typeof out.tx_id === "string" ? out.tx_id : null, body: JSON.stringify(out) };
  });
}

/**
 * Split prepared lines into groups that share no account (connected components of the
 * src–dst graph), each in file order. Lines of one group must be sent one after another so
 * every account's events arrive in timestamp order; separate groups can be sent in parallel.
 * A line that is not a JSON object (or has no accounts) forms its own group.
 */
export function independentGroups(lines: readonly PreparedLine[]): PreparedLine[][] {
  const parent = new Map<string, string>();
  const find = (a: string): string => {
    let r = a;
    while (parent.get(r) !== r) r = parent.get(r)!;
    parent.set(a, r);
    return r;
  };
  const accountsOf = (l: PreparedLine): string[] => {
    try {
      const d = JSON.parse(l.body) as unknown;
      if (!isRecord(d)) return [];
      return [d.src, d.dst].filter((v): v is string => typeof v === "string");
    } catch {
      return [];
    }
  };
  const keys = lines.map((l, i) => {
    const accts = accountsOf(l);
    if (accts.length === 0) return `#line${i}`;
    for (const a of accts) if (!parent.has(a)) parent.set(a, a);
    const [first, ...rest] = accts;
    for (const a of rest) parent.set(find(a), find(first!));
    return first!;
  });
  const groups = new Map<string, PreparedLine[]>();
  lines.forEach((l, i) => {
    const k = keys[i]!.startsWith("#line") ? keys[i]! : find(keys[i]!);
    const g = groups.get(k);
    if (g) g.push(l);
    else groups.set(k, [l]);
  });
  return [...groups.values()];
}

/** The three stories in the sample file, keyed by an account that only that story uses. */
export const STORIES: ReadonlyArray<{ account: string; title: string }> = [
  { account: "ACC-ALICE", title: "Alice: Berlin card user, then 4,800 EUR from Lagos on a new device 40 min later" },
  { account: "ACC-BOB", title: "Bob: three transfers just under 10,000 EUR within 24 h (structuring)" },
  { account: "ACC-CARL", title: "Mule chain: Carl → Dave → Erin → Finn, each hop slightly smaller, ~25 min apart" },
  { account: "ACC-DAVE", title: "Mule chain: Carl → Dave → Erin → Finn, each hop slightly smaller, ~25 min apart" },
];

export interface ScenarioCase {
  caseId: string;
  title: string;
  summary: CaseSummary;
}

/** Cases (Active_Queue or backlog) that involve this run's accounts, one per story, in
 *  story order. */
export function scenarioCases(cases: Readonly<Record<string, CaseSummary>>, tag: string): ScenarioCase[] {
  const out: ScenarioCase[] = [];
  const seen = new Set<string>();
  const titles = new Set<string>();
  for (const s of STORIES) {
    if (titles.has(s.title)) continue;
    const account = `${s.account}${tag}`;
    const hits = Object.values(cases)
      .filter((c) => c.accounts.includes(account) && !c.merged_into)
      .sort((a, b) => b.risk - a.risk || (a.case_id < b.case_id ? -1 : 1));
    const c = hits.find((h) => !seen.has(h.case_id));
    if (!c) continue;
    seen.add(c.case_id);
    titles.add(s.title);
    out.push({ caseId: c.case_id, title: s.title, summary: c });
  }
  return out;
}

// Subgraph and Replay_Timeline documents for the sample run (docs/sample_transactions.jsonl),
// shaped like the API_Server's jsonable() output of helix/replay. Hand-built from the
// sample events: Alice T004-T007 (T006 has no location, T007 is Berlin → Lagos) and Bob's
// STRUCTURING transactions T010-T012 to three shell accounts.

import { vi } from "vitest";
import type { NeighbourhoodDoc, SubgraphDoc, TimelineDoc, TimelineEntryDoc, TxEvent } from "../api/types";

function entry(seq_no: number, event: TxEvent): TimelineEntryDoc {
  return { seq_no, event, late: false, assessment: null, stage2: null, risk: null };
}

/** GET /cases/C-1/timeline: [T007 - 24 h lead, case updated_at]. */
export const aliceTimeline: TimelineDoc = {
  case_id: "C-1",
  start: "2024-06-04T11:20:00+00:00",
  end: "2024-06-05T11:20:00+00:00",
  entries: [
    entry(4, { tx_id: "T004", ts: "2024-06-04T18:30:00+00:00", src: "ACC-ALICE", dst: "MER-GYM", amount: "35.00", currency: "EUR", lat: 52.53, lon: 13.41 }),
    entry(5, { tx_id: "T005", ts: "2024-06-05T10:10:00+00:00", src: "ACC-ALICE", dst: "MER-GROCER", amount: "47.80", currency: "EUR", lat: 52.52, lon: 13.4 }),
    entry(6, { tx_id: "T006", ts: "2024-06-05T10:40:00+00:00", src: "ACC-ALICE", dst: "MER-GROCER", amount: "39.99", currency: "EUR" }),
    entry(7, { tx_id: "T007", ts: "2024-06-05T11:20:00+00:00", src: "ACC-ALICE", dst: "ACC-UNKNOWN-77", amount: "4800.00", currency: "EUR", lat: 6.52, lon: 3.38 }),
  ],
};

/** GET /cases/C-1/subgraph */
export const aliceSubgraph: SubgraphDoc = {
  case_id: "C-1",
  nodes: [
    { account: "ACC-ALICE", is_case_account: true, hop: 0 },
    { account: "ACC-UNKNOWN-77", is_case_account: true, hop: 0 },
    { account: "MER-GROCER", is_case_account: false, hop: 1 },
    { account: "MER-GYM", is_case_account: false, hop: 1 },
  ],
  edges: [
    { tx_id: "T004", src: "ACC-ALICE", dst: "MER-GYM", ts: "2024-06-04T18:30:00+00:00", amount: "35.00", currency: "EUR" },
    { tx_id: "T005", src: "ACC-ALICE", dst: "MER-GROCER", ts: "2024-06-05T10:10:00+00:00", amount: "47.80", currency: "EUR" },
    { tx_id: "T006", src: "ACC-ALICE", dst: "MER-GROCER", ts: "2024-06-05T10:40:00+00:00", amount: "39.99", currency: "EUR" },
    { tx_id: "T007", src: "ACC-ALICE", dst: "ACC-UNKNOWN-77", ts: "2024-06-05T11:20:00+00:00", amount: "4800.00", currency: "EUR" },
  ],
  case_accounts: ["ACC-ALICE", "ACC-UNKNOWN-77"],
  start: "2024-06-04T11:20:00+00:00",
  end: "2024-06-05T12:00:00+00:00",
  hops: 2,
  max_nodes: 500,
  truncated: false,
  omitted: 0,
};

/** GET /cases/C-2/subgraph, flagged as truncated with 7 omitted accounts. */
export const bobSubgraph: SubgraphDoc = {
  case_id: "C-2",
  nodes: [
    { account: "ACC-BOB", is_case_account: true, hop: 0 },
    { account: "ACC-SHELL-1", is_case_account: true, hop: 0 },
    { account: "ACC-SHELL-2", is_case_account: true, hop: 0 },
    { account: "ACC-SHELL-3", is_case_account: true, hop: 0 },
    { account: "ACC-MULE-9", is_case_account: false, hop: 1 },
  ],
  edges: [
    { tx_id: "T010", src: "ACC-BOB", dst: "ACC-SHELL-1", ts: "2024-06-05T09:00:00+00:00", amount: "9800.00", currency: "EUR" },
    { tx_id: "T020", src: "ACC-SHELL-1", dst: "ACC-MULE-9", ts: "2024-06-05T12:00:00+00:00", amount: "9700.00", currency: "EUR" },
    { tx_id: "T011", src: "ACC-BOB", dst: "ACC-SHELL-2", ts: "2024-06-05T15:30:00+00:00", amount: "9650.00", currency: "EUR" },
    { tx_id: "T012", src: "ACC-BOB", dst: "ACC-SHELL-3", ts: "2024-06-06T08:45:00+00:00", amount: "9900.00", currency: "EUR" },
  ],
  case_accounts: ["ACC-BOB", "ACC-SHELL-1", "ACC-SHELL-2", "ACC-SHELL-3"],
  hops: 2,
  max_nodes: 500,
  truncated: true,
  omitted: 7,
};

/** POST /cases/C-2/expand {account: "ACC-MULE-9"} */
export const muleNeighbourhood: NeighbourhoodDoc = {
  case_id: "C-2",
  account: "ACC-MULE-9",
  nodes: [
    { account: "ACC-MULE-9", is_case_account: false, hop: 1 },
    { account: "ACC-CASHOUT", is_case_account: false, hop: 2 },
    { account: "ACC-SHELL-1", is_case_account: true, hop: 0 },
  ],
  edges: [
    { tx_id: "T020", src: "ACC-SHELL-1", dst: "ACC-MULE-9", ts: "2024-06-05T12:00:00+00:00", amount: "9700.00", currency: "EUR" },
    { tx_id: "T021", src: "ACC-MULE-9", dst: "ACC-CASHOUT", ts: "2024-06-05T13:00:00+00:00", amount: "9600.00", currency: "EUR" },
  ],
};

/** GET /cases/C-2/timeline: no event of Bob's case has a location. */
export const bobTimeline: TimelineDoc = {
  case_id: "C-2",
  start: "2024-06-04T09:00:00+00:00",
  end: "2024-06-06T08:45:00+00:00",
  entries: [
    entry(10, { tx_id: "T010", ts: "2024-06-05T09:00:00+00:00", src: "ACC-BOB", dst: "ACC-SHELL-1", amount: "9800.00", currency: "EUR" }),
    entry(11, { tx_id: "T011", ts: "2024-06-05T15:30:00+00:00", src: "ACC-BOB", dst: "ACC-SHELL-2", amount: "9650.00", currency: "EUR" }),
    entry(12, { tx_id: "T012", ts: "2024-06-06T08:45:00+00:00", src: "ACC-BOB", dst: "ACC-SHELL-3", amount: "9900.00", currency: "EUR" }),
  ],
};

export const NO_STORAGE = { detail: "replay timeline requires Storage, which is not configured" };

/** Pretend the browser has WebGL (jsdom has none), so the panels pick their 3D/map views;
 *  the libraries themselves are replaced by the recording mocks of setup.ts. */
export function enableWebGL() {
  vi.stubGlobal("WebGLRenderingContext", function WebGLRenderingContext() {});
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({} as never);
}

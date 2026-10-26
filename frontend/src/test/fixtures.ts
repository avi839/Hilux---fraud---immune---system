import { vi } from "vitest";
import type { CaseSummary, CaseUpdatedMessage, ScoredEventMessage, Tier } from "../api/types";
import { toFeedItem, type FeedItem } from "../store/reducers";

export function scored(seq: number, overrides: Partial<ScoredEventMessage["event"]> = {}, risk = 0.5): ScoredEventMessage {
  return {
    channel: "events",
    type: "event.scored",
    seq,
    event: {
      tx_id: `T-${seq}`,
      ts: "2025-03-14T10:22:05.120+00:00",
      src: "A-1",
      dst: "A-2",
      amount: "949.00",
      currency: "GBP",
      ...overrides,
    },
    assessment: { stage: 1, belief: 0.4, plausibility: 0.87, uncertainty: 0.46, conflict: 0.08, high_conflict: false },
    risk,
    late: false,
    case_id: null,
  };
}

export function feedItem(seq: number): FeedItem {
  return toFeedItem(scored(seq));
}

export function caseSummary(id: string, overrides: Partial<CaseSummary> = {}): CaseSummary {
  return {
    case_id: id,
    status: "OPEN",
    tier: "HIGH" as Tier,
    risk: 0.81,
    uncertainty: 0.21,
    priority: "100.00",
    expected_loss: "120.00",
    placement: "ACTIVE",
    amount_at_risk: [{ currency: "GBP", amount: "2696.80" }],
    accounts: ["A-1042", "A-7781", "A-3310"],
    tx_count: 4,
    motifs: ["STRUCTURING"],
    assignee: null,
    merged_into: null,
    label: null,
    updated_at: "2025-03-14T10:22:05.180+00:00",
    ...overrides,
  };
}

export function caseMsg(c: CaseSummary, seq = 1, backlog = 0): CaseUpdatedMessage {
  return { channel: "cases", type: "case.updated", seq, case: c, backlog_count: backlog };
}

type Route = (init: RequestInit | undefined, url: string) => Response | Promise<Response>;

/**
 * Stub global fetch with a router keyed by API path (without /api/v1 and query string).
 * Unknown paths answer 404 like the API_Server. Returns the vi.fn for call assertions.
 */
export function routeFetch(routes: Record<string, Route | unknown>) {
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const path = url.replace(/^.*\/api\/v1/, "").split("?")[0] ?? "";
    if (!(path in routes)) return jsonResponse({ detail: "Not Found" }, 404);
    const r = routes[path];
    return typeof r === "function" ? (r as Route)(init, url) : jsonResponse(r);
  });
  vi.stubGlobal("fetch", fn);
  return fn;
}

/** A response that resolves when ``release`` is called and rejects with an AbortError when
 *  the request's signal aborts, like the browser's fetch. */
export function deferredResponse(body: unknown, status = 200) {
  let release!: () => void;
  let signal: AbortSignal | undefined;
  const route: Route = (init) =>
    new Promise<Response>((resolve, reject) => {
      signal = init?.signal ?? undefined;
      release = () => resolve(jsonResponse(body, status));
      signal?.addEventListener("abort", () => reject(new DOMException("The operation was aborted.", "AbortError")));
    });
  return { route, release: () => release(), aborted: () => signal?.aborted === true };
}

/** Minimal fetch Response stand-in carrying a JSON body. */
export function jsonResponse(body: unknown, status = 200): Response {
  const text = JSON.stringify(body);
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? "OK" : "Error",
    text: async () => text,
    json: async () => JSON.parse(text) as unknown,
  } as unknown as Response;
}

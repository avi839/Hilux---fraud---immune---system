// Typed fetch wrapper for the HELIX REST API (/api/v1). Every request has a deadline so a
// control action that receives no confirmation surfaces as an error (Req 22.15).

import {
  ALERT_BUDGET,
  INVESTIGATION_COST,
  type BulkFailure,
  type BulkFeedbackResult,
  type CaseDetail,
  type DatasetDoc,
  type CasePage,
  type CaseSummary,
  type ConfigDoc,
  type ConfigPatchResult,
  type EvidenceDoc,
  type FeedbackLabel,
  type FieldProblem,
  type FrameDoc,
  type GeneratorState,
  type IngestAccepted,
  type NeighbourhoodDoc,
  type StatusDoc,
  type SubgraphDoc,
  type TimelineDoc,
} from "./types";

export const API_BASE = import.meta.env.VITE_API_URL ? `${import.meta.env.VITE_API_URL}/api/v1` : "/api/v1";
/** Deadline of control actions: no confirmation within it is reported as an error (Req 22.15). */
export const CONFIRM_TIMEOUT_MS = 5_000;
/** Deadline of read-only Case documents. The evidence, timeline and subgraph of a large Case
 *  are megabytes and can take several seconds to build, so 5 s would fail them spuriously. */
export const READ_TIMEOUT_MS = 30_000;

/** "aborted": the caller cancelled the request through its AbortSignal (e.g. a stale
 *  selection); callers normally ignore it. */
export type ApiErrorKind = "http" | "timeout" | "network" | "parse" | "aborted";

export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  readonly status: number | null;
  readonly detail: string;
  readonly errors: FieldProblem[];
  /** Per-Case failures of a rejected bulk request (POST /cases/bulk-feedback). */
  readonly failures: BulkFailure[];

  constructor(
    kind: ApiErrorKind,
    detail: string,
    status: number | null = null,
    errors: FieldProblem[] = [],
    failures: BulkFailure[] = [],
  ) {
    super(detail);
    this.name = "ApiError";
    this.kind = kind;
    this.status = status;
    this.detail = detail;
    this.errors = errors;
    this.failures = failures;
  }

  /** One human-readable line, including permitted ranges reported by the server. */
  describe(): string {
    if (this.kind === "timeout") return "No confirmation from the server within 5 s; nothing was changed here.";
    if (this.kind === "aborted") return "Request cancelled.";
    if (this.kind === "network") return `Cannot reach the server: ${this.detail}`;
    const parts = this.errors.map((e) => {
      const range = e.permitted ? ` (permitted: ${e.permitted.rule})` : "";
      return `${e.field}: ${e.reason}${range}`;
    });
    const head = this.status !== null ? `Server rejected the request (${this.status}): ${this.detail}` : this.detail;
    return parts.length ? `${head} - ${parts.join("; ")}` : head;
  }
}

export interface RequestOptions {
  method?: "GET" | "POST" | "PATCH";
  body?: unknown;
  /** A body sent verbatim (already JSON text), instead of JSON.stringify(body). */
  rawBody?: string;
  timeoutMs?: number;
  /** Caller cancellation; an aborted request rejects with ApiError kind "aborted". */
  signal?: AbortSignal;
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

export async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const { method = "GET", rawBody, timeoutMs = CONFIRM_TIMEOUT_MS, signal } = opts;
  const body = rawBody ?? (opts.body === undefined ? undefined : JSON.stringify(opts.body));
  if (signal?.aborted) throw new ApiError("aborted", "request cancelled");
  const ctrl = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    ctrl.abort();
  }, timeoutMs);
  const onAbort = () => ctrl.abort();
  signal?.addEventListener("abort", onAbort, { once: true });
  const done = () => {
    clearTimeout(timer);
    signal?.removeEventListener("abort", onAbort);
  };
  const cancelled = () => (signal?.aborted ? new ApiError("aborted", "request cancelled") : null);
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      method,
      headers: body === undefined ? { Accept: "application/json" } : { Accept: "application/json", "Content-Type": "application/json" },
      body,
      signal: ctrl.signal,
    });
  } catch (err) {
    done();
    if (timedOut) throw new ApiError("timeout", "request timed out");
    throw cancelled() ?? new ApiError("network", err instanceof Error ? err.message : String(err));
  }
  let data: unknown = null;
  try {
    const text = await res.text();
    data = text ? (JSON.parse(text) as unknown) : null;
  } catch (err) {
    done();
    if (timedOut) throw new ApiError("timeout", "request timed out");
    const c = cancelled();
    if (c) throw c;
    if (!res.ok) throw new ApiError("http", res.statusText || "request failed", res.status);
    throw new ApiError("parse", "invalid JSON in response", res.status);
  }
  done();
  const c = cancelled();
  if (c) throw c;
  if (!res.ok) {
    const detail = isRecord(data) && typeof data.detail === "string" ? data.detail : res.statusText || "request failed";
    const errors = isRecord(data) && Array.isArray(data.errors) ? (data.errors as FieldProblem[]) : [];
    const failures = isRecord(data) && Array.isArray(data.failures) ? (data.failures as BulkFailure[]) : [];
    throw new ApiError("http", detail, res.status, errors, failures);
  }
  return data as T;
}

const enc = encodeURIComponent;

export const api = {
  listCases: (pageSize = 100, cursor?: string | null) =>
    request<CasePage>(`/cases?page_size=${pageSize}${cursor ? `&cursor=${enc(cursor)}` : ""}`, { timeoutMs: READ_TIMEOUT_MS }),

  /** Submit one Transaction_Event document (JSON text, sent as is) to the Stream_Ingestor.
   *  202 = accepted or duplicate; an invalid event rejects with ApiError 422 and field errors. */
  ingest: (json: string) => request<IngestAccepted>("/events", { method: "POST", rawBody: json }),

  /** Every Case, following next_cursor (bounded so a misbehaving server cannot loop us). */
  async allCases(maxPages = 100): Promise<{ items: CaseSummary[]; backlog_count: number }> {
    const items: CaseSummary[] = [];
    let cursor: string | null = null;
    let backlog = 0;
    for (let i = 0; i < maxPages; i++) {
      const page: CasePage = await api.listCases(100, cursor);
      items.push(...page.items);
      backlog = page.backlog_count;
      cursor = page.next_cursor;
      if (!cursor) break;
    }
    return { items, backlog_count: backlog };
  },

  getCase: (id: string, signal?: AbortSignal) => request<CaseDetail>(`/cases/${enc(id)}`, { signal, timeoutMs: READ_TIMEOUT_MS }),
  getEvidence: (id: string, signal?: AbortSignal) =>
    request<EvidenceDoc>(`/cases/${enc(id)}/evidence`, { signal, timeoutMs: READ_TIMEOUT_MS }),
  openCase: (id: string, investigator: string) =>
    request<CaseDetail>(`/cases/${enc(id)}/open`, { method: "POST", body: { investigator } }),
  feedback: (id: string, by: string, label: FeedbackLabel, note = "") =>
    request<CaseDetail>(`/cases/${enc(id)}/feedback`, { method: "POST", body: { by, label, note } }),
  escalate: (id: string, by: string) => request<CaseDetail>(`/cases/${enc(id)}/escalate`, { method: "POST", body: { by } }),
  /** One Feedback_Label on 1-50 distinct Cases, all or nothing. A rejection (404/409/422)
   *  carries the per-Case failures in ApiError.failures. */
  bulkFeedback: (by: string, label: FeedbackLabel, caseIds: string[], note = "") =>
    request<BulkFeedbackResult>("/cases/bulk-feedback", { method: "POST", body: { by, label, note, case_ids: caseIds } }),
  /** Dataset_Feeder status; DISABLED without --dataset, 404 on servers without the route. */
  getDataset: (signal?: AbortSignal) => request<DatasetDoc>("/dataset", { signal }),

  /** Related_Subgraph of the Case accounts (hops 1-4, server default when omitted). */
  getSubgraph: (id: string, hops?: number, signal?: AbortSignal) =>
    request<SubgraphDoc>(`/cases/${enc(id)}/subgraph${hops !== undefined ? `?hops=${hops}` : ""}`, { signal, timeoutMs: READ_TIMEOUT_MS }),
  /** Direct neighbours of one subgraph account, live or in the replay frame at ``t``. */
  expand: (id: string, account: string, t?: string | null, signal?: AbortSignal) =>
    request<NeighbourhoodDoc>(`/cases/${enc(id)}/expand`, {
      method: "POST",
      body: t ? { account, t } : { account },
      signal,
      timeoutMs: READ_TIMEOUT_MS,
    }),
  /** Replay_Timeline; 503 when the server runs without Storage. */
  getTimeline: (id: string, signal?: AbortSignal) =>
    request<TimelineDoc>(`/cases/${enc(id)}/timeline`, { signal, timeoutMs: READ_TIMEOUT_MS }),
  /** Point-in-time frame; 422 when ``t`` is outside the timeline, 503 without Storage. */
  getFrame: (id: string, t: string, signal?: AbortSignal) =>
    request<FrameDoc>(`/cases/${enc(id)}/timeline/at?t=${enc(t)}`, { signal, timeoutMs: READ_TIMEOUT_MS }),

  getConfig: () => request<ConfigDoc>("/config"),
  setAlertBudget: (n: number) => request<ConfigPatchResult>("/config", { method: "PATCH", body: { [ALERT_BUDGET]: n } }),
  setInvestigationCost: (n: number) =>
    request<ConfigPatchResult>("/config", { method: "PATCH", body: { [INVESTIGATION_COST]: n } }),

  getStatus: () => request<StatusDoc>("/status"),
  getMetrics: (signal?: AbortSignal) => request<Record<string, unknown>>("/metrics", { signal }),
  /** Immune_System state; the endpoint may not exist on older servers (404). */
  getImmune: (signal?: AbortSignal) => request<Record<string, unknown>>("/immune", { signal }),
  /** Attacker_Agent state; the endpoint may not exist on older servers (404). */
  getAttacker: (signal?: AbortSignal) => request<Record<string, unknown>>("/attacker", { signal }),
  startGenerator: () => request<GeneratorState>("/sim/generator/start", { method: "POST" }),
  stopGenerator: () => request<GeneratorState>("/sim/generator/stop", { method: "POST" }),
};

export type Api = typeof api;

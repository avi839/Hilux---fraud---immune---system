// Wire types of the HELIX API_Server (backend/helix/api/schemas.py, design "WebSocket messages").
// Amounts and priorities are decimal strings; timestamps are ISO 8601 with offset.

export type Tier = "LOW" | "MEDIUM" | "HIGH" | "CRITICAL";
export type CaseStatus = "OPEN" | "IN_REVIEW" | "CLOSED";
export type Placement = "ACTIVE" | "BACKLOG" | "NONE";
export type FeedbackLabel = "CONFIRMED_FRAUD" | "FALSE_POSITIVE";
export type Channel = "events" | "cases" | "metrics";

export interface TxEvent {
  tx_id: string;
  ts: string;
  src: string;
  dst: string;
  amount: string;
  currency: string;
  channel?: string;
  mcc?: string;
  lat?: number;
  lon?: number;
  device_id?: string;
  session_id?: string;
}

export interface Assessment {
  stage?: number;
  belief: number;
  plausibility: number;
  uncertainty: number;
  conflict: number;
  high_conflict: boolean;
}

export interface ScoredEventMessage {
  channel: "events";
  type: "event.scored";
  seq: number;
  event: TxEvent;
  assessment: Assessment;
  stage2?: { belief: number; plausibility: number; uncertainty: number; risk: number };
  risk: number;
  late: boolean;
  case_id: string | null;
}

export interface AmountAtRisk {
  currency: string;
  amount: string;
}

/** Why a CLOSED Case was closed (Req 41.1). AUTO_CLEARED is not a Feedback_Label. */
export type ClosureReason = "LABELLED" | "MERGED" | "AUTO_CLEARED";
export type SuggestionLabel = FeedbackLabel | "NONE";

/** Label_Suggester advice (Req 41.4): computed at serialisation, never stored or applied. */
export interface SuggestedLabel {
  label: SuggestionLabel;
  /** 0-1. */
  confidence: number;
  reason: string;
}

export interface CaseSummary {
  case_id: string;
  status: CaseStatus;
  tier: Tier;
  risk: number;
  uncertainty: number | null;
  priority: string;
  expected_loss: string;
  placement: Placement;
  amount_at_risk: AmountAtRisk[];
  accounts: string[];
  tx_count: number;
  motifs: string[];
  assignee: string | null;
  merged_into?: string | null;
  label?: FeedbackLabel | null;
  updated_at: string;
  closure_reason?: ClosureReason | null;
  suggested_label?: SuggestedLabel | null;
}

export interface CaseHistoryEntry {
  ts: string;
  action: string;
  prev: string | null;
  new: string | null;
  actor: string | null;
}

export interface CaseDetail extends CaseSummary {
  seq_no: number;
  tx_ids: string[];
  created_at: string;
  escalated?: boolean;
  risk_at_creation?: number;
  evidence_available?: boolean;
  closed_at?: string | null;
  history: CaseHistoryEntry[];
  [key: string]: unknown;
}

// --- Evidence_Chain and explanation (GET /cases/{id}/evidence) -------------------------

/** Mass_Function as stored in the Evidence_Chain: raw (undiscounted) masses on {FRAUD},
 *  {LEGIT} and {FRAUD, LEGIT}. The discounted masses are f' = w·f, l' = w·l, Θ' = 1 − f' − l'. */
export interface MassDoc {
  f: number;
  l: number;
  t: number;
}

export type EntryKind =
  | "CONTRIBUTOR"
  | "FUSED"
  | "MOTIF"
  | "CHANGE_POINT"
  | "MISSING_FIELD"
  | "SENSOR_FAILURE"
  | "LATE"
  | "TRUNCATION";

export interface ChainEntry {
  ts: string;
  tx_id: string;
  contributor_id: string;
  kind: EntryKind | (string & {});
  ctype: string | null;
  mass: MassDoc | null;
  weight: number | null;
  features: Record<string, unknown>;
  detail: Record<string, unknown>;
}

export interface EvidenceChainDoc {
  schema: number;
  case_id: string;
  /** Canonical (ts, tx_id, contributor_id) order. */
  entries: ChainEntry[];
}

export interface ExplanationItemDoc {
  name: string;
  label: string;
  ctype: string;
  weight: number;
  tx_ids: string[];
  /** F' = max discounted mass on {FRAUD} over the contributor's entries. */
  f: number;
  /** L' = max discounted mass on {LEGIT} over the contributor's entries. */
  l: number;
  comparison: string;
  generic: boolean;
  text: string;
}

export interface ExplanationDoc {
  case_id: string;
  summary: string;
  reasons: ExplanationItemDoc[];
  mitigating: ExplanationItemDoc[];
  insufficient: ExplanationItemDoc[];
  notes: string[];
  message: string | null;
  text?: string;
}

export interface EvidenceDoc {
  case_id: string;
  chain: EvidenceChainDoc | null;
  explanation: ExplanationDoc;
}

export interface CaseUpdatedMessage {
  channel: "cases";
  type: "case.updated";
  seq: number;
  case: CaseSummary;
  backlog_count: number;
}

/** Per-stage rolling-window stats (AppState.stage_means). ``mean_ms`` is the string
 *  "unavailable" when the window has no samples; ``total`` counts since start. */
export interface StageStat {
  count: number;
  mean_ms: number | string | null;
  total?: number;
}

/** /ws/metrics frame (pipeline metrics + stages/ingest/latency_breach) or GET /metrics
 *  (loop/stages/ingest/latency_breach/evaluation/...). Read through LoopPanel's normaliser. */
export interface MetricsMessage {
  channel?: "metrics";
  type?: string;
  seq?: number;
  stages?: Record<string, StageStat>;
  [key: string]: unknown;
}

export interface CasePage {
  items: CaseSummary[];
  page_size: number;
  total: number;
  next_cursor: string | null;
  backlog_count: number;
}

export interface PermittedRange {
  lo: number;
  hi: number;
  lo_open: boolean;
  rule: string;
}

export interface FieldProblem {
  field: string;
  reason: string;
  rule?: string | null;
  permitted?: PermittedRange | null;
}

export interface ConfigDoc {
  effective: { "prioritiser.alert_budget": number; "prioritiser.investigation_cost": number };
  /** Changes waiting for the next budget period, keyed by short name, e.g. "alert_budget". */
  pending: Record<string, number>;
  current_period: number;
  runtime?: Record<string, PermittedRange>;
}

export interface ConfigPatchResult {
  accepted: Record<string, number>;
  applies_from_period: number;
  pending: Record<string, number>;
}

/** 202 body of POST /events (schemas.IngestAccepted). */
export interface IngestAccepted {
  tx_id: string;
  status: "accepted" | "duplicate" | (string & {});
  duplicate: boolean;
  backpressure: boolean;
}

export interface GeneratorState {
  running: boolean;
  changed: boolean;
}

export interface StatusDoc {
  status: string;
  generator?: { configured: boolean; running: boolean };
  [key: string]: unknown;
}

// --- Related_Subgraph and Replay_Timeline (backend/helix/replay, serialised by jsonable) ----

export interface SubgraphNodeDoc {
  account: string;
  is_case_account: boolean;
  /** Hop distance from the nearest Case account (0 for Case accounts). */
  hop: number;
}

export interface SubgraphEdgeDoc {
  tx_id: string;
  src: string;
  dst: string;
  ts: string;
  amount: string;
  currency: string;
}

/** GET /cases/{id}/subgraph (and ReplayFrame.subgraph). Nodes by (hop, account), edges by (ts, tx_id). */
export interface SubgraphDoc {
  case_id?: string;
  nodes: SubgraphNodeDoc[];
  edges: SubgraphEdgeDoc[];
  case_accounts?: string[];
  start?: string | null;
  end?: string | null;
  hops?: number;
  max_nodes?: number;
  truncated: boolean;
  /** Nodes left out by truncation. */
  omitted: number;
}

/** POST /cases/{id}/expand: the account first, then its direct neighbours. */
export interface NeighbourhoodDoc {
  case_id?: string;
  account: string;
  nodes: SubgraphNodeDoc[];
  edges: SubgraphEdgeDoc[];
}

/** FusedAssessment as serialised by jsonable (mass {f,l,t}, contributions). */
export interface FusedAssessmentDoc {
  tx_id: string;
  stage: 1 | 2;
  mass: MassDoc;
  belief: number;
  plausibility: number;
  uncertainty: number;
  conflict: number;
  high_conflict: boolean;
  contributions?: Array<{ contributor_id: string; kind: string; mass: MassDoc; weight: number }>;
}

export interface TimelineEntryDoc {
  seq_no: number;
  event: TxEvent;
  late: boolean;
  assessment: FusedAssessmentDoc | null;
  stage2: FusedAssessmentDoc | null;
  risk: number | null;
}

/** GET /cases/{id}/timeline: entries ordered by (ts, tx_id). 503 without Storage. */
export interface TimelineDoc {
  case_id: string;
  start: string;
  end: string;
  entries: TimelineEntryDoc[];
}

/** GET /cases/{id}/timeline/at?t: subgraph over [t - Graph_Window, t] and entries with ts <= t. */
export interface FrameDoc {
  case_id: string;
  t: string;
  window_start: string;
  subgraph: SubgraphDoc;
  entries: TimelineEntryDoc[];
}

// --- Bulk feedback (POST /cases/bulk-feedback, Req 41.6, 41.7) ------------------------

export const MAX_BULK_CASES = 50;

export interface BulkFeedbackResult {
  by: string;
  label: FeedbackLabel;
  count: number;
  case_ids: string[];
  cases: CaseSummary[];
}

/** One failing Case of a rejected (all-or-nothing) bulk request. */
export interface BulkFailure {
  case_id: string;
  reason: string;
  status: number;
}

// --- Dataset streaming (GET /dataset, Req 39.5, 39.6, 40.8) --------------------------

export type DatasetStatus = "DISABLED" | "LOADING" | "WARMING_UP" | "STREAMING" | "DONE" | "FAILED";

export interface DatasetPatternTypeDoc {
  pattern_type: string;
  mapped: boolean;
  motif: string | null;
  patterns: number;
  detected_patterns: number;
  detection_rate: number | null;
  recall: number | null;
  precision: number | null;
  /** Why precision is null: "unmapped" (no Motif type) or "no_cases" (no Case holds the Motif yet). */
  precision_reason: "unmapped" | "no_cases" | null;
}

export interface DatasetEvaluationDoc {
  precision: number | null;
  recall: number | null;
  captured_fraud_fraction: number | null;
  pattern_types: DatasetPatternTypeDoc[];
}

export interface DatasetWarmupDoc {
  until: string | null;
  done: boolean;
  events: number | null;
  duration_s: number | null;
  events_per_s: number | null;
  accounts: number | null;
  accounts_ready: number | null;
  min_history: number | null;
}

/** With status DISABLED every other field is null. */
export interface DatasetDoc {
  status: DatasetStatus;
  kind: string | null;
  files: { trans: string | null; patterns: string | null; mapping: string | null } | null;
  rows_read: number | null;
  skipped: number | null;
  skip_reasons: Record<string, number> | null;
  in_scope: number | null;
  submitted: number | null;
  /** submitted / in_scope, 0-1; null before the in-scope count is known. */
  progress: number | null;
  speed: number | string | null;
  max_rows: number | null;
  warmup: DatasetWarmupDoc | null;
  error: string | null;
  evaluation: DatasetEvaluationDoc | null;
}

export const ALERT_BUDGET = "prioritiser.alert_budget";
export const INVESTIGATION_COST = "prioritiser.investigation_cost";

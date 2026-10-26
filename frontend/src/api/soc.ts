// Typed calls of the SOC / cyber-fusion routes of the HELIX API_Server (/api/v1, Req 29-37),
// through the shared request() wrapper (deadlines, ApiError). Shapes mirror
// backend/helix/api/soc_routes.py, soc_response_routes.py, soc_intel_routes.py and
// soc_ops_routes.py. Fields the backend may add later are tolerated (index signatures).

import { ApiError, READ_TIMEOUT_MS, request } from "./client";

const enc = encodeURIComponent;

// --- Kill chain and techniques (Req 29, 30) ------------------------------------------------

/** Kill_Chain_Stages in stage order (Req 29.1). */
export const KILL_CHAIN_STAGES = [
  "RECONNAISSANCE",
  "CREDENTIAL_ACCESS",
  "ACCOUNT_TAKEOVER",
  "PAYEE_MANIPULATION",
  "CASH_OUT",
  "LAYERING",
] as const;
export type KillChainStage = (typeof KILL_CHAIN_STAGES)[number];

export interface StageSpanDoc {
  stage: KillChainStage | (string & {});
  first_ts: string;
  entries?: number;
  tx_ids?: string[];
  keys?: string[];
}

export interface KillChainDoc {
  case_id: string;
  most_advanced: KillChainStage | (string & {}) | null;
  stages: StageSpanDoc[];
  entries: Array<{ contributor_id: string; tx_id: string; stage: string; key: string; mapped: boolean }>;
  unmapped: Array<{ contributor_id: string; tx_id: string }>;
  default_stage: string;
  multiplier: {
    enabled: boolean;
    value: number;
    applied: boolean;
    expected_loss: string;
    expected_loss_base: string | null;
    priority: string;
  };
}

export interface TechniqueDoc {
  technique_id: string;
  name: string;
  domain: string;
  kill_chain_stage?: string;
  description?: string;
}

export interface CaseTechniquesDoc {
  case_id: string;
  catalogue_version: string;
  notice: string;
  techniques: TechniqueDoc[];
  contributors: Array<{ contributor_id: string; tx_id?: string; ctype?: string; techniques: string[] }>;
}

export interface CoverageContributor {
  contributor_id: string;
  kind: string;
  version?: number;
}

export interface CoverageRow extends TechniqueDoc {
  contributors: CoverageContributor[];
  detection_rate: number | null;
  campaigns: number;
  gap: boolean;
}

export interface CoverageDoc {
  catalogue_version: string;
  notice: string;
  generated_at?: string;
  summary: { techniques: number; covered: number; gaps: number };
  gaps: string[];
  techniques: CoverageRow[];
}

// --- Detection rules (Req 31) -----------------------------------------------------------------

export interface RuleVersionSummary {
  version: number;
  backtested?: boolean;
  accepted_at?: string;
  activated_at?: string | null;
  activated_by?: string | null;
  [key: string]: unknown;
}

export interface RuleSummary {
  rule_id: string;
  version: number;
  title: string;
  status: string;
  target: string;
  techniques: string[];
  mass: { fraud: number; legit: number };
  latest_version: number;
  active_version: number | null;
  versions: RuleVersionSummary[];
}

export interface RulesDoc {
  rules: RuleSummary[];
  eval_errors: number;
  backtest_window_s: number;
}

export interface BacktestDoc {
  hits: number;
  estimated_false_positives: number;
  window_s?: number;
  transactions_scanned?: number;
  auth_events_scanned?: number;
  sample_hits?: string[];
  notes?: string[];
  [key: string]: unknown;
}

export interface SubmittedRule {
  rule_id: string;
  version: number;
  title: string;
  backtested?: boolean;
  backtest: BacktestDoc | null;
  active_version: number | null;
  [key: string]: unknown;
}

/** One problem of a rejected Detection_Rule (422 errors[]): 1-based line, field, cause. */
export interface RuleProblem {
  line: number | null;
  field: string;
  reason: string;
}

// --- Playbooks (Req 32) ---------------------------------------------------------------------

export interface PlaybookDoc {
  id: string;
  version: number;
  title: string;
  description?: string;
  enabled: boolean;
  trigger: { tiers?: string[]; min_tier?: string; techniques?: string[]; match?: string };
  actions: Array<{ type: string; params?: Record<string, string> }>;
  active: boolean;
}

export interface PlaybooksDoc {
  playbooks: PlaybookDoc[];
  dry_run: boolean;
  approval_threshold: string;
  actions: Record<string, number>;
  simulated_state: Record<string, number>;
  notice: string;
}

export interface PlaybookActionDoc {
  action_id: string;
  case_id: string;
  type: string;
  status: string;
  targets: string[];
  playbook_id?: string;
  playbook_version?: number;
  case_tier?: string;
  requires_approval?: boolean;
  scheduled_at?: string;
  decided_by?: string | null;
  [key: string]: unknown;
}

export interface PlaybookActionsDoc {
  actions: PlaybookActionDoc[];
  count: number;
  notice?: string;
}

// --- Threat intel and audit (Req 34, 35) -----------------------------------------------------

export interface IndicatorDoc {
  value: string;
  type: string;
  confidence: number;
  source: string;
  first_seen: string;
  last_seen: string;
  expiry: string;
  tlp: string;
  expired: boolean;
}

export interface IndicatorsDoc {
  count: number;
  total: number;
  offset: number;
  limit: number;
  indicators: IndicatorDoc[];
  summary?: Record<string, unknown>;
}

export interface ImportReport {
  imported: number;
  rejected: Array<{ id: string | null; errors: Array<{ field: string; reason: string }> }>;
  skipped: number | unknown[];
  [key: string]: unknown;
}

export interface AuditEntryDoc {
  index: number;
  ts: string;
  category: string;
  actor: string;
  payload: unknown;
  prev_hash: string;
  hash: string;
}

export interface AuditPageDoc {
  total: number;
  offset?: number;
  limit?: number;
  head: { index?: number | null; hash?: string | null; [key: string]: unknown } | null;
  entries: AuditEntryDoc[];
}

export interface AuditVerifyDoc {
  valid: boolean;
  first_broken_index: number | null;
  head?: unknown;
  entries: number;
  reason: string | null;
  notice: string;
}

// --- SOC console, canaries, resilience, purple team (Req 33, 36, 37) ---------------------------

export interface SlaTimerDoc {
  name?: string;
  target_s: number;
  elapsed_s: number;
  remaining_s: number;
  running: boolean;
  breached: boolean;
}

export interface ConsoleCaseDoc {
  case_id: string;
  status: string;
  tier: string;
  assignee: string | null;
  age_s: number;
  breached: boolean;
  label: string | null;
  breached_timers: string[];
  timers: Record<string, SlaTimerDoc>;
}

export interface HandoffNoteDoc {
  note_id: number;
  author: string;
  ts: string;
  text: string;
}

export interface ConsoleDoc {
  now: string;
  open_cases: number;
  breached_cases: number;
  cases: ConsoleCaseDoc[];
  workload: Array<{ investigator: string; in_review: number }>;
  mttd: { mean_s: number | null; cases: number };
  mttr: { mean_s: number | null; cases: number };
  handoff_notes: HandoffNoteDoc[];
}

export interface CanaryHitDoc {
  canary_id: string;
  kind: string;
  field: string;
  event_id: string;
  event_type: string;
  ts: string | null;
}

export interface CanaryHitsDoc {
  hit_count: number;
  hits: CanaryHitDoc[];
  touched: Array<{ canary_id: string; kind: string; first_reference: string | null; hits: number }>;
}

export interface ResilienceSettings {
  enabled: boolean;
  sensor_failure_rate?: number;
  delay_rate?: number;
  burst_rate?: number;
  delay_events?: number;
  burst_size?: number;
  seed?: number;
}

export interface ResilienceDoc {
  enabled: boolean;
  rates: Omit<ResilienceSettings, "enabled">;
  counters: Record<string, unknown>;
  queue: { length: number | null; held?: number; peak_while_enabled?: number };
  latency_ms: unknown;
  [key: string]: unknown;
}

/** Attacker pattern types a purple-team campaign can use (helix.attacker.genome.PatternType). */
export const PATTERN_TYPES = [
  "ROUND_TRIP_CYCLE",
  "FAN_OUT",
  "FAN_IN",
  "STRUCTURING",
  "MULE_CHAIN",
  "ACCOUNT_TAKEOVER",
  "CARD_TESTING",
] as const;
export type PatternTypeName = (typeof PATTERN_TYPES)[number];

export interface ExerciseSpec {
  name: string;
  campaigns: Array<{ ptype: PatternTypeName; seed: number; count: number }>;
  background_events: number;
}

export interface ScorecardMetrics {
  campaigns: number;
  detected_campaigns: number;
  campaign_detection_rate: number | null;
  events: number;
  detected_events: number;
  detection_rate: number | null;
  median_detection_latency_s: number | null;
  alert_volume: number;
  false_positives: number;
  [key: string]: number | null;
}

export interface ScorecardDoc {
  scorecard_id: string;
  name: string;
  created_at: string;
  metrics: ScorecardMetrics;
  techniques: Array<{ technique_id: string; name?: string; covered?: boolean; detection_rate?: number | null; campaigns?: number }>;
  campaigns: Array<{ campaign_id: string; ptype: string; detected: boolean; events: number; detected_events: number }>;
  notes?: string[];
  [key: string]: unknown;
}

export interface ScorecardSummary {
  scorecard_id: string;
  name: string;
  created_at: string;
  metrics: ScorecardMetrics;
}

export interface MetricDiff {
  a: unknown;
  b: unknown;
  delta?: number | null;
  changed?: boolean;
}

export interface CompareDoc {
  a: string;
  b: string;
  metrics: Record<string, MetricDiff>;
  techniques: Record<string, Record<string, MetricDiff>>;
}

/** A purple-team exercise runs synchronously on the server; give it far longer than a read. */
export const EXERCISE_TIMEOUT_MS = 180_000;

// --- calls ---------------------------------------------------------------------------------------

const read = { timeoutMs: READ_TIMEOUT_MS };

export const socApi = {
  killChain: (caseId: string, signal?: AbortSignal) =>
    request<KillChainDoc>(`/cases/${enc(caseId)}/killchain`, { ...read, signal }),
  caseTechniques: (caseId: string, signal?: AbortSignal) =>
    request<CaseTechniquesDoc>(`/cases/${enc(caseId)}/techniques`, { ...read, signal }),
  coverage: (signal?: AbortSignal) => request<CoverageDoc>("/soc/coverage", { ...read, signal }),

  rules: (signal?: AbortSignal) => request<RulesDoc>("/soc/rules", { ...read, signal }),
  /** Store a new version and backtest it: 201, or ApiError 422 with errors [{line, field, reason}]. */
  submitRule: (yaml: string) => request<SubmittedRule>("/soc/rules", { method: "POST", body: { yaml }, timeoutMs: READ_TIMEOUT_MS }),
  /** Re-run the backtest of one stored version over the stored events of the window (Req 31.4). */
  backtestRule: (ruleId: string, version: number) =>
    request<BacktestDoc>(`/soc/rules/${enc(ruleId)}/versions/${version}/backtest`, {
      method: "POST",
      body: {},
      timeoutMs: READ_TIMEOUT_MS,
    }),
  /** 409 when the version was not backtested, 503 when cyber fusion is off. */
  activateRule: (ruleId: string, by: string, version?: number) =>
    request<{ rule_id: string; active_version: number }>(`/soc/rules/${enc(ruleId)}/activate`, {
      method: "POST",
      body: version === undefined ? { by } : { version, by },
    }),
  deactivateRule: (ruleId: string) =>
    request<{ rule_id: string; active_version: null; previous_active_version: number | null }>(
      `/soc/rules/${enc(ruleId)}/deactivate`,
      { method: "POST" },
    ),

  playbooks: (signal?: AbortSignal) => request<PlaybooksDoc>("/soc/playbooks", { ...read, signal }),
  pendingActions: (signal?: AbortSignal) =>
    request<PlaybookActionsDoc>("/soc/playbook-actions?status=PENDING", { ...read, signal }),
  approveAction: (actionId: string, by: string) =>
    request<PlaybookActionDoc>(`/soc/playbook-actions/${enc(actionId)}/approve`, { method: "POST", body: { by } }),
  rejectAction: (actionId: string, by: string, reason?: string) =>
    request<PlaybookActionDoc>(`/soc/playbook-actions/${enc(actionId)}/reject`, {
      method: "POST",
      body: reason ? { by, reason } : { by },
    }),
  setDryRun: (enabled: boolean, by: string) =>
    request<{ dry_run: boolean }>("/soc/playbooks/dry-run", { method: "POST", body: { enabled, by } }),

  indicators: (offset = 0, limit = 100, signal?: AbortSignal) =>
    request<IndicatorsDoc>(`/soc/indicators?offset=${offset}&limit=${limit}`, { ...read, signal }),
  /** STIX 2.1-style bundle; TLP:RED Indicators only when ``includeRed`` (Req 34.8). */
  exportIndicators: (includeRed: boolean) =>
    request<Record<string, unknown>>(`/soc/indicators/export?include_red=${includeRed ? "true" : "false"}`, read),
  /** Body is the bundle's JSON text, sent as is. */
  importIndicators: (bundleJson: string) =>
    request<ImportReport>("/soc/indicators/import", { method: "POST", rawBody: bundleJson, timeoutMs: READ_TIMEOUT_MS }),

  audit: (offset = 0, limit = 50, signal?: AbortSignal) =>
    request<AuditPageDoc>(`/soc/audit?offset=${offset}&limit=${limit}`, { ...read, signal }),
  verifyAudit: () => request<AuditVerifyDoc>("/soc/audit/verify", read),

  console: (signal?: AbortSignal) => request<ConsoleDoc>("/soc/console", { ...read, signal }),
  handoffNotes: (limit = 100, signal?: AbortSignal) =>
    request<{ notes: HandoffNoteDoc[]; total: number }>(`/soc/handoff-notes?limit=${limit}`, { ...read, signal }),
  addHandoffNote: (author: string, text: string) =>
    request<HandoffNoteDoc>("/soc/handoff-notes", { method: "POST", body: { author, text } }),

  canaryHits: (signal?: AbortSignal) => request<CanaryHitsDoc>("/soc/canaries/hits", { ...read, signal }),

  resilience: (signal?: AbortSignal) => request<ResilienceDoc>("/soc/resilience", { ...read, signal }),
  setResilience: (settings: ResilienceSettings) =>
    request<ResilienceDoc>("/soc/resilience", { method: "POST", body: settings }),

  /** Runs the whole exercise before answering: 201 scorecard, 409 while another runs. */
  runExercise: (spec: ExerciseSpec) =>
    request<ScorecardDoc>("/soc/purple/exercises", { method: "POST", body: spec, timeoutMs: EXERCISE_TIMEOUT_MS }),
  scorecards: (signal?: AbortSignal) =>
    request<{ scorecards: ScorecardSummary[] }>("/soc/purple/scorecards", { ...read, signal }),
  compareScorecards: (a: string, b: string) => request<CompareDoc>(`/soc/purple/compare?a=${enc(a)}&b=${enc(b)}`, read),
};

export type SocApi = typeof socApi;

// --- availability ---------------------------------------------------------------------------------

export const SOC_ABSENT_MESSAGE = "SOC routes not available on this server";
export const SOC_DISABLED_MESSAGE = "Cyber fusion is disabled (start with --soc)";

export type Unavailable = "absent" | "disabled";

/** "absent": the route itself does not exist (FastAPI's generic 404 "Not Found", as opposed to
 *  an unknown Case or rule, which carries its own detail). "disabled": 503, soc.enabled off. */
export function unavailableKind(err: unknown): Unavailable | null {
  if (!(err instanceof ApiError) || err.kind !== "http") return null;
  if (err.status === 404 && err.detail === "Not Found") return "absent";
  if (err.status === 503) return "disabled";
  return null;
}

/** One line for an error of a SOC call, using the two availability messages where they apply. */
export function describeSocError(err: unknown): string {
  const u = unavailableKind(err);
  if (u === "absent") return `${SOC_ABSENT_MESSAGE}.`;
  if (u === "disabled") return `${SOC_DISABLED_MESSAGE}.`;
  if (err instanceof ApiError) return err.describe();
  return err instanceof Error ? err.message : String(err);
}

/** The optional "soc" key of GET /status; null when the server does not report it. */
export function socStatusOf(status: unknown): { enabled: boolean } | null {
  if (typeof status !== "object" || status === null) return null;
  const soc = (status as Record<string, unknown>).soc;
  if (typeof soc !== "object" || soc === null) return null;
  const enabled = (soc as Record<string, unknown>).enabled;
  return typeof enabled === "boolean" ? { enabled } : null;
}

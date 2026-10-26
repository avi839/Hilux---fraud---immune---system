// View-model for the LoopPanel. Reads /ws/metrics frames and GET /metrics documents
// (backend helix/api/app.py get_metrics, state.py _on_metrics) and the optional /immune
// and /attacker documents. Anything the server does not report stays null and is shown as
// "n/a"; nothing is invented.

export const LOOP_STAGES = ["SENSE", "PERCEIVE", "REMEMBER", "REASON", "ACT", "LEARN"] as const;
export type LoopStage = (typeof LOOP_STAGES)[number];

/** Stages the backend's LatencyRecorder measures (helix.core.Stage), plus END_TO_END. */
export const MEASURED_STAGES = ["PERCEIVE", "REMEMBER", "REASON", "ACT", "END_TO_END"] as const;

export const NOT_APPLICABLE = "not applicable";
export type Ratio = number | typeof NOT_APPLICABLE;

export interface StageView {
  meanMs: number | null;
  /** Events through the stage since start (monotonic counter). */
  total: number | null;
}

export interface BreachView {
  active: boolean;
  count: number | null;
  thresholdMs: number | null;
  p95Ms: number | null;
}

export interface EvaluationView {
  precision: Ratio | null;
  recall: Ratio | null;
  alertsPerHour: Ratio | null;
  latencyMedianS: number | null;
  latencyMeanS: number | null;
}

/** Everything the panel shows. Missing keys mean "this source did not report it". */
export interface LoopView {
  stages: Record<string, StageView>;
  ingestRate: number | null;
  queueLength: number | null;
  breach: BreachView | null;
  /** NOT_APPLICABLE when the server has no Evaluation_Monitor (``evaluation: null``). */
  evaluation: EvaluationView | typeof NOT_APPLICABLE;
  weights: Record<string, number>;
  /** Investigator workload (GET /metrics "workload", Req 41.10); REST only. */
  workload: WorkloadView;
}

export interface WorkloadView {
  labelledLastHour: number | null;
  labelledLastHourBy: Record<string, number>;
  labelledTotal: number | null;
  autoClearedTotal: number | null;
  autoClearedLastHour: number | null;
  bulkRequestsTotal: number | null;
}

export function workloadOf(v: unknown): WorkloadView | null {
  if (!isObj(v)) return null;
  const by: Record<string, number> = {};
  if (isObj(v.labelled_last_hour_by)) {
    for (const [k, x] of Object.entries(v.labelled_last_hour_by)) {
      const n = num(x);
      if (n !== null) by[k] = n;
    }
  }
  return {
    labelledLastHour: num(v.labelled_last_hour),
    labelledLastHourBy: by,
    labelledTotal: num(v.labelled_total),
    autoClearedTotal: num(v.auto_cleared_total),
    autoClearedLastHour: num(v.auto_cleared_last_hour),
    bulkRequestsTotal: num(v.bulk_requests_total),
  };
}

const isObj = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const ratio = (v: unknown): Ratio | null => (v === NOT_APPLICABLE ? NOT_APPLICABLE : num(v));

function weightsOf(v: unknown): Record<string, number> | null {
  if (!isObj(v)) return null;
  const out: Record<string, number> = {};
  for (const [k, x] of Object.entries(v)) {
    const n = num(x);
    if (n === null) return null;
    out[k] = n;
  }
  return Object.keys(out).length ? out : null;
}

/** Normalise a /ws/metrics frame or a GET /metrics document. Only reported keys are set,
 *  so merging a later frame over an earlier view replaces just what that frame reports. */
export function normaliseMetrics(doc: unknown): Partial<LoopView> {
  if (!isObj(doc)) return {};
  const out: Partial<LoopView> = {};
  if (isObj(doc.stages)) {
    const stages: Record<string, StageView> = {};
    for (const [name, s] of Object.entries(doc.stages)) {
      if (isObj(s)) stages[name] = { meanMs: num(s.mean_ms), total: num(s.total) };
    }
    out.stages = stages;
  }
  if (isObj(doc.ingest)) {
    out.ingestRate = num(doc.ingest.rate_eps);
    out.queueLength = num(doc.ingest.queue_length);
  }
  const b = doc.latency_breach;
  if (isObj(b)) {
    out.breach = { active: b.active === true, count: num(b.count), thresholdMs: num(b.threshold_ms), p95Ms: num(b.p95_ms) };
  }
  if ("evaluation" in doc) {
    const e = doc.evaluation;
    out.evaluation = isObj(e)
      ? {
          precision: ratio(e.precision),
          recall: ratio(e.recall),
          alertsPerHour: ratio(e.alerts_per_hour),
          latencyMedianS: num(e.latency_median_s),
          latencyMeanS: num(e.latency_mean_s),
        }
      : NOT_APPLICABLE;
  }
  // Current Sensor_Weights: GET /metrics (backend task 11) does not report them. Read them
  // if a server version adds them at the top level or under "loop"; never fabricate them.
  const loop = isObj(doc.loop) ? doc.loop : {};
  const w = weightsOf(doc.weights) ?? weightsOf(doc.sensor_weights) ?? weightsOf(loop.weights) ?? weightsOf(loop.sensor_weights);
  if (w) out.weights = w;
  const wl = workloadOf(doc.workload);
  if (wl) out.workload = wl;
  return out;
}

export function mergeView(prev: Partial<LoopView>, next: Partial<LoopView>): Partial<LoopView> {
  return { ...prev, ...next };
}

// --- throughput from counters over time ------------------------------------------------

export interface Sample {
  at: number; // ms, wall clock of receipt
  totals: Record<string, number>;
}

export const SAMPLE_HORIZON_MS = 10_000;

export function sampleOf(view: Partial<LoopView>, at: number): Sample | null {
  if (!view.stages) return null;
  const totals: Record<string, number> = {};
  for (const [k, s] of Object.entries(view.stages)) if (s.total !== null) totals[k] = s.total;
  return Object.keys(totals).length ? { at, totals } : null;
}

/** Append a sample, keep those within the horizon (at least two), and restart the series if
 *  any counter went backwards (server restart). */
export function pushSample(samples: Sample[], s: Sample | null, horizonMs = SAMPLE_HORIZON_MS): Sample[] {
  if (!s) return samples;
  const last = samples[samples.length - 1];
  if (last && s.at <= last.at) return samples;
  if (last && Object.entries(s.totals).some(([k, v]) => (last.totals[k] ?? 0) > v)) return [s];
  const out = [...samples, s];
  while (out.length > 2 && out[1]!.at <= s.at - horizonMs) out.shift();
  return out;
}

/** Events per second through ``stage`` between the oldest and newest sample, or null with
 *  fewer than two samples. */
export function throughput(samples: Sample[], stage: string): number | null {
  if (samples.length < 2) return null;
  const a = samples[0]!;
  const b = samples[samples.length - 1]!;
  const x0 = a.totals[stage];
  const x1 = b.totals[stage];
  const dt = (b.at - a.at) / 1000;
  if (x0 === undefined || x1 === undefined || dt <= 0) return null;
  return (x1 - x0) / dt;
}

export interface StageRow {
  stage: LoopStage | "END_TO_END";
  throughput: number | null;
  meanMs: number | null;
  note?: string;
}

/** SENSE throughput is the ingest rate (accepted events/s over 60 s); SENSE latency and the
 *  LEARN stage are not instrumented by the API_Server and stay null. */
export function stageRows(view: Partial<LoopView>, samples: Sample[]): StageRow[] {
  const rows: StageRow[] = [];
  for (const stage of LOOP_STAGES) {
    if (stage === "SENSE") {
      rows.push({ stage, throughput: view.ingestRate ?? null, meanMs: null, note: "ingest latency is not measured" });
    } else if (stage === "LEARN") {
      rows.push({ stage, throughput: null, meanMs: null, note: "not instrumented by the API_Server" });
    } else {
      rows.push({ stage, throughput: throughput(samples, stage), meanMs: view.stages?.[stage]?.meanMs ?? null });
    }
  }
  rows.push({ stage: "END_TO_END", throughput: throughput(samples, "END_TO_END"), meanMs: view.stages?.END_TO_END?.meanMs ?? null });
  return rows;
}

// --- optional endpoints ------------------------------------------------------------------

/** Antibody_Pool size from an /immune document, tolerating a few plausible shapes; null when
 *  none is present. */
export function poolSize(doc: unknown): { size: number; max: number | null } | null {
  if (!isObj(doc)) return null;
  const pool = doc.pool;
  const max =
    num(doc.max_pool) ?? num(doc.pool_max) ?? num(doc.max_pool_size) ?? (isObj(pool) ? (num(pool.max) ?? num(pool.max_size)) : null);
  const direct = num(doc.pool_size) ?? num(doc.active_pool_size) ?? num(doc.antibody_pool_size) ?? num(doc.active_count);
  if (direct !== null) return { size: direct, max };
  if (typeof pool === "number") return { size: pool, max };
  if (Array.isArray(pool)) return { size: pool.length, max };
  if (isObj(pool)) {
    const s = num(pool.size) ?? num(pool.active);
    if (s !== null) return { size: s, max };
  }
  if (num(doc.active) !== null) return { size: num(doc.active)!, max };
  if (Array.isArray(doc.active)) return { size: doc.active.length, max };
  return null;
}

export function attackerRunning(doc: unknown): boolean | null {
  if (!isObj(doc)) return null;
  if (typeof doc.running === "boolean") return doc.running;
  if (typeof doc.state === "string") return doc.state.toLowerCase() === "running";
  return null;
}

export function formatRatio(r: Ratio | null | undefined, dp = 3): string {
  if (r === NOT_APPLICABLE) return NOT_APPLICABLE;
  return typeof r === "number" ? r.toFixed(dp) : "n/a";
}

import { useEffect, useState } from "react";
import { api, ApiError } from "../../api/client";
import { formatTime } from "../../format";
import { useDashboard } from "../../store/store";
import { Panel } from "../common/Panel";
import { DatasetPanel } from "../Dataset/DatasetPanel";
import {
  attackerRunning,
  formatRatio,
  mergeView,
  normaliseMetrics,
  NOT_APPLICABLE,
  poolSize,
  pushSample,
  sampleOf,
  stageRows,
  type BreachView,
  type EvaluationView,
  type LoopView,
  type Ratio,
  type Sample,
  type StageRow,
  type WorkloadView,
} from "./loop";

/** REST refresh period. /ws/metrics frames arrive more often (every 0.5 s while
 *  subscribed); polling keeps the panel within the 5 s refresh bound without them. */
export const POLL_MS = 4_000;
/** Re-check period of an optional endpoint (/immune, /attacker) the server does not have. */
export const ABSENT_RECHECK_MS = 60_000;
const SKIPPED = Symbol("skipped");
/** Ingest queue length above which the panel warns that processing is falling behind. */
export const QUEUE_BEHIND = 200;

export type Optional =
  | { kind: "unknown" }
  | { kind: "ok"; doc: Record<string, unknown> }
  | { kind: "absent" }
  | { kind: "error"; message: string };

function optional(r: PromiseSettledResult<Record<string, unknown>>): Optional {
  if (r.status === "fulfilled") return { kind: "ok", doc: r.value };
  const e = r.reason;
  if (e instanceof ApiError && e.kind === "http" && (e.status === 404 || e.status === 501)) return { kind: "absent" };
  return { kind: "error", message: e instanceof ApiError ? e.describe() : String(e) };
}

export const n2 = (x: number | null, dp = 2) => (x === null ? "n/a" : x.toFixed(dp));
const count = (x: number | null) => (x === null ? "n/a" : String(x));

const STAGE_LABEL: Record<string, string> = { END_TO_END: "End to end" };

/** Everything the loop views read: the merged metrics view, the counter samples for
 *  throughput, the optional /immune and /attacker documents and the last refresh. */
export interface LoopData {
  view: Partial<LoopView>;
  samples: Sample[];
  immune: Optional;
  attacker: Optional;
  metricsError: string | null;
  updatedAt: number | null;
}

/** Loop metrics from /ws/metrics frames (via the store) plus a REST poll of GET /metrics and
 *  the optional /immune and /attacker. Call it once per page and share the result. */
export function useLoopData(pollMs: number = POLL_MS): LoopData {
  const ws = useDashboard((s) => s.metrics);
  const [view, setView] = useState<Partial<LoopView>>({});
  const [samples, setSamples] = useState<Sample[]>([]);
  const [immune, setImmune] = useState<Optional>({ kind: "unknown" });
  const [attacker, setAttacker] = useState<Optional>({ kind: "unknown" });
  const [metricsError, setMetricsError] = useState<string | null>(null);
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);

  const apply = (doc: unknown) => {
    const v = normaliseMetrics(doc);
    const now = Date.now();
    setView((prev) => mergeView(prev, v));
    setSamples((prev) => pushSample(prev, sampleOf(v, now)));
    setUpdatedAt(now);
  };

  // Live frames from /ws/metrics (stages, ingest, latency breach; no evaluation).
  useEffect(() => {
    if (ws) apply(ws);
  }, [ws]);

  // REST poll: GET /metrics (adds evaluation) plus the optional /immune and /attacker. An
  // optional endpoint that answered 404/501 is asked again only every ABSENT_RECHECK_MS,
  // so a server without it is not hit (and the console not filled) every few seconds.
  useEffect(() => {
    let alive = true;
    let ctrl: AbortController | null = null;
    const absentUntil = { immune: 0, attacker: 0 };
    const skipped = Promise.reject<Record<string, unknown>>(SKIPPED);
    skipped.catch(() => undefined);
    const optionalCall = (name: "immune" | "attacker", call: () => Promise<Record<string, unknown>>) =>
      Date.now() < absentUntil[name] ? skipped : call();
    const note = (name: "immune" | "attacker", r: PromiseSettledResult<Record<string, unknown>>, set: (o: Optional) => void) => {
      if (r.status === "rejected" && r.reason === SKIPPED) return; // keep the last state
      const o = optional(r);
      if (o.kind === "absent") absentUntil[name] = Date.now() + ABSENT_RECHECK_MS;
      set(o);
    };
    const tick = async () => {
      ctrl?.abort();
      const c = new AbortController();
      ctrl = c;
      const [m, im, at] = await Promise.allSettled([
        api.getMetrics(c.signal),
        optionalCall("immune", () => api.getImmune(c.signal)),
        optionalCall("attacker", () => api.getAttacker(c.signal)),
      ]);
      if (!alive || c.signal.aborted) return;
      if (m.status === "fulfilled") {
        apply(m.value);
        setMetricsError(null);
      } else {
        setMetricsError(m.reason instanceof ApiError ? m.reason.describe() : String(m.reason));
      }
      note("immune", im, setImmune);
      note("attacker", at, setAttacker);
    };
    void tick();
    const timer = setInterval(() => void tick(), pollMs);
    return () => {
      alive = false;
      clearInterval(timer);
      ctrl?.abort();
    };
  }, [pollMs]);

  return { view, samples, immune, attacker, metricsError, updatedAt };
}

/** The loop stages as a compact row of tiles. It is an ARIA table (stage = row header,
 *  events/s and mean ms = cells), so the tiles keep their table semantics under any CSS. */
export function StageStrip({ rows }: { rows: StageRow[] }) {
  return (
    <div role="table" className="loop-stages" aria-label="Loop stages: events per second and mean latency over the rolling window" data-testid="loop-stages">
      <div role="rowgroup" className="sr-only">
        <div role="row">
          <span role="columnheader">Stage</span>
          <span role="columnheader">Events/s</span>
          <span role="columnheader">Mean ms</span>
        </div>
      </div>
      <div role="rowgroup" className="loop-stages__body">
        {rows.map((r) => (
          <div
            role="row"
            key={r.stage}
            data-testid={`stage-${r.stage}`}
            className={`loop-stage${r.stage === "END_TO_END" ? " loop-stage--total" : ""}`}
            title={r.note ? `${STAGE_LABEL[r.stage] ?? r.stage}: ${r.note}` : undefined}
          >
            <span role="rowheader" className="loop-stage__name">
              {STAGE_LABEL[r.stage] ?? r.stage}
              {r.note ? <span className="sr-only"> ({r.note})</span> : null}
            </span>
            <span role="cell" className="loop-stage__value" data-unit="ev/s">
              {n2(r.throughput, 1)}
            </span>
            <span role="cell" className="loop-stage__value loop-stage__value--ms" data-unit="ms">
              {n2(r.meanMs, 3)}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

/** Latency target state as a text chip (never colour alone). */
export function LatencyBreach({ breach }: { breach: BreachView | null }) {
  return (
    <p className="loop-breach" data-testid="loop-breach">
      {breach === null ? (
        <span className="badge">Latency target: n/a</span>
      ) : breach.active ? (
        <span className="badge badge--warn">
          Latency breach: p95 {n2(breach.p95Ms)} ms &gt; {n2(breach.thresholdMs, 0)} ms target
        </span>
      ) : (
        <span className="badge badge--ok">
          Within latency target: p95 {n2(breach.p95Ms)} ms ≤ {n2(breach.thresholdMs, 0)} ms
        </span>
      )}
      {breach?.count !== null && breach?.count !== undefined ? <span className="muted small"> · {breach.count} breaches so far</span> : null}
    </p>
  );
}

/** Current Sensor_Weights as small inline bars, scaled to the largest weight. */
export function SensorWeights({ weights }: { weights: Record<string, number> | undefined }) {
  const entries = weights ? Object.entries(weights).sort(([a], [b]) => (a < b ? -1 : 1)) : [];
  const max = Math.max(1e-9, ...entries.map(([, w]) => Math.abs(w)));
  // Compare at the displayed precision, so "1.00" everywhere really reads as the start.
  const atStart = entries.length > 0 && entries.every(([, w]) => w.toFixed(2) === START_WEIGHT.toFixed(2));
  const moved = atStart ? [] : entries.filter(([, w]) => w.toFixed(2) !== START_WEIGHT.toFixed(2));
  return (
    <div className="loop-weights">
      <h3 className="loop-sub">Sensor weights</h3>
      <div data-testid="loop-weights">
        {weights ? (
          <ul className="loop-weights__list">
            {entries.map(([k, w]) => (
              <li key={k} className="loop-weight">
                {k} <strong>{w.toFixed(2)}</strong>
                <span className="loop-weight__bar" aria-hidden="true">
                  <span style={{ width: `${Math.round((Math.abs(w) / max) * 100)}%` }} />
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted small loop-na">
            n/a <span>(current weights are not exposed by GET /metrics)</span>
          </p>
        )}
      </div>
      {atStart ? (
        <p className="muted small loop-note" data-testid="loop-weights-note">
          {WEIGHTS_AT_START_TEXT}
        </p>
      ) : moved.length ? (
        <p className="muted small loop-note" data-testid="loop-weights-delta">
          Change from the starting weight 1.00 after labels: {moved.map(([k, w]) => `${k} ${signed(w - START_WEIGHT)}`).join(", ")}
        </p>
      ) : null}
    </div>
  );
}

export const SIM_ANALYST_HELP =
  "Demo only: a simulated investigator labels top Cases from generator ground truth, with 5% errors, through the normal label path.";

/** "Simulated analyst on" chip, from GET /status "sim_investigator" (via the store). Nothing
 *  is shown when it is off or the server does not report it. */
export function SimAnalystChip() {
  const sim = useDashboard((s) => s.simAnalyst);
  if (!sim?.enabled) return null;
  const n = sim.labelsTotal;
  return (
    <p className="small loop-sim" data-testid="sim-analyst">
      <span className="badge" title={SIM_ANALYST_HELP}>
        Simulated analyst on · {n} label{n === 1 ? "" : "s"} ({sim.confirmed} fraud, {sim.falsePositive} false positive)
      </span>
      <span className="sr-only"> {SIM_ANALYST_HELP}</span>
    </p>
  );
}

/** Starting (and maximum) Sensor_Weight; feedback only lowers weights from here. */
export const START_WEIGHT = 1;
export const WEIGHTS_AT_START_TEXT =
  "All sensors are at the starting weight 1.00 (the maximum). Weights move only when investigators label Cases; a false-positive label lowers the sensors that blamed it.";

const signed = (d: number) => `${d >= 0 ? "+" : "−"}${Math.abs(d).toFixed(2)}`;

function absentNote(o: Optional, path: string): string {
  return o.kind === "absent" ? `(no ${path} endpoint) · not available yet on this server` : o.kind === "error" ? `(GET ${path} failed)` : "";
}

/** Antibody_Pool size and Attacker_Agent state. */
export function ImmuneStatus({ immune, attacker }: { immune: Optional; attacker: Optional }) {
  const pool = immune.kind === "ok" ? poolSize(immune.doc) : null;
  const running = attacker.kind === "ok" ? attackerRunning(attacker.doc) : null;
  return (
    <dl className="kv">
      <div>
        <dt>Antibody_Pool size</dt>
        <dd data-testid="loop-pool">
          {pool ? (
            <>
              <strong>{pool.size}</strong>
              {pool.max !== null ? ` / ${pool.max}` : ""}
            </>
          ) : (
            <>
              n/a <span className="muted small">{absentNote(immune, "/immune")}</span>
            </>
          )}
        </dd>
      </div>
      <div>
        <dt>Attacker_Agent</dt>
        <dd data-testid="loop-attacker">
          {running === null ? (
            <>
              n/a <span className="muted small">{absentNote(attacker, "/attacker")}</span>
            </>
          ) : running ? (
            "running"
          ) : (
            "stopped"
          )}
        </dd>
      </div>
    </dl>
  );
}

/** Evaluation_Monitor metrics, or why there are none. */
export function EvaluationBlock({ ev }: { ev: EvaluationView | typeof NOT_APPLICABLE | undefined }) {
  return (
    <>
      <h3 className="loop-sub">Evaluation</h3>
      {ev === undefined ? (
        <p className="muted" data-testid="loop-eval">
          n/a
        </p>
      ) : ev === NOT_APPLICABLE ? (
        <p className="muted" data-testid="loop-eval">
          Not applicable: no Evaluation_Monitor (ground truth) is configured.
        </p>
      ) : (
        <dl className="kv" data-testid="loop-eval">
          <div>
            <dt>Precision</dt>
            <dd>{formatRatio(ev.precision)}</dd>
          </div>
          <div>
            <dt>Recall</dt>
            <dd>{formatRatio(ev.recall)}</dd>
          </div>
          <div>
            <dt>Alerts per hour</dt>
            <dd>{formatRatio(ev.alertsPerHour, 2)}</dd>
          </div>
          <div>
            <dt>Detection latency</dt>
            <dd>
              {ev.latencyMedianS === null && ev.latencyMeanS === null
                ? NOT_APPLICABLE
                : `median ${n2(ev.latencyMedianS, 1)} s, mean ${n2(ev.latencyMeanS, 1)} s`}
            </dd>
          </div>
        </dl>
      )}
      {ev !== undefined && ev !== NOT_APPLICABLE && nothingDetected(ev) ? (
        <p className="muted small loop-note" data-testid="loop-eval-note">
          No fraud detected yet in this run: precision, recall and detection latency fill in once a Case catches labelled
          fraud.
        </p>
      ) : null}
    </>
  );
}

const zeroOrNa = (r: Ratio | null) => r === null || r === NOT_APPLICABLE || r === 0;

/** True when the Evaluation_Monitor has nothing to report yet: precision and recall are 0 or
 *  not applicable and no detection latency was measured. */
export function nothingDetected(ev: EvaluationView): boolean {
  return zeroOrNa(ev.precision) && zeroOrNa(ev.recall) && ev.latencyMedianS === null && ev.latencyMeanS === null;
}

/** Investigator workload from GET /metrics (Req 41.10). */
export function WorkloadBlock({ wl }: { wl: WorkloadView | undefined }) {
  const byName = wl ? Object.entries(wl.labelledLastHourBy).sort(([a], [b]) => (a < b ? -1 : 1)) : [];
  return (
    <>
      <h3 className="loop-sub">Investigator workload</h3>
      {wl ? (
        <dl className="kv" data-testid="loop-workload">
          <div>
            <dt>Labels, last 60 min</dt>
            <dd>
              <strong>{count(wl.labelledLastHour)}</strong>
              {byName.length ? <span className="muted small"> ({byName.map(([k, v]) => `${k} ${v}`).join(", ")})</span> : null}
            </dd>
          </div>
          <div>
            <dt>Labels, total</dt>
            <dd>{count(wl.labelledTotal)}</dd>
          </div>
          <div>
            <dt>Auto-cleared (not labels)</dt>
            <dd>
              {count(wl.autoClearedTotal)} total · {count(wl.autoClearedLastHour)} in the last 60 min
            </dd>
          </div>
          <div>
            <dt>Bulk label requests</dt>
            <dd>{count(wl.bulkRequestsTotal)}</dd>
          </div>
        </dl>
      ) : (
        <p className="muted" data-testid="loop-workload">
          n/a <span className="small">(GET /metrics does not report workload on this server)</span>
        </p>
      )}
      {wl && !wl.labelledTotal && !wl.labelledLastHour ? (
        <p className="muted small loop-note" data-testid="loop-workload-note">
          No labels yet. Open a Case and mark it Confirmed fraud or False positive; each label adjusts the sensor weights.
        </p>
      ) : null}
    </>
  );
}

function LoopPanelView({ data, datasetPollMs }: { data: LoopData; datasetPollMs?: number }) {
  const { view, samples, immune, attacker, metricsError, updatedAt } = data;
  const rows = stageRows(view, samples);

  return (
    <Panel
      title="HELIX loop"
      area="loop"
      actions={
        <span className="muted small" data-testid="loop-updated">
          {updatedAt ? `Updated ${formatTime(new Date(updatedAt).toISOString())}` : "Waiting for metrics…"}
        </span>
      }
    >
      {metricsError ? (
        <p className="msg msg--error small" role="alert">
          GET /metrics failed: {metricsError} Showing the last received values.
        </p>
      ) : null}

      <StageStrip rows={rows} />

      <div className="loop-status">
        <LatencyBreach breach={view.breach ?? null} />
        {view.queueLength !== null && view.queueLength !== undefined ? (
          <p className="small loop-queue" data-testid="loop-queue">
            {view.queueLength > QUEUE_BEHIND ? (
              <span className="badge badge--warn">
                Pipeline behind: {view.queueLength} events waiting in the ingest queue; live views lag behind the input
              </span>
            ) : (
              <span className="muted">Ingest queue: {view.queueLength} waiting</span>
            )}
          </p>
        ) : null}
        <SimAnalystChip />
      </div>

      <SensorWeights weights={view.weights} />

      <details className="ev-details loop-more">
        <summary>More metrics</summary>
        <div className="loop-more__body">
          <ImmuneStatus immune={immune} attacker={attacker} />
          <EvaluationBlock ev={view.evaluation} />
          <WorkloadBlock wl={view.workload} />
          <DatasetPanel pollMs={datasetPollMs} />
        </div>
      </details>
    </Panel>
  );
}

function SelfLoadingLoopPanel({ pollMs, datasetPollMs }: { pollMs: number; datasetPollMs?: number }) {
  const data = useLoopData(pollMs);
  return <LoopPanelView data={data} datasetPollMs={datasetPollMs} />;
}

/** HELIX loop panel (Req 22.9): per-stage throughput and mean latency, latency breach state,
 *  Sensor_Weights, and (under "More metrics") Antibody_Pool size, Attacker_Agent state,
 *  Evaluation_Monitor metrics, investigator workload and the dataset stream. Pass ``data``
 *  from a shared useLoopData() to avoid a second poll; without it the panel polls itself. */
export function LoopPanel({ pollMs = POLL_MS, datasetPollMs, data }: { pollMs?: number; datasetPollMs?: number; data?: LoopData }) {
  return data ? <LoopPanelView data={data} datasetPollMs={datasetPollMs} /> : <SelfLoadingLoopPanel pollMs={pollMs} datasetPollMs={datasetPollMs} />;
}

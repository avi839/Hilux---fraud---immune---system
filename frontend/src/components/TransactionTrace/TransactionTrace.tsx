import { useEffect, useId, useRef, useState, type FormEvent, type ReactNode } from "react";
import { ApiError } from "../../api/client";
import { useDashboard } from "../../store/store";
import { TierBadge } from "../common/TierBadge";
import { ViewHeader } from "../common/ViewHeader";
import { useShellTab } from "../Shell/TabbedDashboard";
import {
  getTrace,
  STAGE_ORDER,
  type FeatureValue,
  type StageName,
  type TraceAssessment,
  type TraceDoc,
  type TraceMass,
} from "./traceApi";
import "./TransactionTrace.css";

const STAGE_HINT: Record<StageName, string> = {
  SENSE: "Ingestion: arrival time and lateness",
  PERCEIVE: "Sensor_Agents' Mass_Functions and the stage-1 Fused_Assessment",
  REMEMBER: "Behavioural_DNA comparison and change-point state",
  REASON: "Motif hits on the temporal graph and the stage-2 Fused_Assessment",
  ACT: "Risk_Score, Severity_Tier and Case",
  LEARN: "Feedback_Label and learning loop",
};

const n4 = (x: number | undefined | null) => (typeof x === "number" && Number.isFinite(x) ? x.toFixed(4) : "-");

function feature(v: FeatureValue): string {
  if (v === null) return "none";
  return typeof v === "number" ? (Number.isInteger(v) ? String(v) : v.toFixed(4)) : v;
}

const clamp01 = (x: number | undefined | null) => (typeof x === "number" && Number.isFinite(x) ? Math.min(1, Math.max(0, x)) : 0);
const pc = (x: number) => `${Math.round(x * 100)}%`;

/** A Mass_Function as one stacked bar: FRAUD, LEGIT, then Θ (uncertain). The numbers sit in
 *  the neighbouring cells; the bar's accessible name repeats them as percentages. */
function MassBar({ m }: { m: TraceMass | null | undefined }) {
  if (!m) return <span className="trace-muted">-</span>;
  const f = clamp01(m.fraud);
  const l = clamp01(m.legit);
  const u = clamp01(m.uncertainty);
  return (
    <span className="trace-mass" role="img" aria-label={`FRAUD ${pc(f)}, LEGIT ${pc(l)}, uncertain ${pc(u)}`}>
      <span className="trace-mass__seg trace-mass__seg--f" style={{ width: pc(f) }} />
      <span className="trace-mass__seg trace-mass__seg--l" style={{ width: pc(l) }} />
      <span className="trace-mass__seg trace-mass__seg--u" style={{ width: pc(u) }} />
    </span>
  );
}

function MassLegend() {
  return (
    <p className="trace-legend" aria-hidden="true">
      <span>
        <i className="trace-mass__seg--f" /> FRAUD
      </span>
      <span>
        <i className="trace-mass__seg--l" /> LEGIT
      </span>
      <span>
        <i className="trace-mass__seg--u" /> Θ uncertain
      </span>
    </p>
  );
}

function MassCells({ m }: { m: TraceMass | null | undefined }) {
  return (
    <>
      <td>{n4(m?.fraud)}</td>
      <td>{n4(m?.legit)}</td>
      <td>{n4(m?.uncertainty)}</td>
      <td className="trace-table__bar">
        <MassBar m={m} />
      </td>
    </>
  );
}

function Assessment({ label, a }: { label: string; a: TraceAssessment | null | undefined }) {
  if (!a) return <p className="trace-muted">{label}: not available.</p>;
  const b = clamp01(a.belief);
  const p = Math.max(b, clamp01(a.plausibility));
  return (
    <dl className="trace-dl">
      <dt>{label}</dt>
      <dd>
        belief {n4(a.belief)}, plausibility {n4(a.plausibility)}, uncertainty {n4(a.uncertainty)}, conflict {n4(a.conflict)}
        {a.high_conflict ? <strong className="trace-flag"> (high conflict)</strong> : null}
        <span
          className="trace-interval"
          role="img"
          aria-label={`Fraud belief ${pc(b)} up to plausibility ${pc(p)}`}
        >
          <span className="trace-interval__b" style={{ width: pc(b) }} />
          <span className="trace-interval__p" style={{ width: pc(p - b) }} />
        </span>
      </dd>
    </dl>
  );
}

function StageBody({ name, trace, openCase }: { name: StageName; trace: TraceDoc; openCase: (id: string) => void }): ReactNode {
  const s = trace.stages;
  switch (name) {
    case "SENSE": {
      const st = s.SENSE ?? {};
      return (
        <dl className="trace-dl">
          <dt>Received at</dt>
          <dd>{st.received_at ?? "not available"}</dd>
          <dt>Lateness</dt>
          <dd>{st.lateness ? `${st.lateness.kind} (${st.lateness.seconds.toFixed(2)} s)` : "not available"}</dd>
        </dl>
      );
    }
    case "PERCEIVE": {
      const sensors = s.PERCEIVE?.sensor_results ?? [];
      return (
        <>
          {sensors.length ? (
            <table className="trace-table">
              <caption className="sr-only">Sensor_Agent Mass_Functions</caption>
              <thead>
                <tr>
                  <th scope="col">Sensor</th>
                  <th scope="col">Outcome</th>
                  <th scope="col">m(FRAUD)</th>
                  <th scope="col">m(LEGIT)</th>
                  <th scope="col">m(Θ)</th>
                  <th scope="col">Split</th>
                  <th scope="col">Note</th>
                </tr>
              </thead>
              <tbody>
                {sensors.map((r) => (
                  <tr key={r.name}>
                    <th scope="row">{r.name}</th>
                    <td>{r.outcome}</td>
                    <MassCells m={r.mass} />
                    <td>{r.error ?? (r.vacuous_reason ? `vacuous: ${r.vacuous_reason}` : "")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <p className="trace-muted">No Sensor_Agent results.</p>
          )}
          {sensors.length ? <MassLegend /> : null}
          <Assessment label="Stage-1 Fused_Assessment" a={s.PERCEIVE?.stage1_assessment} />
        </>
      );
    }
    case "REMEMBER": {
      const dna = Object.entries(s.REMEMBER?.dna_comparison ?? {});
      const cp = s.REMEMBER?.change_point;
      return (
        <>
          {dna.length ? (
            <dl className="trace-dl">
              {dna.map(([sensor, feats]) => (
                <div key={sensor} className="trace-dl__row">
                  <dt>{sensor}</dt>
                  <dd>
                    {Object.entries(feats)
                      .map(([k, v]) => `${k} = ${feature(v)}`)
                      .join(", ")}
                  </dd>
                </div>
              ))}
            </dl>
          ) : (
            <p className="trace-muted">No Behavioural_DNA comparison recorded.</p>
          )}
          <p>{cp ? `Change point: ${cp.kind} (short-run mass ${n4(cp.short_run_mass)})` : "No change point at this event."}</p>
        </>
      );
    }
    case "REASON": {
      const hits = s.REASON?.motifs ?? [];
      const truncated = s.REASON?.truncated_motif_types ?? [];
      return (
        <>
          {hits.length ? (
            <ul className="trace-list">
              {hits.map((h) => (
                <li key={h.contributor_id}>
                  <span>
                    {h.contributor_id}: m(FRAUD) {n4(h.mass.fraud)}, weight {n4(h.weight)}
                  </span>
                  <MassBar m={h.mass} />
                </li>
              ))}
            </ul>
          ) : (
            <p className="trace-muted">No Motif hits.</p>
          )}
          {truncated.length ? <p>Motif search truncated for: {truncated.join(", ")}</p> : null}
          <Assessment label="Stage-2 Fused_Assessment" a={s.REASON?.stage2_assessment} />
        </>
      );
    }
    case "ACT": {
      const act = s.ACT ?? {};
      return (
        <dl className="trace-dl">
          <dt>Risk_Score</dt>
          <dd>{n4(act.risk_score)}</dd>
          <dt>Severity_Tier</dt>
          <dd>{act.severity_tier ? <TierBadge tier={act.severity_tier} /> : "not available"}</dd>
          <dt>Case</dt>
          <dd>
            {act.case_id ? (
              <button type="button" className="btn btn--small" onClick={() => openCase(act.case_id!)}>
                Open {act.case_id} in the Live view
              </button>
            ) : (
              "No Case"
            )}
          </dd>
        </dl>
      );
    }
    case "LEARN": {
      const l = s.LEARN ?? {};
      return (
        <>
          <p>{l.label ? `Feedback_Label: ${l.label}` : "No Feedback_Label recorded yet."}</p>
          {l.note ? <p className="trace-muted">{l.note}</p> : null}
        </>
      );
    }
  }
}

function describeError(err: unknown, txId: string): { text: string; disabled: boolean } {
  if (err instanceof ApiError && err.status === 404) {
    // FastAPI's own 404 ("Not Found") means the route is absent on this server (Req 38.10).
    if (err.detail === "Not Found") return { text: "The transaction trace feature is not available on this server.", disabled: true };
    return { text: `Transaction ${txId} not found: ${err.detail}`, disabled: false };
  }
  return { text: err instanceof ApiError ? err.describe() : String(err), disabled: false };
}

/** Transaction trace view (Req 38.4-38.6): pick a recent Transaction_Event or type its id and
 *  see each pipeline stage, in order, from the API_Server's trace endpoint. */
export function TransactionTrace() {
  const inputId = useId();
  const recentId = useId();
  const [txId, setTxId] = useState("");
  const [trace, setTrace] = useState<TraceDoc | null>(null);
  const [error, setError] = useState<{ text: string; disabled: boolean } | null>(null);
  const [loading, setLoading] = useState(false);
  const ctrl = useRef<AbortController | null>(null);
  const recent = useDashboard((s) => s.feed).slice(0, 8);
  const selectCase = useDashboard((s) => s.selectCase);
  const { select } = useShellTab();

  useEffect(() => () => ctrl.current?.abort(), []);

  const load = async (raw: string) => {
    const id = raw.trim();
    if (!id) {
      setError({ text: "Enter a transaction ID.", disabled: false });
      return;
    }
    ctrl.current?.abort();
    const c = new AbortController();
    ctrl.current = c;
    setLoading(true);
    setError(null);
    try {
      const doc = await getTrace(id, c.signal);
      if (!c.signal.aborted) setTrace(doc);
    } catch (err) {
      if (err instanceof ApiError && err.kind === "aborted") return;
      setTrace(null);
      setError(describeError(err, id));
    } finally {
      if (ctrl.current === c) setLoading(false);
    }
  };

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    void load(txId);
  };

  const pick = (id: string) => {
    setTxId(id);
    void load(id);
  };

  const openCase = (id: string) => {
    selectCase(id);
    select("live");
  };

  return (
    <div className="hx-view trace-view" role="region" aria-labelledby="trace-title" aria-busy={loading || undefined}>
      <ViewHeader
        id="trace-title"
        eyebrow="Explainability"
        title="Transaction trace"
        sub="Follow one payment through the six stages of HELIX and see exactly why it was scored the way it was."
      />
      <div className="trace">
        <div className="trace-finder">
          <form className="trace-search" onSubmit={onSubmit} role="search" aria-label="Trace a transaction">
            <label htmlFor={inputId} className="trace-search__label">
              Transaction ID
            </label>
            <div className="trace-search__box">
              <svg className="trace-search__icon" viewBox="0 0 20 20" aria-hidden="true" focusable="false">
                <circle cx="8.5" cy="8.5" r="5.5" />
                <path d="M13 13l4 4" />
              </svg>
              <input
                id={inputId}
                type="text"
                value={txId}
                onChange={(e) => setTxId(e.target.value)}
                placeholder="e.g. T010"
                autoComplete="off"
                spellCheck={false}
              />
              <button type="submit" className="btn btn--primary" disabled={loading}>
                {loading ? "Tracing…" : "Trace"}
              </button>
            </div>
          </form>

          {recent.length ? (
            <div className="trace-recent">
              <h3 id={recentId} className="trace-recent__title">
                Recent transactions
              </h3>
              <ul className="trace-recent__list" aria-labelledby={recentId}>
                {recent.map((f) => (
                  <li key={f.key}>
                    <button
                      type="button"
                      className="trace-chip"
                      aria-pressed={trace?.tx_id === f.tx_id}
                      onClick={() => pick(f.tx_id)}
                    >
                      {f.tx_id}
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          ) : (
            <p className="trace-recent__empty">Recent transactions appear here as the live feed receives them.</p>
          )}
        </div>

        {error ? (
          <p role="alert" className="msg msg--error" data-testid={error.disabled ? "trace-disabled" : "trace-error"}>
            {error.text}
          </p>
        ) : null}

        {trace ? (
          <article className="trace-result" aria-label={`Trace of ${trace.tx_id}`}>
            <div className="trace-result__head">
              <h3 className="trace-result__title">Transaction {trace.tx_id}</h3>
              {trace.event ? (
                <p className="trace-result__meta">
                  {trace.event.src} → {trace.event.dst}, {trace.event.amount} {trace.event.currency}
                  {trace.timestamp ? `, ${trace.timestamp}` : ""}
                </p>
              ) : null}
              {trace.source === "storage" ? (
                <p className="trace-muted">Rebuilt from Storage: per-sensor features are no longer held in memory.</p>
              ) : null}
            </div>
            <ol className="trace-stages">
              {STAGE_ORDER.map((name, i) => (
                <li key={name} className="trace-step" data-testid={`trace-stage-${name}`}>
                  <span className="trace-step__node" aria-hidden="true">
                    {i + 1}
                  </span>
                  <section className="trace-stage" aria-labelledby={`${inputId}-${name}`}>
                    <h4 id={`${inputId}-${name}`} className="trace-stage__title">
                      {name}
                    </h4>
                    <p className="trace-stage__hint">{STAGE_HINT[name]}</p>
                    <StageBody name={name} trace={trace} openCase={openCase} />
                  </section>
                </li>
              ))}
            </ol>
          </article>
        ) : null}
      </div>
    </div>
  );
}

import { useEffect, useState } from "react";
import { api, ApiError } from "../../api/client";
import type { DatasetDoc, DatasetPatternTypeDoc, DatasetStatus } from "../../api/types";
import { formatScore } from "../../format";

/** Poll period of GET /dataset. */
export const DATASET_POLL_MS = 5_000;
/** Re-check period when the server has no /dataset route (404). */
export const DATASET_ABSENT_RECHECK_MS = 60_000;

export const DATASET_DISABLED_TEXT = "Dataset stream off (synthetic demo). Start with -Dataset amlworld to stream IBM AMLworld.";
export const UNMAPPED_PRECISION = "unavailable (no motif)";

const STATUS_TEXT: Record<DatasetStatus, string> = {
  DISABLED: "Disabled",
  LOADING: "Loading files",
  WARMING_UP: "Warming up (no cases created)",
  STREAMING: "Streaming",
  DONE: "Done",
  FAILED: "Failed",
};

type Load =
  | { kind: "loading" }
  | { kind: "absent" }
  | { kind: "error"; message: string }
  | { kind: "ok"; doc: DatasetDoc; error: string | null };

/** Whole-number percentage of a 0-1 fraction, or null. */
export function progressPercent(p: number | null | undefined): number | null {
  if (typeof p !== "number" || !Number.isFinite(p)) return null;
  return Math.floor(Math.min(1, Math.max(0, p)) * 100);
}

/** Precision cell text: the value, "unavailable (no motif)" for unmapped types, or n/a. */
export function precisionText(t: DatasetPatternTypeDoc): string {
  if (t.precision !== null) return formatScore(t.precision, 3);
  if (t.precision_reason === "unmapped" || !t.mapped) return UNMAPPED_PRECISION;
  if (t.precision_reason === "no_cases") return "n/a (no cases yet)";
  return "n/a";
}

const int = (n: number | null | undefined) => (typeof n === "number" ? n.toLocaleString("en-GB") : "n/a");

/** Poll GET /dataset every 5 s. DISABLED and FAILED are final (no more polls); 404 means the
 *  server predates the route and is asked again only every minute. */
function useDataset(pollMs: number): Load {
  const [load, setLoad] = useState<Load>({ kind: "loading" });
  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let ctrl: AbortController | null = null;
    const tick = async () => {
      ctrl = new AbortController();
      let next: number | null = pollMs;
      try {
        const doc = await api.getDataset(ctrl.signal);
        if (!alive) return;
        setLoad({ kind: "ok", doc, error: null });
        if (doc.status === "DISABLED" || doc.status === "FAILED") next = null;
      } catch (err) {
        if (!alive || (err instanceof ApiError && err.kind === "aborted")) return;
        if (err instanceof ApiError && err.kind === "http" && err.status === 404) {
          setLoad({ kind: "absent" });
          next = DATASET_ABSENT_RECHECK_MS;
        } else {
          const message = err instanceof ApiError ? err.describe() : String(err);
          setLoad((prev) => (prev.kind === "ok" ? { ...prev, error: message } : { kind: "error", message }));
        }
      }
      if (alive && next !== null) timer = setTimeout(() => void tick(), next);
    };
    void tick();
    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
      ctrl?.abort();
    };
  }, [pollMs]);
  return load;
}

function Body({ doc }: { doc: DatasetDoc }) {
  const pct = progressPercent(doc.progress);
  const reasons = Object.entries(doc.skip_reasons ?? {});
  const w = doc.warmup;
  const ev = doc.evaluation;
  const badge = doc.status === "FAILED" ? " badge--warn" : doc.status === "DONE" ? " badge--ok" : "";
  return (
    <>
      <p data-testid="dataset-status">
        Status <span className={`badge${badge}`}>{STATUS_TEXT[doc.status] ?? doc.status}</span>
        {doc.kind ? <span className="muted small"> · {doc.kind}</span> : null}
        {doc.files?.trans ? <span className="muted small mono"> · {doc.files.trans}</span> : null}
      </p>
      {doc.error ? (
        <p className="msg msg--error small" data-testid="dataset-error">
          {doc.error}
        </p>
      ) : null}
      <div className="dataset__progress">
        <progress max={1} value={pct === null ? undefined : (doc.progress ?? 0)} aria-label="Dataset submission progress" />
        <span data-testid="dataset-progress">
          {pct === null ? "Progress n/a" : `${pct}%`} ({int(doc.submitted)} of {int(doc.in_scope)} in-scope events submitted)
        </span>
      </div>
      <p className="small" data-testid="dataset-rows">
        Rows read {int(doc.rows_read)} · skipped {int(doc.skipped)}
        {reasons.length ? `: ${reasons.map(([r, n]) => `${r} ${int(n)}`).join(", ")}` : ""}
      </p>
      {w ? (
        <p className="small" data-testid="dataset-warmup">
          {w.done
            ? `Warm-up done: ${int(w.events)} events in ${formatScore(w.duration_s, 1)} s (${formatScore(w.events_per_s, 1)} events/s); ` +
              `${int(w.accounts_ready)} of ${int(w.accounts)} accounts reached ${int(w.min_history)} events of history.`
            : `Warm-up in progress${w.until ? ` (rows before ${w.until})` : " (all rows)"}: cases are not created yet.`}
        </p>
      ) : null}
      {ev ? (
        <>
          <p className="small" data-testid="dataset-eval">
            Precision {formatScore(ev.precision, 3)} · recall {formatScore(ev.recall, 3)} · captured fraud fraction{" "}
            {formatScore(ev.captured_fraud_fraction, 3)}
          </p>
          {ev.pattern_types.length ? (
            <div className="scroll scroll--short">
              <table className="table table--compact" data-testid="dataset-patterns">
                <caption className="sr-only">Detection per AMLworld pattern type</caption>
                <thead>
                  <tr>
                    <th scope="col">Pattern type</th>
                    <th scope="col">Motif</th>
                    <th scope="col" className="num">
                      Patterns
                    </th>
                    <th scope="col" className="num">
                      Detected
                    </th>
                    <th scope="col" className="num">
                      Recall
                    </th>
                    <th scope="col" className="num">
                      Precision
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {ev.pattern_types.map((t) => (
                    <tr key={t.pattern_type} data-testid={`pattern-${t.pattern_type}`}>
                      <th scope="row">{t.pattern_type}</th>
                      <td>{t.motif ?? "none"}</td>
                      <td className="num">{int(t.patterns)}</td>
                      <td className="num">
                        {int(t.detected_patterns)}
                        {t.detection_rate !== null ? ` (${Math.round(t.detection_rate * 100)}%)` : ""}
                      </td>
                      <td className="num">{formatScore(t.recall, 3)}</td>
                      <td className="num">{precisionText(t)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="muted small">No pattern file loaded: no per-pattern results.</p>
          )}
        </>
      ) : null}
    </>
  );
}

/** Dataset_Feeder status (Req 39.5, 39.6, 40.8), shown inside the HELIX loop panel. */
export function DatasetPanel({ pollMs = DATASET_POLL_MS }: { pollMs?: number }) {
  const load = useDataset(pollMs);
  // Off is the normal synthetic-demo state: one compact muted line instead of a whole block.
  if (load.kind === "ok" && load.doc.status === "DISABLED") {
    return (
      <div className="dataset dataset--off" data-testid="dataset">
        <p className="muted small" data-testid="dataset-disabled">
          {DATASET_DISABLED_TEXT}
        </p>
      </div>
    );
  }
  return (
    <div className="dataset" data-testid="dataset">
      <h3 className="ev-section__title">Dataset stream</h3>
      {load.kind === "loading" ? (
        <p className="muted small">Checking dataset status…</p>
      ) : load.kind === "absent" ? (
        <p className="muted small" data-testid="dataset-absent">
          Dataset status is not available on this server (no GET /dataset).
        </p>
      ) : load.kind === "error" ? (
        <p className="msg msg--error small" role="alert">
          GET /dataset failed: {load.message}
        </p>
      ) : (
        <>
          {load.error ? (
            <p className="msg msg--error small" role="alert">
              GET /dataset failed: {load.error} Showing the last received values.
            </p>
          ) : null}
          <Body doc={load.doc} />
        </>
      )}
    </div>
  );
}

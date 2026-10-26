import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "../../api/client";
import { independentGroups, prepareScenario, runTag, SAMPLE_JSONL, scenarioCases, type PreparedLine } from "../../demo/scenario";
import { formatScore } from "../../format";
import { dashboard, useDashboard } from "../../store/store";
import { TierBadge } from "../common/TierBadge";

export interface LineResult {
  line: number;
  txId: string | null;
  outcome: "accepted" | "duplicate" | "rejected" | "error";
  detail: string;
}

type Run =
  | { status: "idle" }
  | { status: "sending"; tag: string; done: number; total: number }
  | { status: "done"; tag: string; results: LineResult[] };

function describe(err: unknown): string {
  return err instanceof ApiError ? err.describe() : err instanceof Error ? err.message : String(err);
}

/** Submit every prepared line in file order, one request at a time (order matters for the
 *  per-account history). Rejections are recorded and the run continues. */
export async function submitScenario(
  text: string,
  nowMs: number,
  tag: string,
  onProgress: (done: number, total: number) => void,
  ingest: (json: string) => ReturnType<typeof api.ingest> = api.ingest,
): Promise<LineResult[]> {
  const lines = prepareScenario(text, nowMs, tag);
  const results: LineResult[] = [];
  const send = async (l: PreparedLine) => {
    try {
      const r = await ingest(l.body);
      results.push({ line: l.line, txId: l.txId, outcome: r.duplicate ? "duplicate" : "accepted", detail: r.status });
    } catch (err) {
      const rejected = err instanceof ApiError && err.kind === "http" && err.status === 422;
      const detail = rejected && err.errors.length ? err.errors.map((e) => `${e.field}: ${e.reason}`).join("; ") : describe(err);
      results.push({ line: l.line, txId: l.txId, outcome: rejected ? "rejected" : "error", detail });
    }
    onProgress(results.length, lines.length);
  };
  // Stories with disjoint accounts go in parallel (a busy server answers each request
  // slowly); within a story, lines go one at a time in file order.
  await Promise.all(
    independentGroups(lines).map(async (group) => {
      for (const l of group) await send(l);
    }),
  );
  return results.sort((a, b) => a.line - b.line);
}

function count(results: LineResult[], o: LineResult["outcome"]) {
  return results.filter((r) => r.outcome === o).length;
}

/** State and actions of one "Load demo scenario" control. The Live dashboard holds one and
 *  shares it between the button in its KPI strip and the result line under it. */
export function useDemoScenario() {
  const [run, setRun] = useState<Run>({ status: "idle" });
  const cases = useDashboard((s) => s.cases);
  const selectCase = useDashboard((s) => s.selectCase);
  const tag = run.status === "idle" ? null : run.tag;
  const found = tag ? scenarioCases(cases, tag) : [];
  const autoSelected = useRef<string | null>(null);

  // Show the first scenario Case as soon as the backend opens it, unless the user has
  // already picked a Case.
  const first = found[0]?.caseId ?? null;
  useEffect(() => {
    if (!tag || !first || autoSelected.current === tag) return;
    autoSelected.current = tag;
    if (!dashboard.getState().selectedCaseId) selectCase(first);
  }, [tag, first, selectCase]);

  const load = async () => {
    const now = Date.now();
    const t = runTag(now);
    setRun({ status: "sending", tag: t, done: 0, total: 0 });
    const results = await submitScenario(SAMPLE_JSONL, now, t, (done, total) => setRun({ status: "sending", tag: t, done, total }));
    setRun({ status: "done", tag: t, results });
    // Cases arrive on /ws/cases; a REST read also covers a missed or dropped message. It only
    // adds/updates Cases (newer wins), never removes any.
    setTimeout(() => {
      api.allCases().then(
        (r) => r.items.forEach((c) => dashboard.getState().upsertCase(c)),
        () => undefined,
      );
    }, 1_500);
  };

  const dismiss = () => setRun((r) => (r.status === "sending" ? r : { status: "idle" }));

  return { run, found, load, dismiss, selectCase };
}

export type DemoScenarioState = ReturnType<typeof useDemoScenario>;

/** What the demo scenario sends, for the Controls panel and the Controls drawer. */
export function DemoScenarioDescription() {
  return (
    <p className="muted small">
      Sends the 15 lines of docs/sample_transactions.jsonl to POST /api/v1/events: Alice&apos;s Berlin → Lagos takeover (T007), Bob&apos;s
      structuring (T010–T012) and a mule chain (T013–T015), plus one duplicate and one invalid line. Timestamps are shifted so the newest
      is now and ids get a run suffix, so it can be loaded repeatedly.
    </p>
  );
}

/** The "Load demo scenario" button. Its accessible name is stable while idle (e2e uses it). */
export function DemoScenarioButton({ demo, className = "btn btn--primary" }: { demo: DemoScenarioState; className?: string }) {
  const sending = demo.run.status === "sending";
  return (
    <button type="button" className={className} disabled={sending} onClick={() => void demo.load()}>
      {sending ? "Sending…" : "Load demo scenario"}
    </button>
  );
}

/** Progress, the run's outcome counts (data-testid="demo-result"), any rejected lines and the
 *  Cases the backend opened for the run, each with a "Show C-n" button. */
export function DemoScenarioResult({ demo, onDismiss }: { demo: DemoScenarioState; onDismiss?: () => void }) {
  const { run, found, selectCase } = demo;
  if (run.status === "sending") {
    return (
      <p role="status" className="msg muted">
        Submitting line {run.done} of {run.total || 15}…
      </p>
    );
  }
  if (run.status !== "done") return null;
  const failed = run.results.filter((r) => r.outcome === "rejected" || r.outcome === "error");
  return (
    <>
      <div className="demo-result__head">
        <p role="status" className="msg msg--ok" data-testid="demo-result">
          Run {run.tag.slice(1)}: {count(run.results, "accepted")} accepted, {count(run.results, "duplicate")} duplicate,{" "}
          {count(run.results, "rejected")} rejected by validation
          {count(run.results, "error") ? `, ${count(run.results, "error")} failed` : ""}.
        </p>
        {onDismiss ? (
          <button type="button" className="btn btn--small demo-result__dismiss" aria-label="Dismiss demo result" onClick={onDismiss}>
            <span aria-hidden="true">×</span>
          </button>
        ) : null}
      </div>
      {failed.length ? (
        <details className="ev-details demo-result__lines">
          <summary>
            {failed.length} {failed.length === 1 ? "line was" : "lines were"} not accepted
          </summary>
          {failed.map((r) => (
            <p key={r.line} className="muted small">
              Line {r.line} ({r.txId ?? "not JSON"}): {r.detail}
            </p>
          ))}
        </details>
      ) : null}
      {found.length === 0 ? (
        <p className="muted small">Waiting for the backend to open Cases for this run…</p>
      ) : (
        <ul className="demo-cases" aria-label="Cases opened for the demo scenario">
          {found.map((c) => (
            <li key={c.caseId}>
              <button type="button" className="btn btn--small" onClick={() => selectCase(c.caseId)}>
                Show {c.caseId}
              </button>{" "}
              <TierBadge tier={c.summary.tier} /> risk {formatScore(c.summary.risk)}
              {c.summary.placement === "BACKLOG" ? <span className="muted small"> (backlog: over the alert budget)</span> : null}
              <div className="muted small demo-cases__title" title={c.title}>
                {c.title}
              </div>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}

/** "Load demo scenario": sends docs/sample_transactions.jsonl through the real ingest
 *  endpoint and lists the Cases the backend opens for it (see demo/scenario.ts). */
export function DemoScenario() {
  const demo = useDemoScenario();
  return (
    <fieldset className="group" data-testid="demo-scenario">
      <legend>Demo scenario</legend>
      <DemoScenarioDescription />
      <div className="buttons">
        <DemoScenarioButton demo={demo} />
      </div>
      <DemoScenarioResult demo={demo} />
    </fieldset>
  );
}

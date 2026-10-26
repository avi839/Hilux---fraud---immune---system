import { useId, useState, type FormEvent } from "react";
import { ApiError } from "../../api/client";
import { describeSocError, socApi, type RuleProblem, type RuleSummary, type SubmittedRule } from "../../api/soc";
import { Message, SocSection, formatDuration, useInvestigator, useReportFigure } from "./common";
import { useAction, useResource } from "./useResource";

export const EXAMPLE_RULE = `id: HXR-DEMO-1
title: Dormant account moves a large amount
description: An account with history, quiet for 30+ days, suddenly sends 1000 or more.
status: experimental
target: transaction
techniques: [HX-T0014]
detection:
  dormant:
    dna.count|gte: 5
    dna.seconds_since_last|gte: 2592000
  large:
    tx.amount|gte: 1000
  condition: dormant and large
mass:
  fraud: 0.4
  legit: 0.0
`;

type Submit =
  | { kind: "ok"; rule: SubmittedRule }
  | { kind: "invalid"; detail: string; problems: RuleProblem[] }
  | { kind: "error"; text: string }
  | null;

function problemsOf(err: ApiError): RuleProblem[] {
  return (err.errors as unknown as Array<Partial<RuleProblem>>).map((p) => ({
    line: typeof p.line === "number" ? p.line : null,
    field: String(p.field ?? "document"),
    reason: String(p.reason ?? ""),
  }));
}

function RuleEditor({ onStored }: { onStored: () => void }) {
  const [yaml, setYaml] = useState(EXAMPLE_RULE);
  const [pending, setPending] = useState(false);
  const [result, setResult] = useState<Submit>(null);
  const areaId = useId();
  const hintId = useId();

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setPending(true);
    setResult(null);
    try {
      const rule = await socApi.submitRule(yaml);
      setResult({ kind: "ok", rule });
      onStored();
    } catch (err) {
      if (err instanceof ApiError && err.kind === "http" && err.status === 422 && err.errors.length) {
        setResult({ kind: "invalid", detail: err.detail, problems: problemsOf(err) });
      } else {
        setResult({ kind: "error", text: describeSocError(err) });
      }
    } finally {
      setPending(false);
    }
  };

  return (
    <form className="soc-sub" onSubmit={submit} noValidate>
      <div className="field">
        <label htmlFor={areaId}>Detection rule (YAML)</label>
        <textarea
          id={areaId}
          className="code"
          rows={14}
          spellCheck={false}
          value={yaml}
          aria-describedby={hintId}
          aria-invalid={result?.kind === "invalid" ? true : undefined}
          onChange={(e) => setYaml(e.target.value)}
        />
        <p id={hintId} className="muted small">
          Submitting stores a new version and backtests it on recent stored events. Activate it below once you are happy with the backtest.
        </p>
      </div>
      <div className="buttons">
        <button type="submit" className="btn btn--primary" disabled={pending}>
          Submit and backtest
        </button>
        <button type="button" className="btn" onClick={() => setYaml(EXAMPLE_RULE)}>
          Reset to example
        </button>
      </div>
      {pending ? <p className="msg muted">Validating and backtesting…</p> : null}
      {result?.kind === "ok" ? (
        <p role="status" className="msg msg--ok" data-testid="rule-backtest">
          Stored {result.rule.rule_id} version {result.rule.version}. Backtest:{" "}
          {result.rule.backtest
            ? `${result.rule.backtest.hits} hits, ${result.rule.backtest.estimated_false_positives} estimated false positives` +
              (result.rule.backtest.window_s ? ` over ${formatDuration(result.rule.backtest.window_s)}` : "")
            : "not run"}
          .
        </p>
      ) : null}
      {result?.kind === "invalid" ? (
        <div role="alert" className="msg msg--error" data-testid="rule-errors">
          <p className="msg__text">Rule rejected ({result.detail}); nothing was changed.</p>
          <ul className="ev-list">
            {result.problems.map((p, i) => (
              <li key={i}>
                {p.line !== null ? `Line ${p.line}` : "Document"}: {p.field} - {p.reason}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {result?.kind === "error" ? (
        <p role="alert" className="msg msg--error">
          {result.text}
        </p>
      ) : null}
    </form>
  );
}

function RuleRow({ rule, onChanged }: { rule: RuleSummary; onChanged: () => void }) {
  const action = useAction();
  const who = useInvestigator();
  const latest = rule.versions.find((v) => v.version === rule.latest_version);
  const backtested = latest?.backtested;
  const activate = () => {
    const w = who();
    if (!w.ok) return action.setMsg({ kind: "error", text: w.message });
    void action.run(async () => {
      const r = await socApi.activateRule(rule.rule_id, w.value, rule.latest_version);
      onChanged();
      return `${r.rule_id} version ${r.active_version} is active.`;
    });
  };
  const deactivate = () =>
    void action.run(async () => {
      const r = await socApi.deactivateRule(rule.rule_id);
      onChanged();
      return `${r.rule_id} deactivated.`;
    });
  const backtest = () =>
    void action.run(async () => {
      const b = await socApi.backtestRule(rule.rule_id, rule.latest_version);
      onChanged();
      return (
        `Backtest of ${rule.rule_id} v${rule.latest_version}: ${b.hits} hits, ${b.estimated_false_positives} estimated false positives` +
        (b.window_s ? ` over ${formatDuration(b.window_s)}.` : ".")
      );
    });
  return (
    <tr data-testid={`rule-${rule.rule_id}`}>
      <th scope="row" className="loop-stage">
        <span className="mono">{rule.rule_id}</span> {rule.title}
        <div className="muted small">
          {rule.target} · {rule.status} · {rule.techniques.join(", ")} · mass on fraud {rule.mass.fraud}
        </div>
      </th>
      <td className="num">{rule.latest_version}</td>
      <td>
        {rule.active_version !== null ? `active v${rule.active_version}` : "inactive"}
        {backtested === false ? <div className="muted small">latest not backtested</div> : null}
      </td>
      <td>
        <div className="buttons">
          <button
            type="button"
            className="btn btn--small"
            disabled={action.pending}
            onClick={backtest}
            aria-label={`Backtest ${rule.rule_id} version ${rule.latest_version}`}
          >
            Backtest
          </button>
          <button
            type="button"
            className="btn btn--small"
            disabled={action.pending || rule.active_version === rule.latest_version}
            onClick={activate}
            aria-label={`Activate ${rule.rule_id} version ${rule.latest_version}`}
          >
            Activate v{rule.latest_version}
          </button>
          <button
            type="button"
            className="btn btn--small"
            disabled={action.pending || rule.active_version === null}
            onClick={deactivate}
            aria-label={`Deactivate ${rule.rule_id}`}
          >
            Deactivate
          </button>
        </div>
        <Message msg={action.msg} />
      </td>
    </tr>
  );
}

/** Detection-as-code (Req 31): list rules with activate / deactivate, and a YAML editor that
 *  submits, validates (line-numbered errors) and backtests a new version. */
export function RulesSection() {
  const r = useResource((sig) => socApi.rules(sig), "rules");
  useReportFigure("rules", r.data);
  return (
    <SocSection
      title="Detection rules"
      area="soc-rules"
      explain="Detections written as small YAML files. Each new version is tested against recent history before it can be switched on."
      resource={r}
      footer={<RuleEditor onStored={r.reload} />}
    >
      {(doc) =>
        doc.rules.length === 0 ? (
          <p className="empty">No detection rules stored yet. Submit the example below.</p>
        ) : (
          <>
            <table className="table table--compact soc-table">
              <caption className="sr-only">Stored detection rules</caption>
              <thead>
                <tr>
                  <th scope="col">Rule</th>
                  <th scope="col" className="num">
                    Latest
                  </th>
                  <th scope="col">State</th>
                  <th scope="col">Actions</th>
                </tr>
              </thead>
              <tbody>
                {doc.rules.map((rule) => (
                  <RuleRow key={rule.rule_id} rule={rule} onChanged={r.reload} />
                ))}
              </tbody>
            </table>
            <p className="muted small">
              Backtest window {formatDuration(doc.backtest_window_s)} · evaluation errors {doc.eval_errors}
            </p>
          </>
        )
      }
    </SocSection>
  );
}

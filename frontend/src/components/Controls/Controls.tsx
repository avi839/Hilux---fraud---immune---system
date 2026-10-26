import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { api, ApiError } from "../../api/client";
import { ALERT_BUDGET, INVESTIGATION_COST, type FeedbackLabel } from "../../api/types";
import { useDashboard } from "../../store/store";
import { Panel } from "../common/Panel";
import { TierBadge } from "../common/TierBadge";
import { registerCaseActions, shortcutKey, type CaseActionSource } from "./caseActions";
import { DemoScenario } from "./DemoScenario";
import { checkAlertBudget, checkInvestigationCost, checkInvestigator } from "./validation";

type Msg = { kind: "ok" | "error"; text: string } | null;

function describe(err: unknown): string {
  return err instanceof ApiError ? err.describe() : err instanceof Error ? err.message : String(err);
}

/** One control action: pending flag plus the last confirmation or error. The store is only
 *  updated from server responses, so a failure leaves the last confirmed state on screen. */
function useAction() {
  const [pending, setPending] = useState(false);
  const [msg, setMsg] = useState<Msg>(null);
  const run = async (fn: () => Promise<string>) => {
    setPending(true);
    setMsg(null);
    try {
      setMsg({ kind: "ok", text: await fn() });
    } catch (err) {
      setMsg({ kind: "error", text: describe(err) });
    } finally {
      setPending(false);
    }
  };
  return { pending, msg, setMsg, run };
}

function Message({ msg, id }: { msg: Msg; id?: string }) {
  if (!msg) return null;
  return msg.kind === "error" ? (
    <p id={id} role="alert" className="msg msg--error">
      {msg.text}
    </p>
  ) : (
    <p id={id} role="status" className="msg msg--ok">
      {msg.text}
    </p>
  );
}

/** Confirm fraud / False positive / Escalate for the selected Case, with the investigator
 *  name. Mount exactly one instance: it registers the handlers the F/P/E shortcuts run.
 *  ``layout="inline"`` is the Case workspace's compact bar (the selection is shown by the
 *  workspace header, so the selection hint stays for screen readers only). */
export function CaseActions({ layout = "panel" }: { layout?: "panel" | "inline" }) {
  const inline = layout === "inline";
  const selectedId = useDashboard((s) => s.selectedCaseId);
  const selected = useDashboard((s) => (s.selectedCaseId ? s.cases[s.selectedCaseId] : undefined));
  const upsertCase = useDashboard((s) => s.upsertCase);
  const investigator = useDashboard((s) => s.investigator);
  const setInvestigator = useDashboard((s) => s.setInvestigator);
  const action = useAction();
  const nameId = useId();
  const hintId = useId();
  const closed = selected?.status === "CLOSED";
  const disabled = !selectedId || closed || action.pending;

  const withName = (fn: (id: string, name: string) => Promise<string>) => () => {
    if (!selectedId) return;
    const who = checkInvestigator(investigator);
    if (!who.ok) {
      action.setMsg({ kind: "error", text: who.message });
      return;
    }
    void action.run(() => fn(selectedId, who.value));
  };

  const label = (lab: FeedbackLabel) =>
    withName(async (id, name) => {
      // Labels need the Case IN_REVIEW with this investigator as assignee; open is idempotent
      // for the current assignee, so it is always sent first.
      upsertCase(await api.openCase(id, name));
      const d = await api.feedback(id, name, lab);
      upsertCase(d);
      return `Confirmed: ${d.case_id} is ${d.status} with label ${d.label ?? lab}.`;
    });

  const escalate = withName(async (id, name) => {
    const d = await api.escalate(id, name);
    upsertCase(d);
    return `Confirmed: ${d.case_id} escalated to ${d.tier}.`;
  });

  const onFraud = label("CONFIRMED_FRAUD");
  const onFalsePositive = label("FALSE_POSITIVE");
  // The EvidencePanel shortcuts (F/P/E) call these same handlers through the registry; the
  // ref always holds this render's handlers and disabled state.
  const current: ReturnType<CaseActionSource> = {
    CONFIRMED_FRAUD: { run: onFraud, disabled },
    FALSE_POSITIVE: { run: onFalsePositive, disabled },
    ESCALATE: { run: escalate, disabled },
  };
  const handles = useRef(current);
  handles.current = current;
  useEffect(() => registerCaseActions(() => handles.current), []);

  const buttons = [
    { kind: "CONFIRMED_FRAUD" as const, label: "Confirm fraud", className: "btn btn--danger", onClick: onFraud },
    { kind: "FALSE_POSITIVE" as const, label: "False positive", className: "btn", onClick: onFalsePositive },
    { kind: "ESCALATE" as const, label: "Escalate", className: "btn btn--warn", onClick: escalate },
  ];

  return (
    <fieldset className={inline ? "group case-actions case-actions--inline" : "group case-actions"} data-testid="case-actions">
      <legend className={inline ? "sr-only" : undefined}>Case actions</legend>
      <p className={inline ? "sr-only" : "selected"} id={hintId}>
        {selected ? (
          <>
            Selected <span className="mono">{selected.case_id}</span> <TierBadge tier={selected.tier} />{" "}
            <span className="muted">
              {selected.status}
              {selected.assignee ? ` · ${selected.assignee}` : ""}
            </span>
          </>
        ) : selectedId ? (
          <>Selected {selectedId}</>
        ) : (
          "Select a case in the queue to label or escalate it."
        )}
      </p>
      <div className={inline ? "case-actions__bar" : undefined}>
        <div className="field case-actions__name">
          <label htmlFor={nameId}>Investigator name</label>
          <input
            id={nameId}
            type="text"
            autoComplete="username"
            maxLength={64}
            value={investigator}
            onChange={(e) => setInvestigator(e.target.value)}
          />
        </div>
        <div className="buttons case-actions__buttons">
          {buttons.map((b) => (
            <button
              key={b.kind}
              type="button"
              className={b.className}
              disabled={disabled}
              aria-describedby={hintId}
              aria-keyshortcuts={shortcutKey(b.kind)}
              onClick={b.onClick}
            >
              {b.label}
              {/* The key is exposed through aria-keyshortcuts; hidden here so the button's
                  accessible name stays exactly its label. */}
              {inline ? (
                <kbd className="case-actions__key" aria-hidden="true">
                  {shortcutKey(b.kind)}
                </kbd>
              ) : null}
            </button>
          ))}
        </div>
      </div>
      {/* Inline, the key chips on the buttons and the Evidence view's shortcut line show this
          visually; the sentence stays for screen readers. */}
      <p className={inline ? "sr-only" : "muted small"}>
        Shortcuts while focus is in the Evidence panel: F confirm fraud, P false positive, E escalate.
      </p>
      {action.pending ? <p className="msg muted">Waiting for confirmation…</p> : null}
      <Message msg={action.msg} />
    </fieldset>
  );
}

interface NumberSettingProps {
  label: string;
  path: typeof ALERT_BUDGET | typeof INVESTIGATION_COST;
  confirmed: number | null;
  step: string;
  check: (raw: string) => { ok: true; value: number } | { ok: false; message: string };
  send: (n: number) => ReturnType<typeof api.setAlertBudget>;
  confirmedKey: "alertBudget" | "investigationCost";
}

function NumberSetting({ label, path, confirmed, step, check, send, confirmedKey }: NumberSettingProps) {
  const setConfirmed = useDashboard((s) => s.setConfirmed);
  const [draft, setDraft] = useState("");
  const action = useAction();
  const inputId = useId();
  const confirmedId = useId();
  const msgId = useId();

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const c = check(draft);
    if (!c.ok) {
      // Rejected before any request; the confirmed value stays on screen (Req 22.14).
      action.setMsg({ kind: "error", text: c.message });
      return;
    }
    void action.run(async () => {
      const r = await send(c.value);
      const value = r.accepted[path] ?? c.value;
      setConfirmed({ [confirmedKey]: value, appliesFromPeriod: r.applies_from_period });
      setDraft("");
      return `Confirmed: ${label.toLowerCase()} ${value}, applies from budget period ${r.applies_from_period}.`;
    });
  };

  return (
    <form className="setting" onSubmit={submit} noValidate>
      <div className="field">
        <label htmlFor={inputId}>{label}</label>
        <div className="field__row">
          <input
            id={inputId}
            type="number"
            inputMode="decimal"
            step={step}
            value={draft}
            placeholder={confirmed !== null ? String(confirmed) : ""}
            aria-describedby={`${confirmedId} ${msgId}`}
            aria-invalid={action.msg?.kind === "error" ? true : undefined}
            onChange={(e) => setDraft(e.target.value)}
          />
          <button type="submit" className="btn" disabled={action.pending}>
            Apply
          </button>
        </div>
        <p id={confirmedId} className="confirmed">
          Confirmed: <strong data-testid={`confirmed-${confirmedKey}`}>{confirmed ?? "unknown"}</strong>
        </p>
      </div>
      {action.pending ? <p className="msg muted">Waiting for confirmation…</p> : null}
      <div id={msgId}>
        <Message msg={action.msg} />
      </div>
    </form>
  );
}

export function GeneratorControl() {
  const running = useDashboard((s) => s.confirmed.generatorRunning);
  const setConfirmed = useDashboard((s) => s.setConfirmed);
  const action = useAction();
  const toggle = (start: boolean) => () =>
    void action.run(async () => {
      const r = start ? await api.startGenerator() : await api.stopGenerator();
      setConfirmed({ generatorRunning: r.running });
      return `Confirmed: generator ${r.running ? "running" : "stopped"}${r.changed ? "" : " (no change)"}.`;
    });
  const state = running === null ? "unknown" : running ? "running" : "stopped";
  return (
    <fieldset className="group">
      <legend>Synthetic generator</legend>
      <p className="confirmed">
        Confirmed state: <strong data-testid="confirmed-generator">{state}</strong>
      </p>
      <div className="buttons">
        <button type="button" className="btn" disabled={action.pending} onClick={toggle(true)}>
          Start generator
        </button>
        <button type="button" className="btn" disabled={action.pending} onClick={toggle(false)}>
          Stop generator
        </button>
      </div>
      {action.pending ? <p className="msg muted">Waiting for confirmation…</p> : null}
      <Message msg={action.msg} />
    </fieldset>
  );
}

/** Prioritiser settings: alert budget and investigation cost, server-confirmed. */
export function PrioritiserControls() {
  const confirmed = useDashboard((s) => s.confirmed);
  return (
    <fieldset className="group">
      <legend>Prioritiser</legend>
      <NumberSetting
        label="Alert budget"
        path={ALERT_BUDGET}
        confirmed={confirmed.alertBudget}
        step="1"
        check={checkAlertBudget}
        send={api.setAlertBudget}
        confirmedKey="alertBudget"
      />
      <NumberSetting
        label="Investigation cost"
        path={INVESTIGATION_COST}
        confirmed={confirmed.investigationCost}
        step="any"
        check={checkInvestigationCost}
        send={api.setInvestigationCost}
        confirmedKey="investigationCost"
      />
      {confirmed.appliesFromPeriod !== null ? (
        <p className="muted small">Changes apply from budget period {confirmed.appliesFromPeriod}.</p>
      ) : null}
    </fieldset>
  );
}

/** Investigator and operator controls (Req 22.7, 22.8, 22.14, 22.15) as one panel. The Live
 *  dashboard composes the same parts differently (Case workspace + Controls drawer). */
export function Controls() {
  return (
    <Panel title="Controls" area="controls">
      <CaseActions />
      <PrioritiserControls />
      <GeneratorControl />
      <DemoScenario />
    </Panel>
  );
}

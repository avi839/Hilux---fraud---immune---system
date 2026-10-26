import { useEffect, useId, useRef, useState, type KeyboardEvent } from "react";
import { api, ApiError } from "../../api/client";
import { MAX_BULK_CASES, type BulkFailure, type FeedbackLabel } from "../../api/types";
import { formatScore } from "../../format";
import { useDashboard } from "../../store/store";
import { TierBadge } from "../common/TierBadge";
import { checkInvestigator } from "../Controls/validation";
import { percent } from "../EvidencePanel/SuggestedLabel";

const LABEL_TEXT: Record<FeedbackLabel, string> = { CONFIRMED_FRAUD: "Confirm fraud", FALSE_POSITIVE: "False positive" };

type Outcome =
  | { kind: "ok"; text: string }
  | { kind: "error"; text: string; failures: BulkFailure[] }
  | null;

/** Keep only selected ids that are still in the queue, in queue order. */
export function pruneSelection(selection: ReadonlySet<string>, queue: readonly string[]): string[] {
  return queue.filter((id) => selection.has(id));
}

/** "Select all visible": the first MAX_BULK_CASES of the queue (priority order). */
export function selectAllVisible(queue: readonly string[], max = MAX_BULK_CASES): string[] {
  return queue.slice(0, max);
}

/** Modal confirmation (role="alertdialog") with focus kept on Cancel / Confirm; Escape cancels. */
function ConfirmDialog({
  count,
  label,
  investigator,
  onConfirm,
  onCancel,
}: {
  count: number;
  label: FeedbackLabel;
  investigator: string;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const titleId = useId();
  const descId = useId();
  const cancelRef = useRef<HTMLButtonElement>(null);
  const confirmRef = useRef<HTMLButtonElement>(null);
  useEffect(() => cancelRef.current?.focus(), []);

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Escape") {
      e.preventDefault();
      onCancel();
    } else if (e.key === "Tab") {
      // Focus trap: two buttons, so Tab and Shift+Tab both move to the other one.
      e.preventDefault();
      (document.activeElement === cancelRef.current ? confirmRef : cancelRef).current?.focus();
    }
  };

  return (
    <div className="modal-backdrop">
      <div
        className="modal"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descId}
        onKeyDown={onKeyDown}
        data-testid="bulk-dialog"
      >
        <h3 id={titleId} className="modal__title">
          Label {count} {count === 1 ? "case" : "cases"} as {label}?
        </h3>
        <p id={descId}>
          As investigator <strong>{investigator}</strong>. The label is stored on every selected case, or on none if any case fails
          validation. Suggestions are not applied; this is your decision.
        </p>
        <div className="buttons">
          <button ref={cancelRef} type="button" className="btn" onClick={onCancel}>
            Cancel
          </button>
          <button ref={confirmRef} type="button" className={`btn ${label === "CONFIRMED_FRAUD" ? "btn--danger" : "btn--primary"}`} onClick={onConfirm}>
            Confirm
          </button>
        </div>
      </div>
    </div>
  );
}

/**
 * Bulk labelling (Req 41.8): checkboxes over the Active_Queue, select all visible (capped at
 * 50), Confirm fraud / False positive behind a confirmation dialog, and the result or every
 * per-Case failure announced in a polite live region. The investigator name and its check are
 * the Controls' ones. The checkboxes live outside the queue's listbox (a listbox option cannot
 * contain a checkbox), so the queue's arrow-key navigation is unchanged.
 */
export function BulkLabel({ onDone }: { onDone: () => void }) {
  const queue = useDashboard((s) => s.queue);
  const cases = useDashboard((s) => s.cases);
  const investigator = useDashboard((s) => s.investigator);
  const upsertCase = useDashboard((s) => s.upsertCase);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [pending, setPending] = useState<FeedbackLabel | null>(null);
  const [asking, setAsking] = useState<{ label: FeedbackLabel; name: string } | null>(null);
  const [outcome, setOutcome] = useState<Outcome>(null);
  const [nameError, setNameError] = useState<string | null>(null);
  const trigger = useRef<HTMLButtonElement | null>(null);
  const allRef = useRef<HTMLInputElement>(null);
  const doneRef = useRef<HTMLButtonElement>(null);
  const [refocus, setRefocus] = useState(false);
  const countId = useId();

  const selected = pruneSelection(picked, queue);
  const n = selected.length;
  const full = n >= MAX_BULK_CASES;
  const allChecked = queue.length > 0 && selected.length === Math.min(queue.length, MAX_BULK_CASES);

  const toggle = (id: string, on: boolean) =>
    setPicked((prev) => {
      const next = new Set(pruneSelection(prev, queue));
      if (on && next.size < MAX_BULK_CASES) next.add(id);
      else if (!on) next.delete(id);
      return next;
    });

  const ask = (label: FeedbackLabel, btn: HTMLButtonElement) => {
    const who = checkInvestigator(investigator);
    if (!who.ok) {
      setNameError(`${who.message} Use the Investigator name field under Case actions.`);
      return;
    }
    setNameError(null);
    trigger.current = btn;
    setAsking({ label, name: who.value });
  };

  const close = () => {
    setAsking(null);
    trigger.current?.focus();
  };

  const submit = async () => {
    if (!asking) return;
    const { label, name } = asking;
    const ids = selected;
    setAsking(null);
    setPending(label);
    setOutcome(null);
    try {
      const r = await api.bulkFeedback(name, label, ids);
      for (const c of r.cases) upsertCase(c);
      setPicked(new Set());
      setOutcome({ kind: "ok", text: `Confirmed: ${r.count} ${r.count === 1 ? "case" : "cases"} labelled ${r.label} by ${r.by}: ${r.case_ids.join(", ")}.` });
    } catch (err) {
      const e = err instanceof ApiError ? err : null;
      const failures = e?.failures ?? [];
      const head = failures.length
        ? `Nothing was changed: ${e?.status ?? ""} ${e?.detail ?? ""}`.replace(/\s+/g, " ").trim()
        : `Nothing was changed: ${e ? e.describe() : String(err)}`;
      setOutcome({ kind: "error", text: head, failures });
    } finally {
      setPending(null);
      setRefocus(true);
    }
  };

  // After the request settles, return focus to the action button that opened the dialog, or
  // (when it is now disabled because the selection was cleared) to "Select all visible".
  useEffect(() => {
    if (!refocus) return;
    setRefocus(false);
    const t = trigger.current;
    (t && !t.disabled ? t : (allRef.current && !allRef.current.disabled ? allRef.current : doneRef.current))?.focus();
  }, [refocus]);

  return (
    <div className="bulk" data-testid="bulk">
      <fieldset className="group bulk__select" aria-describedby={countId}>
        <legend>Select cases for bulk labelling</legend>
        <label className="bulk__all">
          <input
            ref={allRef}
            type="checkbox"
            checked={allChecked}
            disabled={queue.length === 0}
            onChange={(e) => setPicked(new Set(e.target.checked ? selectAllVisible(queue) : []))}
          />{" "}
          Select all visible{queue.length > MAX_BULK_CASES ? ` (first ${MAX_BULK_CASES} of ${queue.length})` : ` (${queue.length})`}
        </label>
        <ul className="bulk__list">
          {queue.map((id) => {
            const c = cases[id];
            if (!c) return null;
            const on = picked.has(id);
            const s = c.suggested_label;
            return (
              <li key={id} className="bulk__item">
                <label>
                  <input
                    type="checkbox"
                    aria-label={`Select ${id} for bulk labelling`}
                    checked={on}
                    disabled={!on && full}
                    onChange={(e) => toggle(id, e.target.checked)}
                  />{" "}
                  <span className="mono">{id}</span> <TierBadge tier={c.tier} /> <span className="muted">risk {formatScore(c.risk)}</span>
                  {s && s.label !== "NONE" ? (
                    <span className="muted small">
                      {" "}
                      · suggests {s.label} {percent(s.confidence)}
                    </span>
                  ) : null}
                </label>
              </li>
            );
          })}
        </ul>
      </fieldset>
      <p id={countId} className="small" data-testid="bulk-count">
        <strong>{n}</strong> selected (max {MAX_BULK_CASES}){full ? ". Limit reached: clear some to pick others." : ""}
      </p>
      <div className="buttons" role="group" aria-label={`Bulk actions for ${n} selected ${n === 1 ? "case" : "cases"}`}>
        {(["CONFIRMED_FRAUD", "FALSE_POSITIVE"] as const).map((label) => (
          <button
            key={label}
            type="button"
            className={`btn${label === "CONFIRMED_FRAUD" ? " btn--danger" : ""}`}
            disabled={n === 0 || pending !== null}
            onClick={(e) => ask(label, e.currentTarget)}
          >
            {LABEL_TEXT[label]}
          </button>
        ))}
        <button type="button" className="btn" disabled={n === 0 || pending !== null} onClick={() => setPicked(new Set())}>
          Clear selection
        </button>
        <button ref={doneRef} type="button" className="btn" onClick={onDone}>
          Done
        </button>
      </div>
      {nameError ? (
        <p role="alert" className="msg msg--error">
          {nameError}
        </p>
      ) : null}
      {pending ? <p className="msg muted">Waiting for confirmation…</p> : null}
      <div role="status" aria-live="polite" className="bulk__result" data-testid="bulk-result">
        {outcome ? (
          <>
            <p className={`msg ${outcome.kind === "ok" ? "msg--ok" : "msg--error"}`}>{outcome.text}</p>
            {outcome.kind === "error" && outcome.failures.length ? (
              <ul className="ev-list" aria-label="Failing cases">
                {outcome.failures.map((f) => (
                  <li key={f.case_id}>
                    <span className="mono">{f.case_id}</span>: {f.reason} ({f.status})
                  </li>
                ))}
              </ul>
            ) : null}
          </>
        ) : null}
      </div>
      {asking ? <ConfirmDialog count={n} label={asking.label} investigator={asking.name} onConfirm={() => void submit()} onCancel={close} /> : null}
    </div>
  );
}

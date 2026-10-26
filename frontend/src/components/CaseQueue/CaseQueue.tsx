import { useMemo, useRef, useState, type KeyboardEvent } from "react";
import type { CaseSummary } from "../../api/types";
import { formatAccounts, formatMoney, formatScore } from "../../format";
import { useDashboard } from "../../store/store";
import { Panel } from "../common/Panel";
import { TierBadge } from "../common/TierBadge";
import { AUTO_CLEARED_TEXT } from "../EvidencePanel/SuggestedLabel";
import { BulkLabel } from "./BulkLabel";

export const AUTO_CLEARED_SHOWN = 20;

/** Cases closed by Auto_Triage (Closure_Reason AUTO_CLEARED), most recently updated first. */
export function autoClearedCases(cases: Readonly<Record<string, CaseSummary>>): CaseSummary[] {
  return Object.values(cases)
    .filter((c) => c.status === "CLOSED" && c.closure_reason === "AUTO_CLEARED")
    .sort((a, b) => (Date.parse(b.updated_at) || 0) - (Date.parse(a.updated_at) || 0) || (a.case_id < b.case_id ? -1 : 1));
}

/**
 * Active_Queue ordered by Case_Priority (Req 22.2) as a single-select listbox with a roving
 * tabindex: Tab enters/leaves the list, Arrow keys / Home / End move focus, Enter or Space
 * selects the focused Case (Req 22.11). Tier is shown as text plus colour (Req 22.10).
 */
export function CaseQueue() {
  const queue = useDashboard((s) => s.queue);
  const cases = useDashboard((s) => s.cases);
  const backlog = useDashboard((s) => s.backlogCount);
  const selected = useDashboard((s) => s.selectedCaseId);
  const selectCase = useDashboard((s) => s.selectCase);
  const [focusId, setFocusId] = useState<string | null>(null);
  const [bulk, setBulk] = useState(false);
  const items = useRef(new Map<string, HTMLLIElement>());
  const autoCleared = useMemo(() => autoClearedCases(cases), [cases]);

  // The tab stop follows focus, falling back to the selection, then the top of the queue.
  const tabStop =
    focusId && queue.includes(focusId) ? focusId : selected && queue.includes(selected) ? selected : (queue[0] ?? null);

  const move = (id: string | undefined) => {
    if (!id) return;
    setFocusId(id);
    items.current.get(id)?.focus();
  };

  const onKeyDown = (e: KeyboardEvent<HTMLUListElement>) => {
    if (queue.length === 0) return;
    const i = tabStop ? queue.indexOf(tabStop) : 0;
    switch (e.key) {
      case "ArrowDown":
        move(queue[Math.min(queue.length - 1, i + 1)]);
        break;
      case "ArrowUp":
        move(queue[Math.max(0, i - 1)]);
        break;
      case "Home":
        move(queue[0]);
        break;
      case "End":
        move(queue[queue.length - 1]);
        break;
      case "Enter":
      case " ":
        if (tabStop) selectCase(tabStop);
        break;
      default:
        return;
    }
    e.preventDefault();
  };

  return (
    <Panel
      title="Case queue"
      area="queue"
      actions={
        <span className="muted" aria-live="off">
          Active <strong>{queue.length}</strong> · Backlog <strong data-testid="backlog-count">{backlog}</strong>
          {autoCleared.length ? (
            <>
              {" "}
              · Auto-cleared <strong data-testid="auto-cleared-count">{autoCleared.length}</strong>
            </>
          ) : null}
        </span>
      }
    >
      {bulk ? (
        <BulkLabel onDone={() => setBulk(false)} />
      ) : queue.length === 0 ? (
        <p className="empty">No active cases.</p>
      ) : (
        <ul className="queue" role="listbox" aria-label="Active queue, highest priority first" onKeyDown={onKeyDown}>
          {queue.map((id) => {
            const c = cases[id];
            if (!c) return null;
            const isSel = id === selected;
            return (
              <li
                key={id}
                id={`case-${id}`}
                ref={(el) => {
                  if (el) items.current.set(id, el);
                  else items.current.delete(id);
                }}
                role="option"
                aria-selected={isSel}
                tabIndex={id === tabStop ? 0 : -1}
                className={`queue__item${isSel ? " queue__item--selected" : ""} queue__item--${c.tier.toLowerCase()}`}
                onClick={() => {
                  setFocusId(id);
                  selectCase(id);
                }}
                onFocus={() => setFocusId(id)}
              >
                <div className="queue__main">
                  <div className="queue__head">
                    <TierBadge tier={c.tier} />
                    <span className="queue__id mono">{c.case_id}</span>
                  </div>
                  <div className="queue__amount">
                    {c.amount_at_risk.length ? c.amount_at_risk.map((a) => formatMoney(a.amount, a.currency)).join(", ") : "none"}
                    <span className="queue__amount-label"> at risk</span>
                  </div>
                  <div className="queue__meta muted">
                    {c.motifs.length ? `${c.motifs.join(", ")} · ` : ""}
                    {c.status === "IN_REVIEW" ? `In review${c.assignee ? ` (${c.assignee})` : ""}` : c.status}
                  </div>
                  <div className="queue__accounts mono" title={c.accounts.join(", ")}>
                    {formatAccounts(c.accounts)}
                  </div>
                </div>
                <div className="queue__score">
                  <span className="queue__risk">
                    <span className="queue__metric-label">Risk</span> <strong>{formatScore(c.risk)}</strong>
                  </span>
                  <span className="queue__bar" aria-hidden="true">
                    <span style={{ width: `${Math.round(Math.min(1, Math.max(0, c.risk)) * 100)}%` }} />
                  </span>
                  <span className="queue__metric" title="Uncertainty_Mass">
                    U <strong>{formatScore(c.uncertainty)}</strong>
                  </span>
                </div>
              </li>
            );
          })}
        </ul>
      )}
      {/* After the list, so Tab still enters the queue first (Req 22.11). */}
      {queue.length > 0 || bulk ? (
        <p className="queue__tools">
          <button type="button" className="btn btn--small" aria-pressed={bulk} onClick={() => setBulk(!bulk)}>
            Bulk label
          </button>{" "}
          <span className="muted small">Select several cases and label them in one step.</span>
        </p>
      ) : null}
      {autoCleared.length ? (
        <details className="ev-details queue__cleared" data-testid="auto-cleared">
          <summary>
            {AUTO_CLEARED_TEXT}: {autoCleared.length} {autoCleared.length === 1 ? "case" : "cases"}
          </summary>
          <p className="muted small">Closed by Auto_Triage after inactivity. No Feedback_Label was stored and nothing was learned from them.</p>
          <ul className="ev-list">
            {autoCleared.slice(0, AUTO_CLEARED_SHOWN).map((c) => (
              <li key={c.case_id} className="queue__cleared-item">
                <button type="button" className="btn btn--small" onClick={() => selectCase(c.case_id)}>
                  View <span className="mono">{c.case_id}</span>
                </button>{" "}
                <TierBadge tier={c.tier} /> <span className="tag tag--auto">{AUTO_CLEARED_TEXT}</span>
              </li>
            ))}
          </ul>
          {autoCleared.length > AUTO_CLEARED_SHOWN ? (
            <p className="muted small">
              Showing the {AUTO_CLEARED_SHOWN} most recent of {autoCleared.length}.
            </p>
          ) : null}
        </details>
      ) : null}
    </Panel>
  );
}

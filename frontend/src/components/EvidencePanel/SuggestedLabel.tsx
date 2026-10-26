import type { CaseSummary, ClosureReason, SuggestedLabel as SuggestedLabelDoc } from "../../api/types";
import { formatTime } from "../../format";

export const SUGGESTION_DISCLAIMER = "Suggestion only: the investigator decides";
export const AUTO_CLEARED_TEXT = "Auto-cleared (not a label)";

export const SUGGESTION_TEXT: Record<SuggestedLabelDoc["label"], string> = {
  CONFIRMED_FRAUD: "Confirmed fraud",
  FALSE_POSITIVE: "False positive",
  NONE: "No suggestion",
};

export const percent = (x: number) => `${Math.round(Math.min(1, Math.max(0, x)) * 100)}%`;

/** Label_Suggester advice (Req 41.5): label, confidence and reason, marked as a suggestion.
 *  It has no apply button: a Feedback_Label is stored only from the Case action buttons. */
export function SuggestedLabel({ suggestion }: { suggestion: SuggestedLabelDoc | null | undefined }) {
  return (
    <div className="suggest" data-testid="ev-suggestion">
      <h3 className="ev-section__title">Suggested label</h3>
      {suggestion ? (
        <>
          <p className="suggest__head">
            <span className={`suggest__label suggest__label--${suggestion.label.toLowerCase()}`}>{SUGGESTION_TEXT[suggestion.label]}</span>{" "}
            <span className="muted">({suggestion.label})</span> · confidence <strong>{percent(suggestion.confidence)}</strong>
          </p>
          <p className="suggest__reason small">{suggestion.reason}</p>
        </>
      ) : (
        <p className="muted">No suggestion from this server.</p>
      )}
      <p className="suggest__disclaimer small">{SUGGESTION_DISCLAIMER}</p>
    </div>
  );
}

export function closureText(reason: ClosureReason | null | undefined, c: Pick<CaseSummary, "label" | "merged_into">): string | null {
  if (reason === "AUTO_CLEARED") return AUTO_CLEARED_TEXT;
  if (reason === "MERGED") return `Closed: merged${c.merged_into ? ` into ${c.merged_into}` : ""}`;
  if (reason === "LABELLED") return `Closed with label ${c.label ?? "unknown"}`;
  return null;
}

/** How a CLOSED Case was closed. AUTO_CLEARED is shown apart from a label: Auto_Triage
 *  closed it for inactivity and no Feedback_Label was stored (Req 41.1, 41.2). */
export function ClosureNote({ c, closedAt }: { c: CaseSummary; closedAt?: string | null }) {
  if (c.status !== "CLOSED") return null;
  const text = closureText(c.closure_reason, c) ?? "Closed";
  const auto = c.closure_reason === "AUTO_CLEARED";
  return (
    <p className={`closure${auto ? " closure--auto" : ""}`} data-testid="ev-closure">
      <strong>{text}</strong>
      {closedAt ? (
        <>
          {" "}
          at{" "}
          <time dateTime={closedAt} className="mono">
            {formatTime(closedAt)}
          </time>
        </>
      ) : null}
      {auto ? <span className="muted"> · closed by Auto_Triage after inactivity; no Feedback_Label was stored.</span> : null}
    </p>
  );
}

// Bridge between the Case action buttons (Controls › CaseActions) and the EvidencePanel's
// keyboard shortcuts (Req 41.9). The buttons register their own click handlers here, so a
// shortcut runs exactly the button's path: the same investigator-name check, open-then-label
// sequence, 5 s confirmation deadline and result message. Nothing else performs the actions.

export type CaseActionKind = "CONFIRMED_FRAUD" | "FALSE_POSITIVE" | "ESCALATE";

export interface CaseShortcut {
  /** The key as shown and as exposed through aria-keyshortcuts. */
  key: "F" | "P" | "E";
  kind: CaseActionKind;
  label: string;
}

export const CASE_SHORTCUTS: readonly CaseShortcut[] = [
  { key: "F", kind: "CONFIRMED_FRAUD", label: "Confirm fraud" },
  { key: "P", kind: "FALSE_POSITIVE", label: "False positive" },
  { key: "E", kind: "ESCALATE", label: "Escalate" },
];

export const shortcutKey = (kind: CaseActionKind): CaseShortcut["key"] => CASE_SHORTCUTS.find((s) => s.kind === kind)!.key;

export interface CaseActionHandle {
  /** The button's onClick. */
  run: () => void;
  /** The button's disabled state (no selection, CLOSED Case, request pending). */
  disabled: boolean;
}

export type CaseActionSource = () => Record<CaseActionKind, CaseActionHandle>;

let source: CaseActionSource | null = null;

/** Register the live button handlers; returns the unregister function. */
export function registerCaseActions(s: CaseActionSource): () => void {
  source = s;
  return () => {
    if (source === s) source = null;
  };
}

export type RunResult = "ran" | "disabled" | "unavailable";

/** Run an action as if its button were clicked. A disabled button does nothing. */
export function runCaseAction(kind: CaseActionKind): RunResult {
  if (!source) return "unavailable";
  const h = source()[kind];
  if (h.disabled) return "disabled";
  h.run();
  return "ran";
}

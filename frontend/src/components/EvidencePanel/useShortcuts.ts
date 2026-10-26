// EvidencePanel keyboard shortcuts (Req 41.9, design §26): F confirm fraud, P false positive,
// E escalate, ? help. Single-character shortcuts are active only while focus is inside the
// panel (WCAG 2.1.4), never in a text field, select or editable element, never with
// Ctrl/Alt/Meta (or Shift on the letters), never on key repeat. F/P/E run the Case action
// buttons' own handlers (Controls › caseActions), so validation and confirmation are shared.

import { useCallback, useState, type KeyboardEvent } from "react";
import { CASE_SHORTCUTS, runCaseAction, type CaseActionKind } from "../Controls/caseActions";

export type Shortcut = CaseActionKind | "HELP";

const TEXT_ENTRY = "input, textarea, select, [contenteditable]:not([contenteditable='false'])";

/** True when the key should go to a form field rather than to a shortcut. */
export function isTextEntry(target: EventTarget | null): boolean {
  if (!(target instanceof Element)) return false;
  if (target instanceof HTMLElement && target.isContentEditable) return true;
  return target.closest(TEXT_ENTRY) !== null;
}

interface KeyLike {
  key: string;
  ctrlKey: boolean;
  altKey: boolean;
  metaKey: boolean;
  shiftKey: boolean;
  repeat: boolean;
  target: EventTarget | null;
}

/** The shortcut a keydown maps to, or null when it must be ignored. */
export function shortcutFor(e: KeyLike): Shortcut | null {
  if (e.ctrlKey || e.altKey || e.metaKey || e.repeat) return null;
  if (isTextEntry(e.target)) return null;
  if (e.key === "?") return "HELP"; // Shift is part of typing "?" on most layouts
  if (e.shiftKey || e.key.length !== 1) return null;
  const k = e.key.toUpperCase();
  return CASE_SHORTCUTS.find((s) => s.key === k)?.kind ?? null;
}

export interface ShortcutState {
  helpOpen: boolean;
  setHelpOpen: (open: boolean) => void;
  /** Last shortcut feedback, for the panel's polite status line. */
  note: string;
  onKeyDown: (e: KeyboardEvent<HTMLElement>) => void;
}

export function useShortcuts(selectedId: string | null): ShortcutState {
  const [helpOpen, setHelpOpen] = useState(false);
  const [note, setNote] = useState("");

  const onKeyDown = useCallback(
    (e: KeyboardEvent<HTMLElement>) => {
      if (e.key === "Escape" && helpOpen && !isTextEntry(e.target)) {
        setHelpOpen(false);
        return;
      }
      const s = shortcutFor(e);
      if (s === null) return;
      if (s === "HELP") {
        e.preventDefault();
        setHelpOpen((o) => !o);
        return;
      }
      if (!selectedId) return;
      e.preventDefault();
      const label = CASE_SHORTCUTS.find((c) => c.kind === s)!.label;
      const r = runCaseAction(s);
      setNote(
        r === "ran"
          ? `${label} requested for ${selectedId}; the result appears under Case actions.`
          : r === "disabled"
            ? `${label} is not available for ${selectedId} right now (closed case or a request in progress).`
            : `${label} is not available: the Case actions controls are not shown.`,
      );
    },
    [helpOpen, selectedId],
  );

  return { helpOpen, setHelpOpen, note, onKeyDown };
}

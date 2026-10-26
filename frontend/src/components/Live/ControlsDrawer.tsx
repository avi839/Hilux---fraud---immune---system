import { useEffect, useId, useRef, type KeyboardEvent } from "react";
import { GeneratorControl, PrioritiserControls } from "../Controls/Controls";
import { DemoScenarioDescription } from "../Controls/DemoScenario";

const FOCUSABLE = 'a[href], button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])';

function focusables(root: HTMLElement | null): HTMLElement[] {
  return root ? [...root.querySelectorAll<HTMLElement>(FOCUSABLE)].filter((el) => !el.closest("[hidden]")) : [];
}

/** Right-side modal drawer with the operator controls: prioritiser settings, the synthetic
 *  generator and what the demo scenario sends. WAI-ARIA dialog: focus moves in on open and
 *  is trapped (Tab / Shift+Tab wrap), Escape or the backdrop closes it, and the caller
 *  returns focus to the trigger in ``onClose``. Mounted only while open. */
export function ControlsDrawer({ id, onClose }: { id: string; onClose: () => void }) {
  const titleId = useId();
  const descId = useId();
  const panel = useRef<HTMLDivElement>(null);
  const closeBtn = useRef<HTMLButtonElement>(null);

  useEffect(() => closeBtn.current?.focus(), []);

  // Escape must close the modal even when focus has fallen out of it, e.g. to <body> after
  // the focused Start/Stop generator button turns disabled. Presses inside the drawer are
  // handled (and stopped) by onKeyDown below, so this only sees the ones from outside.
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    const onDocKey = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape" && !e.defaultPrevented) {
        e.preventDefault();
        closeRef.current();
      }
    };
    document.addEventListener("keydown", onDocKey);
    return () => document.removeEventListener("keydown", onDocKey);
  }, []);

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      onClose();
      return;
    }
    if (e.key !== "Tab") return;
    const items = focusables(panel.current);
    if (items.length === 0) return;
    const first = items[0]!;
    const last = items[items.length - 1]!;
    const at = document.activeElement;
    if (e.shiftKey && (at === first || !panel.current?.contains(at))) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && (at === last || !panel.current?.contains(at))) {
      e.preventDefault();
      first.focus();
    }
  };

  return (
    <div className="lv-drawer-layer">
      <div className="lv-drawer-backdrop" aria-hidden="true" onClick={onClose} data-testid="drawer-backdrop" />
      <div
        ref={panel}
        id={id}
        className="lv-drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descId}
        onKeyDown={onKeyDown}
        data-testid="controls-drawer"
      >
        <div className="lv-drawer__head">
          <h2 id={titleId} className="lv-title">
            Controls
          </h2>
          <button ref={closeBtn} type="button" className="lv-icon-btn" aria-label="Close controls" onClick={onClose}>
            <span aria-hidden="true">×</span>
          </button>
        </div>
        <div className="lv-drawer__body">
          <p id={descId} className="muted small lv-drawer__intro">
            Operator settings. Values change only when the server confirms them.
          </p>
          <PrioritiserControls />
          <GeneratorControl />
          <fieldset className="group">
            <legend>Demo scenario</legend>
            <DemoScenarioDescription />
            <p className="muted small">Send it with Load demo scenario at the top of the dashboard.</p>
          </fieldset>
        </div>
      </div>
    </div>
  );
}

import type { MouseEvent } from "react";
import { HelixLogo } from "../Brand/Logo";
import { ConnectionIndicator } from "./StatusBar";
import { ShellTabList, useShellTab } from "./TabbedDashboard";
import { LIVE_TAB } from "./tabs";

/** Select the Dashboard tab, then put focus on its grid once the panel is visible. */
function useOpenDashboard() {
  const { select } = useShellTab();
  return () => {
    select(LIVE_TAB.id);
    // preventScroll: the view already starts at the top; scrolling #main into view would
    // tuck its first row under the sticky header.
    window.setTimeout(() => document.getElementById("main")?.focus({ preventScroll: true }), 0);
  };
}

/** First focusable element of the page: jumps to the live grid from any tab. */
export function SkipLink() {
  const open = useOpenDashboard();
  const onClick = (e: MouseEvent<HTMLAnchorElement>) => {
    e.preventDefault();
    open();
  };
  return (
    <a className="skip-link" href="#main" onClick={onClick}>
      Skip to dashboard
    </a>
  );
}

/** Sticky app header: wordmark left, view tabs centred, connection and the primary action
 *  right. Must be rendered inside ShellProvider. */
export function AppHeader() {
  const open = useOpenDashboard();
  const { active, all } = useShellTab();
  // One h1 per view: the first (Home) tab has its own hero heading, every other view uses
  // the header wordmark.
  const Name = active === all[0]?.id && active !== LIVE_TAB.id ? "p" : "h1";
  return (
    <header className="topbar hx-header" data-testid="app-header">
      <div className="hx-header__brand">
        <HelixLogo size={24} />
        <Name className="hx-header__name">HELIX</Name>
      </div>
      <div className="hx-header__nav">
        <ShellTabList />
      </div>
      <div className="hx-header__actions">
        <ConnectionIndicator />
        <button type="button" className="hx-pill hx-pill--primary hx-pill--sm" onClick={open}>
          Open dashboard
        </button>
      </div>
    </header>
  );
}

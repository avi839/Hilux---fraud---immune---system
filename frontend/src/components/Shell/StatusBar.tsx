import { useDashboard } from "../../store/store";

/** [visible label, rest of the announced text]. The full text is the two joined; the compact
 *  header chip shows the first part and keeps the rest for screen readers and the tooltip
 *  (the connection banner below the header explains the outage in full). */
const LABEL = {
  open: ["Live", ""],
  connecting: ["Connecting…", ""],
  down: ["Disconnected", " · reconnecting, showing last received data"],
} as const;

/** Connection indicator (Req 22.12, 22.13): text + colour dot, in a polite status region. */
export function ConnectionIndicator() {
  const connection = useDashboard((s) => s.connection);
  const [shown, rest] = LABEL[connection];
  return (
    <div role="status" className={`conn conn--${connection}`} data-testid="connection" title={shown + rest}>
      <span className="conn__dot" aria-hidden="true" />
      <span>{shown}</span>
      {rest ? <span className="sr-only">{rest}</span> : null}
    </div>
  );
}

/** Screen-reader announcements of new HIGH (polite) and CRITICAL (assertive) alerts. Both
 *  regions exist from first render so assistive tech registers them before content changes. */
export function AlertAnnouncer() {
  const a = useDashboard((s) => s.announcement);
  return (
    <>
      <div className="sr-only" aria-live="polite" aria-atomic="true" data-testid="announce-polite">
        {a?.politeness === "polite" ? <span key={a.id}>{a.text}</span> : null}
      </div>
      <div className="sr-only" aria-live="assertive" aria-atomic="true" data-testid="announce-assertive">
        {a?.politeness === "assertive" ? <span key={a.id}>{a.text}</span> : null}
      </div>
    </>
  );
}

/** Shown while the live connection is down: what is wrong and how to fix it. */
export function ConnectionBanner() {
  const connection = useDashboard((s) => s.connection);
  if (connection !== "down") return null;
  return (
    <p className="banner" data-testid="connection-banner">
      Cannot reach the HELIX API_Server through this page (/ws and /api are proxied to 127.0.0.1:8000). Start it with{" "}
      <code>scripts\demo.ps1</code> or <code>python -m helix.api --generator</code> in backend\. Reconnecting automatically; the last
      received data stays on screen.
    </p>
  );
}

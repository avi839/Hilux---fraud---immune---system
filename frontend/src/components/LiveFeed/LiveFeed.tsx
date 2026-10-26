import { memo } from "react";
import type { Tier } from "../../api/types";
import { formatScore, formatTime, groupDigits } from "../../format";
import type { FeedItem } from "../../store/reducers";
import { useDashboard } from "../../store/store";
import { Panel } from "../common/Panel";
import { TierBadge } from "../common/TierBadge";

function riskClass(r: number): string {
  if (r >= 0.8) return "risk risk--critical";
  if (r >= 0.6) return "risk risk--high";
  if (r >= 0.4) return "risk risk--medium";
  return "risk risk--low";
}

/** One scored event. The tier chip is the Severity_Tier of the Case the event joined (from
 *  /ws/cases); events outside any known Case have no tier, so none is shown. */
const Row = memo(function Row({ e, tier }: { e: FeedItem; tier: Tier | null }) {
  const pct = Math.round(Math.min(1, Math.max(0, e.risk)) * 100);
  const time = formatTime(e.ts);
  // "HH:MM:SS.mmm", or "YYYY-MM-DD HH:MM:SS.mmm" for another day: shown as the clock to the
  // second with the date (if any) beneath; the full value is in the title and datetime.
  const [date, clock] = time.includes(" ") ? time.split(" ", 2) : [null, time];
  return (
    <tr>
      <td className="mono feed__time" title={time}>
        <time dateTime={e.ts}>
          <span>{(clock ?? time).replace(/\.\d{3}$/, "")}</span>
          {date ? <span className="feed__date">{date}</span> : null}
        </time>
        {e.late ? <span className="tag">late</span> : null}
      </td>
      <td className="feed__tx" title={`${e.tx_id}: ${e.src} → ${e.dst}`}>
        <span className="mono feed__txid">{e.tx_id}</span>
        <span className="mono feed__route">
          {e.src} <span aria-hidden="true">→</span>
          <span className="sr-only">to</span> {e.dst}
        </span>
      </td>
      <td className="num">
        {groupDigits(e.amount)} <span className="muted">{e.currency}</span>
      </td>
      <td className="num feed__risk">
        <span className="feed__bar" aria-hidden="true">
          <span style={{ width: `${pct}%` }} />
        </span>
        <span className={riskClass(e.risk)}>{formatScore(e.risk, 3)}</span>
      </td>
      <td className="feed__case">
        {tier ? (
          <span className="feed__case-in">
            <TierBadge tier={tier} /> <span className="mono small">{e.case_id}</span>
          </span>
        ) : (
          <span className="muted small">{e.case_id ? <span className="mono">{e.case_id}</span> : "none"}</span>
        )}
      </td>
    </tr>
  );
});

/** Why the feed is empty, and what makes events flow. */
function emptyText(connection: string, generator: boolean | null): string {
  if (connection === "down") return "Not connected to the API_Server; events will appear once it is reachable.";
  if (connection === "connecting") return "Connecting to the live event stream…";
  if (generator === false)
    return "Connected, waiting for events. The synthetic generator is stopped: press Start generator or Load demo scenario.";
  if (generator === null) return "Connected, waiting for events. No generator is configured: submit events or press Load demo scenario.";
  return "Connected, waiting for the first scored events…";
}

/** Live feed of the 200 most recent scored events, newest first (Req 22.1). */
export function LiveFeed() {
  const feed = useDashboard((s) => s.feed);
  const cases = useDashboard((s) => s.cases);
  const connection = useDashboard((s) => s.connection);
  const generator = useDashboard((s) => s.confirmed.generatorRunning);
  return (
    <Panel title="Live feed" area="feed" actions={<span className="muted small">{feed.length} / 200, newest first</span>}>
      {/* tabIndex lets keyboard users scroll the region (it has no focusable children). */}
      <div className="scroll feed__scroll" tabIndex={0} role="region" aria-label="Live transaction feed, newest first">
        <table className="table feed">
          <thead>
            <tr>
              <th scope="col">Time (UTC)</th>
              <th scope="col">
                Tx ID · from <span aria-hidden="true">→</span> to
              </th>
              <th scope="col" className="num">
                Amount
              </th>
              <th scope="col" className="num">
                Risk
              </th>
              <th scope="col">Case</th>
            </tr>
          </thead>
          <tbody>
            {feed.length === 0 ? (
              <tr>
                <td colSpan={5} className="empty" data-testid="feed-empty">
                  {emptyText(connection, generator)}
                </td>
              </tr>
            ) : (
              feed.map((e) => <Row key={e.key} e={e} tier={e.case_id ? (cases[e.case_id]?.tier ?? null) : null} />)
            )}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}

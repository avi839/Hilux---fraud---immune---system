// Thin strip of live numbers from the dashboard store. Shows only what the store holds: when
// the API server is not connected, or connected but nothing has arrived yet, it says so
// instead of showing numbers.

import { useDashboard } from "../../store/store";
import type { MetricsMessage } from "../../api/types";
import { normaliseMetrics } from "../LoopPanel/loop";

const CONN_TEXT = { open: "Connected", connecting: "Connecting…", down: "Disconnected" } as const;

/** Events through the pipeline since the server started, if the metrics report it. */
export function eventsSeen(metrics: MetricsMessage | null): number | null {
  if (!metrics) return null;
  const stages = normaliseMetrics(metrics).stages;
  return stages?.END_TO_END?.total ?? stages?.PERCEIVE?.total ?? null;
}

const fmt = (n: number | null) => (n === null ? "n/a" : n.toLocaleString("en-US"));

export function LiveStrip() {
  const connection = useDashboard((s) => s.connection);
  const metrics = useDashboard((s) => s.metrics);
  const active = useDashboard((s) => s.queue.length);
  const backlog = useDashboard((s) => s.backlogCount);
  const caseCount = useDashboard((s) => Object.keys(s.cases).length);
  const feedCount = useDashboard((s) => s.feed.length);
  const refreshed = useDashboard((s) => s.lastRefresh !== null);

  const hasData = metrics !== null || caseCount > 0 || feedCount > 0 || refreshed;
  const live = connection === "open" && hasData;
  const message =
    connection !== "open"
      ? "Connect the API server to see live numbers."
      : "Connected. Numbers appear as soon as the first events arrive.";

  return (
    <section className="home-live" aria-labelledby="home-live-title" data-testid="home-live">
      <div className="home-live__inner">
        <h2 id="home-live-title" className="home-live__title">
          Live now
        </h2>
        <dl className="home-live__tiles">
          <div className={`hx-kpi home-kpi home-kpi--conn home-kpi--${connection}`}>
            <dt>API server</dt>
            <dd data-testid="home-kpi-connection">
              <span className="home-kpi__dot" aria-hidden="true" />
              {CONN_TEXT[connection]}
            </dd>
          </div>
          {live ? (
            <>
              <div className="hx-kpi home-kpi">
                <dt>Events seen</dt>
                <dd data-testid="home-kpi-events">{fmt(eventsSeen(metrics))}</dd>
              </div>
              <div className="hx-kpi home-kpi">
                <dt>Active cases</dt>
                <dd data-testid="home-kpi-active">{fmt(active)}</dd>
              </div>
              <div className="hx-kpi home-kpi">
                <dt>Backlog</dt>
                <dd data-testid="home-kpi-backlog">{fmt(backlog)}</dd>
              </div>
            </>
          ) : (
            <div className="hx-kpi home-kpi home-kpi--empty">
              <dt className="sr-only">Live numbers</dt>
              <dd data-testid="home-kpi-empty">{message}</dd>
            </div>
          )}
        </dl>
      </div>
    </section>
  );
}

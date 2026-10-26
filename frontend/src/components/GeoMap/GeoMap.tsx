import { lazy, Suspense, useMemo, useState } from "react";
import { formatTime } from "../../format";
import { useDashboard } from "../../store/store";
import { isNoStorage, useCaseVersion, useEvidenceDoc, useTimeline } from "../../viz/caseData";
import { buildGeo, eventsFromChain, eventsFromTimeline, formatHours, formatKm, formatSpeed, type GeoHop, type GeoModel } from "../../viz/geo";
import { formatUtc } from "../../viz/time";
import { hasWebGL } from "../../viz/webgl";
import { WebGLBoundary } from "../common/ErrorBoundary";
import { Panel } from "../common/Panel";

// deck.gl lives in a separate chunk, fetched only when the map shows.
const GeoMapView = lazy(() => import("./GeoMapView"));

function hopKey(h: GeoHop) {
  return `${h.account}|${h.toTx}`;
}

function LocatedList({ geo }: { geo: GeoModel }) {
  const hopTo = new Map(geo.hops.map((h) => [hopKey(h), h]));
  // Group rows per account in ts order so each hop reads from the row above.
  const rows = [...geo.points].sort((a, b) => (a.account < b.account ? -1 : a.account > b.account ? 1 : a.tMs - b.tMs));
  return (
    <div className="scroll scroll--short" tabIndex={0} role="region" aria-label="Located events" data-testid="geo-list">
      <table className="table table--compact">
        <caption className="sr-only">
          Located events per source account in time order, with the distance, time and implied speed from that account's previous located
          event
        </caption>
        <thead>
          <tr>
            <th scope="col">Account</th>
            <th scope="col">Tx</th>
            <th scope="col">Time (UTC)</th>
            <th scope="col" className="num">
              Lat, lon
            </th>
            <th scope="col" className="num">
              Distance
            </th>
            <th scope="col" className="num">
              Δt
            </th>
            <th scope="col" className="num">
              Implied speed
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 ? (
            <tr>
              <td colSpan={7} className="empty">
                No located events.
              </td>
            </tr>
          ) : (
            rows.map((p) => {
              const h = hopTo.get(`${p.account}|${p.tx_id}`);
              return (
                <tr key={p.tx_id} data-testid={`geo-row-${p.tx_id}`}>
                  <td className="mono">{p.account}</td>
                  <td className="mono">
                    {p.tx_id}
                    {p.inCase ? null : <span className="muted small"> (context)</span>}
                  </td>
                  <td className="mono">
                    <time dateTime={p.ts}>{formatTime(p.ts)}</time>
                  </td>
                  <td className="num mono">
                    {p.lat.toFixed(4)}, {p.lon.toFixed(4)}
                  </td>
                  <td className="num">{h ? formatKm(h.distanceKm) : "–"}</td>
                  <td className="num">{h ? formatHours(h.dtH) : "–"}</td>
                  <td className="num">{h ? formatSpeed(h.speedKmh) : "first located"}</td>
                </tr>
              );
            })
          )}
        </tbody>
      </table>
    </div>
  );
}

/** Located Case events on a map, with per-account hop lines and an omitted-count label
 *  (Req 22.5, 22.6). No basemap or tile server: see mapStyle.ts for why. */
export function GeoMap() {
  const selected = useDashboard((s) => s.selectedCaseId);
  const version = useCaseVersion(selected);
  const replayT = useDashboard((s) => s.replayT);
  const [webgl] = useState(hasWebGL);
  const [view, setView] = useState<"map" | "list">(() => (webgl ? "map" : "list"));
  const [reload, setReload] = useState(0);

  const timeline = useTimeline(selected, version, reload);
  const evidence = useEvidenceDoc(selected, version);
  const chain = evidence.status === "ready" ? evidence.data.chain : null;
  const noStorage = timeline.status === "error" && isNoStorage(timeline.error);

  const events = useMemo(() => {
    if (timeline.status === "ready") {
      const caseTx = chain ? new Set(chain.entries.map((e) => e.tx_id)) : undefined;
      return eventsFromTimeline(timeline.data.entries, caseTx);
    }
    if (noStorage && evidence.status === "ready") return eventsFromChain(chain);
    return null;
  }, [timeline, noStorage, evidence.status, chain]);
  const geo = useMemo(() => (events ? buildGeo(events, replayT) : null), [events, replayT]);

  const toggle = (
    <button
      type="button"
      className="btn btn--small"
      aria-pressed={view === "list"}
      disabled={!webgl}
      title={webgl ? undefined : "The map needs WebGL, which this browser does not provide"}
      onClick={() => setView((v) => (v === "map" ? "list" : "map"))}
    >
      List view
    </button>
  );

  const loading = !!selected && !geo && (timeline.status === "loading" || timeline.status === "idle" || (noStorage && evidence.status === "loading"));

  return (
    <Panel title="Map" area="geo" busy={loading} actions={selected ? toggle : null}>
      {!selected ? (
        <p className="empty">Select a case to map its located events.</p>
      ) : timeline.status === "error" && !noStorage ? (
        <div role="alert" className="msg msg--error" data-testid="geo-error">
          <p className="msg__text">Could not load the timeline for {selected}: {timeline.message}</p>
          <button type="button" className="btn" onClick={() => setReload((n) => n + 1)}>
            Retry
          </button>
        </div>
      ) : noStorage && evidence.status === "error" ? (
        <p role="alert" className="msg msg--error" data-testid="geo-error">
          Timeline unavailable (server started without --storage) and the evidence could not be loaded: {evidence.message}
        </p>
      ) : !geo ? (
        <p role="status" className="muted" data-testid="geo-loading">
          Loading located events for {selected}…
        </p>
      ) : (
        <>
          <p className="viz-summary small" data-testid="geo-summary">
            {geo.points.length} located event{geo.points.length === 1 ? "" : "s"} · {geo.paths.length} account path
            {geo.paths.length === 1 ? "" : "s"} ·{" "}
            <strong data-testid="geo-omitted">
              {geo.omitted} of {geo.total} event{geo.total === 1 ? "" : "s"} omitted: no location data
            </strong>
          </p>
          {noStorage ? (
            <p className="muted small" data-testid="geo-fallback">
              Timeline unavailable (server started without --storage): locations are taken from Evidence_Chain features, which may not include
              coordinates.
            </p>
          ) : null}
          {replayT !== null ? <p className="muted small">Replay: events at or before {formatUtc(replayT)}.</p> : null}
          {!webgl ? <p className="muted small">Map unavailable: this browser has no WebGL. Showing the list view.</p> : null}
          {view === "map" && webgl ? (
            <div
              className="viz-stage"
              role="img"
              aria-label={`Map of ${geo.points.length} located events with ${geo.paths.length} account paths. Use List view for distances and speeds.`}
            >
              <WebGLBoundary what="map" onList={() => setView("list")}>
                <Suspense fallback={<p className="muted">Loading map…</p>}>
                  <GeoMapView geo={geo} />
                </Suspense>
              </WebGLBoundary>
            </div>
          ) : (
            <LocatedList geo={geo} />
          )}
          <p className="muted small">
            Offline map: plain background with a 30° lat/lon grid, no map tiles are fetched, so locations stay in this browser. Lines join each
            source account's located events in time order.
          </p>
        </>
      )}
    </Panel>
  );
}

import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../../api/client";
import type { FrameDoc, NeighbourhoodDoc, SubgraphDoc } from "../../api/types";
import { formatMoney, formatTime } from "../../format";
import { useDashboard } from "../../store/store";
import { describeError, useCaseVersion, useEvidenceDoc, useTimeline } from "../../viz/caseData";
import { mergeNeighbourhoods, motifColour, motifEdgeIndex, NODE_COLOUR, toGraph, type GraphModel } from "../../viz/graph";
import { frameParam } from "../../viz/replay";
import { formatUtc } from "../../viz/time";
import { hasWebGL } from "../../viz/webgl";
import { WebGLBoundary } from "../common/ErrorBoundary";
import { Panel } from "../common/Panel";

// three.js + the force layout live in a separate chunk, fetched only when the 3D view shows.
const ForceGraphView = lazy(() => import("./ForceGraphView"));

/** Debounce of GET /timeline/at while the replay slider moves; the local filter applies at once. */
export const FRAME_DEBOUNCE_MS = 250;

type SubLoad =
  | { status: "loading"; id: string }
  | { status: "error"; id: string; message: string }
  | { status: "ready"; id: string; sub: SubgraphDoc };

const isAbort = (e: unknown) => e instanceof ApiError && e.kind === "aborted";

function useSubgraph(id: string | null, version: string | undefined, reload: number): SubLoad | null {
  const [load, setLoad] = useState<SubLoad | null>(null);
  useEffect(() => {
    if (!id) {
      setLoad(null);
      return;
    }
    const ctrl = new AbortController();
    setLoad((prev) => (prev?.status === "ready" && prev.id === id ? prev : { status: "loading", id }));
    api.getSubgraph(id, undefined, ctrl.signal).then(
      (sub) => setLoad({ status: "ready", id, sub }),
      (err: unknown) => !isAbort(err) && setLoad({ status: "error", id, message: describeError(err) }),
    );
    return () => ctrl.abort();
  }, [id, version, reload]);
  return load && load.id === id ? load : id ? { status: "loading", id } : null;
}

/** Server frame for the replay position, fetched after the slider rests for FRAME_DEBOUNCE_MS. */
function useFrame(id: string | null, t: string | null): FrameDoc | null {
  const [frame, setFrame] = useState<{ key: string; doc: FrameDoc } | null>(null);
  const key = id && t ? `${id}|${t}` : null;
  useEffect(() => {
    if (!id || !t) return;
    const ctrl = new AbortController();
    const timer = setTimeout(() => {
      api.getFrame(id, t, ctrl.signal).then(
        (doc) => setFrame({ key: `${id}|${t}`, doc }),
        () => undefined, // the local filter stays in effect
      );
    }, FRAME_DEBOUNCE_MS);
    return () => {
      clearTimeout(timer);
      ctrl.abort();
    };
  }, [id, t]);
  return frame && frame.key === key ? frame.doc : null;
}

function Legend({ graph }: { graph: GraphModel }) {
  return (
    <ul className="viz-legend small" aria-label="Network legend">
      <li>
        <span className="viz-key viz-key--node" style={{ background: NODE_COLOUR.case }} aria-hidden="true" /> Case account (large)
      </li>
      <li>
        <span className="viz-key viz-key--node viz-key--small" style={{ background: NODE_COLOUR.other }} aria-hidden="true" /> Related account
      </li>
      <li>
        <span className="viz-key viz-key--node viz-key--small" style={{ background: NODE_COLOUR.expanded }} aria-hidden="true" /> Expanded
      </li>
      {graph.motifTypes.map((m) => (
        <li key={m}>
          <span className="viz-key viz-key--edge" style={{ background: motifColour(m, graph.motifTypes) }} aria-hidden="true" /> Motif edge:{" "}
          {m}
        </li>
      ))}
    </ul>
  );
}

function NetworkLists({ graph, onExpand, pending }: { graph: GraphModel; onExpand: (a: string) => void; pending: string | null }) {
  return (
    <div className="viz-lists" data-testid="network-list">
      <div className="scroll scroll--short" tabIndex={0} role="region" aria-label="Accounts in the related subgraph">
        <table className="table table--compact">
          <caption className="sr-only">Accounts ({graph.nodes.length}), case accounts first</caption>
          <thead>
            <tr>
              <th scope="col">Account</th>
              <th scope="col">Role</th>
              <th scope="col" className="num">
                Hop
              </th>
              <th scope="col">Action</th>
            </tr>
          </thead>
          <tbody>
            {graph.nodes.map((n) => (
              <tr key={n.id}>
                <td className="mono">{n.id}</td>
                <td>{n.isCase ? <strong>Case account</strong> : n.expanded ? "Related, expanded" : "Related"}</td>
                <td className="num">{n.hop}</td>
                <td>
                  <button
                    type="button"
                    className="btn btn--small"
                    onClick={() => onExpand(n.id)}
                    disabled={pending !== null}
                    aria-label={`Expand ${n.id}`}
                  >
                    {pending === n.id ? "Expanding…" : "Expand"}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="scroll scroll--short" tabIndex={0} role="region" aria-label="Transactions in the related subgraph">
        <table className="table table--compact">
          <caption className="sr-only">Transactions ({graph.links.length}) by time</caption>
          <thead>
            <tr>
              <th scope="col">Tx</th>
              <th scope="col">Time (UTC)</th>
              <th scope="col">From</th>
              <th scope="col">To</th>
              <th scope="col" className="num">
                Amount
              </th>
              <th scope="col">Motif</th>
            </tr>
          </thead>
          <tbody>
            {graph.links.length === 0 ? (
              <tr>
                <td colSpan={6} className="empty">
                  No transactions.
                </td>
              </tr>
            ) : (
              graph.links.map((l) => (
                <tr key={l.id} data-motif={l.motifs.length ? l.motifs.join(",") : undefined}>
                  <td className="mono">{l.id}</td>
                  <td className="mono">
                    <time dateTime={l.ts}>{formatTime(l.ts)}</time>
                  </td>
                  <td className="mono">{l.source}</td>
                  <td className="mono">{l.target}</td>
                  <td className="num">{formatMoney(l.amount, l.currency)}</td>
                  <td>{l.motifs.length ? <strong>{l.motifs.join(", ")}</strong> : "–"}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/** Related_Subgraph of the selected Case as a 3D force-directed network with a keyboard and
 *  screen-reader list view (Req 22.4, 22.6, 17.1-17.3). */
export function Network3D() {
  const selected = useDashboard((s) => s.selectedCaseId);
  const version = useCaseVersion(selected);
  const replayT = useDashboard((s) => s.replayT);
  const [webgl] = useState(hasWebGL);
  const [view, setView] = useState<"3d" | "list">(() => (webgl ? "3d" : "list"));
  const [reload, setReload] = useState(0);
  const [expansions, setExpansions] = useState<{ id: string | null; items: NeighbourhoodDoc[] }>({ id: null, items: [] });
  const [pending, setPending] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ kind: "ok" | "error"; text: string } | null>(null);

  const load = useSubgraph(selected, version, reload);
  const evidence = useEvidenceDoc(selected, version);
  const timeline = useTimeline(selected, version);
  const tl = timeline.status === "ready" ? timeline.data : null;
  const tParam = replayT !== null && tl ? frameParam(replayT, tl) : null;
  const frame = useFrame(selected, tParam);

  useEffect(() => {
    setExpansions({ id: selected, items: [] });
    setNotice(null);
    setPending(null);
  }, [selected]);

  const chain = evidence.status === "ready" ? evidence.data.chain : null;
  const motifs = useMemo(() => motifEdgeIndex(chain), [chain]);
  const items = expansions.id === selected ? expansions.items : [];
  const live = load?.status === "ready" ? load.sub : null;
  // Replay: the server frame (window [T - Graph_Window, T]) once it arrives; meanwhile the
  // live subgraph filtered locally to edges at or before T.
  const base = replayT !== null && frame ? frame.subgraph : live;
  const merged = useMemo(() => (base ? mergeNeighbourhoods(base, items) : null), [base, items]);
  const expandedSet = useMemo(() => new Set(items.map((i) => i.account)), [items]);
  const graph = useMemo(
    () => (merged ? toGraph(merged, motifs, { t: replayT, expanded: expandedSet }) : null),
    [merged, motifs, replayT, expandedSet],
  );

  const expand = useCallback(
    (account: string) => {
      if (!selected || pending) return;
      setPending(account);
      setNotice(null);
      const caseId = selected;
      api.expand(caseId, account, tParam).then(
        (nb) => {
          setExpansions((prev) => (prev.id === caseId ? { id: caseId, items: [...prev.items, nb] } : prev));
          const known = new Set(merged?.nodes.map((n) => n.account));
          const added = nb.nodes.filter((n) => !known.has(n.account)).length;
          setNotice({ kind: "ok", text: `Expanded ${account}: ${nb.nodes.length - 1} neighbours, ${added} new.` });
          setPending(null);
        },
        (err: unknown) => {
          setNotice({ kind: "error", text: `Could not expand ${account}: ${describeError(err)}` });
          setPending(null);
        },
      );
    },
    [selected, pending, tParam, merged],
  );

  const toggle = (
    <button
      type="button"
      className="btn btn--small"
      aria-pressed={view === "list"}
      disabled={!webgl}
      title={webgl ? undefined : "3D view needs WebGL, which this browser does not provide"}
      onClick={() => setView((v) => (v === "3d" ? "list" : "3d"))}
    >
      List view
    </button>
  );

  return (
    <Panel title="Network" area="network" busy={load?.status === "loading"} actions={selected ? toggle : null}>
      {!selected ? (
        <p className="empty">Select a case to see its related accounts.</p>
      ) : !load || load.status === "loading" ? (
        <p role="status" className="muted" data-testid="net-loading">
          Loading related accounts for {selected}…
        </p>
      ) : load.status === "error" ? (
        <div role="alert" className="msg msg--error" data-testid="net-error">
          <p className="msg__text">Could not load the related subgraph for {selected}: {load.message}</p>
          <button type="button" className="btn" onClick={() => setReload((n) => n + 1)}>
            Retry
          </button>
        </div>
      ) : graph ? (
        <>
          <p className="viz-summary small" data-testid="net-summary">
            {graph.nodes.length} accounts ({graph.nodes.filter((n) => n.isCase).length} case) · {graph.links.length} transactions
            {graph.motifTypes.length ? ` · motif edges: ${graph.motifTypes.join(", ")}` : ""}
            {graph.truncated ? (
              <span className="badge badge--warn viz-badge" data-testid="net-truncated">
                Truncated: {graph.omitted} account{graph.omitted === 1 ? "" : "s"} omitted
              </span>
            ) : null}
          </p>
          {replayT !== null ? (
            <p className="muted small" data-testid="net-replay">
              Replay at {formatUtc(replayT)}: {frame ? "server frame (Graph_Window ending at T)" : "transactions at or before T"}
              {graph.hiddenByReplay ? `, ${graph.hiddenByReplay} later transactions hidden` : ""}.
            </p>
          ) : null}
          {!webgl ? <p className="muted small">3D view unavailable: this browser has no WebGL. Showing the list view.</p> : null}
          {notice ? (
            <p role={notice.kind === "error" ? "alert" : "status"} className={`msg ${notice.kind === "error" ? "msg--error" : "msg--ok"}`}>
              {notice.text}
            </p>
          ) : null}
          {view === "3d" && webgl ? (
            <>
              <div
                className="viz-stage"
                role="img"
                aria-label={`3D network of ${graph.nodes.length} accounts and ${graph.links.length} transactions. Drag to rotate, right-drag to pan, scroll to zoom, click an account to expand it. Use List view for a text version.`}
              >
                <WebGLBoundary what="3D network" onList={() => setView("list")}>
                  <Suspense fallback={<p className="muted">Loading 3D view…</p>}>
                    <ForceGraphView graph={graph} onNodeClick={expand} />
                  </Suspense>
                </WebGLBoundary>
              </div>
              <Legend graph={graph} />
            </>
          ) : (
            <NetworkLists graph={graph} onExpand={expand} pending={pending} />
          )}
        </>
      ) : null}
    </Panel>
  );
}

import { useEffect, useId, useMemo, useState, type KeyboardEvent } from "react";
import { dashboard, useDashboard } from "../../store/store";
import { isNoStorage, useCaseVersion, useTimeline } from "../../viz/caseData";
import { clampT, entriesAt, eventStops, stepT, timelineSpan } from "../../viz/replay";
import { formatUtc } from "../../viz/time";
import { Panel } from "../common/Panel";

/** Playback advances one event per tick. */
export const PLAY_INTERVAL_MS = 700;
export const NO_STORAGE_MESSAGE = "Replay needs the server started with --storage.";

/**
 * Replay_Timeline control (Req 22.6). Moving it sets the shared ``replayT``; the network, map
 * and evidence panels filter to data at or before T on the next render, and the network also
 * fetches the server frame (debounced). Keys on the slider: ←/→ previous/next event,
 * Page Up/Down ±10% of the span, Home/End start/end.
 */
export function ReplaySlider() {
  const selected = useDashboard((s) => s.selectedCaseId);
  const version = useCaseVersion(selected);
  const replayT = useDashboard((s) => s.replayT);
  const setReplayT = useDashboard((s) => s.setReplayT);
  const [reload, setReload] = useState(0);
  const [playing, setPlaying] = useState(false);
  const hintId = useId();
  const sliderId = useId();

  const timeline = useTimeline(selected, version, reload);
  const tl = timeline.status === "ready" ? timeline.data : null;
  const span = useMemo(() => (tl ? timelineSpan(tl) : null), [tl]);
  const stops = useMemo(() => (tl && span ? eventStops(tl.entries, span) : []), [tl, span]);
  const t = span ? clampT(replayT ?? span.endMs, span) : null;
  const shown = useMemo(() => (tl ? entriesAt(tl.entries, replayT).length : 0), [tl, replayT]);

  useEffect(() => setPlaying(false), [selected]);

  // Playback: step to the next event until the end of the timeline.
  useEffect(() => {
    if (!playing || !span) return;
    const id = setInterval(() => {
      // Read the latest position outside render: the timer outlives several renders.
      const cur = clampT(dashboard.getState().replayT ?? span.startMs, span);
      if (cur >= span.endMs) {
        setPlaying(false);
        return;
      }
      setReplayT(stepT(stops, cur, 1, span));
    }, PLAY_INTERVAL_MS);
    return () => clearInterval(id);
  }, [playing, span, stops, setReplayT]);

  const move = (next: number) => span && setReplayT(clampT(next, span));

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (!span || t === null) return;
    const big = (span.endMs - span.startMs) / 10;
    const handlers: Record<string, () => number> = {
      ArrowRight: () => stepT(stops, t, 1, span),
      ArrowUp: () => stepT(stops, t, 1, span),
      ArrowLeft: () => stepT(stops, t, -1, span),
      ArrowDown: () => stepT(stops, t, -1, span),
      PageUp: () => t + big,
      PageDown: () => t - big,
      Home: () => span.startMs,
      End: () => span.endMs,
    };
    const h = handlers[e.key];
    if (!h) return;
    e.preventDefault();
    move(h());
  };

  const play = () => {
    if (!span) return;
    if (playing) {
      setPlaying(false);
      return;
    }
    // Restart from the beginning when already at the end.
    if (t === null || t >= span.endMs) setReplayT(span.startMs);
    setPlaying(true);
  };

  let body;
  if (!selected) {
    body = <p className="empty">Select a case to replay its timeline.</p>;
  } else if (timeline.status === "error") {
    body = isNoStorage(timeline.error) ? (
      <p role="status" className="msg muted" data-testid="replay-no-storage">
        {NO_STORAGE_MESSAGE} Without Storage there is no Replay_Timeline, so the network, map and evidence show the current state only.
      </p>
    ) : (
      <div role="alert" className="msg msg--error" data-testid="replay-error">
        <p className="msg__text">Could not load the replay timeline for {selected}: {timeline.message}</p>
        <button type="button" className="btn" onClick={() => setReload((n) => n + 1)}>
          Retry
        </button>
      </div>
    );
  } else if (!tl) {
    body = (
      <p role="status" className="muted" data-testid="replay-loading">
        Loading replay timeline for {selected}…
      </p>
    );
  } else if (!span || t === null) {
    body = <p className="msg msg--error">The replay timeline for {selected} has an invalid span.</p>;
  } else {
    const flat = span.endMs === span.startMs;
    const totalS = Math.ceil((span.endMs - span.startMs) / 1000);
    const valueS = Math.round((t - span.startMs) / 1000);
    const atEvent = stops.includes(t);
    body = (
      <>
        <label htmlFor={sliderId} className="replay__label">
          Replay time
        </label>
        <input
          id={sliderId}
          type="range"
          className="replay__range"
          min={0}
          max={totalS}
          step={1}
          value={valueS}
          disabled={flat}
          aria-valuetext={`${formatUtc(t)}, ${shown} of ${tl.entries.length} events`}
          aria-describedby={hintId}
          onChange={(e) => move(e.currentTarget.valueAsNumber >= totalS ? span.endMs : span.startMs + e.currentTarget.valueAsNumber * 1000)}
          onKeyDown={onKeyDown}
        />
        <div className="replay__ends small muted" aria-hidden="true">
          <span>{formatUtc(span.startMs)}</span>
          <span>{formatUtc(span.endMs)}</span>
        </div>
        <p className="replay__now" data-testid="replay-now">
          {replayT === null ? "Current state" : `T = ${formatUtc(t)}`}
          {atEvent ? " (event)" : ""} · {shown} of {tl.entries.length} events at or before T
        </p>
        <div className="buttons" role="group" aria-label="Replay controls">
          <button type="button" className="btn" onClick={() => move(stepT(stops, t, -1, span))} disabled={flat || t <= span.startMs}>
            Previous event
          </button>
          <button type="button" className="btn" onClick={play} disabled={flat} aria-pressed={playing}>
            {playing ? "Pause" : "Play"}
          </button>
          <button type="button" className="btn" onClick={() => move(stepT(stops, t, 1, span))} disabled={flat || t >= span.endMs}>
            Next event
          </button>
          <button
            type="button"
            className="btn"
            onClick={() => {
              setPlaying(false);
              setReplayT(null);
            }}
            disabled={replayT === null}
          >
            Show current state
          </button>
        </div>
        <p id={hintId} className="muted small">
          Arrow keys step between events, Page Up/Down move 10% of the span, Home/End jump to the start or end.
        </p>
      </>
    );
  }

  return (
    <Panel title="Replay" area="replay">
      {body}
    </Panel>
  );
}

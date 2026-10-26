import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { ApiError, api, request } from "../../api/client";
import { formatTime } from "../../format";
import { dashboard } from "../../store/store";
import { ViewHeader } from "../common/ViewHeader";
import {
  KIND_LABEL,
  LOG_CAP,
  antibodyEntries,
  eventEntry,
  mergeEntries,
  playbookEntries,
  transitionEntries,
  type LogEntry,
} from "./logModel";
import "./LiveLog.css";

/** Poll interval of the REST-only sources (Antibodies, Playbook_Actions). */
export const LOG_POLL_MS = 5_000;

type SourceState = "ok" | "unavailable";

interface LiveLogProps {
  cap?: number;
  pollMs?: number;
}

function countNew(frozen: readonly LogEntry[], entries: readonly LogEntry[]): number {
  const had = new Set(frozen.map((e) => e.id));
  return entries.reduce((n, e) => (had.has(e.id) ? n : n + 1), 0);
}

function unavailable(err: unknown): boolean {
  return err instanceof ApiError && (err.status === 404 || err.status === 503);
}

/** Live log view (Req 38.7): one timestamp-ordered stream of Transaction_Events ingested,
 *  rejected and dropped as duplicates (WebSocket events and metrics), Cases opened and updated
 *  and Feedback_Labels (WebSocket cases), and Antibodies and executed Playbook_Actions (REST).
 *  Pausing freezes the visible lines (and so the live-region announcements) while new entries
 *  keep being collected, up to ``cap`` retained lines. */
export function LiveLog({ cap = LOG_CAP, pollMs = LOG_POLL_MS }: LiveLogProps) {
  const [entries, setEntries] = useState<LogEntry[]>(() =>
    mergeEntries([], [...dashboard.getState().feed].reverse().map(eventEntry), cap), // feed is newest first
  );
  const [frozen, setFrozen] = useState<LogEntry[] | null>(null);
  const [sources, setSources] = useState<{ immune: SourceState; playbooks: SourceState }>({ immune: "ok", playbooks: "ok" });
  const listRef = useRef<HTMLDivElement>(null);

  // WebSocket-fed store: every transition becomes log entries.
  useEffect(
    () =>
      dashboard.subscribe((s, prev) => {
        const add = transitionEntries(prev, s);
        if (add.length) setEntries((e) => mergeEntries(e, add, cap));
      }),
    [cap],
  );

  // REST-only sources, polled; a 404/503 marks the feature as unavailable (Req 38.10).
  useEffect(() => {
    const ctrl = new AbortController();
    const poll = async () => {
      const [immune, playbooks] = await Promise.allSettled([
        api.getImmune(ctrl.signal),
        request<unknown>("/soc/playbook-actions", { signal: ctrl.signal }),
      ]);
      if (ctrl.signal.aborted) return;
      const add = [
        ...(immune.status === "fulfilled" ? antibodyEntries(immune.value) : []),
        ...(playbooks.status === "fulfilled" ? playbookEntries(playbooks.value) : []),
      ];
      if (add.length) setEntries((e) => mergeEntries(e, add, cap));
      setSources({
        immune: immune.status === "rejected" && unavailable(immune.reason) ? "unavailable" : "ok",
        playbooks: playbooks.status === "rejected" && unavailable(playbooks.reason) ? "unavailable" : "ok",
      });
    };
    void poll();
    const timer = setInterval(() => void poll(), pollMs);
    return () => {
      clearInterval(timer);
      ctrl.abort();
    };
  }, [cap, pollMs]);

  const shown = frozen ?? entries;
  const held = frozen ? countNew(frozen, entries) : 0;

  useLayoutEffect(() => {
    const el = listRef.current;
    if (el && !frozen) el.scrollTop = el.scrollHeight;
  }, [shown, frozen]);

  const togglePause = () => setFrozen((f) => (f ? null : entries));
  const clear = () => {
    setEntries([]);
    setFrozen((f) => (f ? [] : null));
  };

  const notes = [
    sources.immune === "unavailable" ? "Antibody entries: the Immune_System is not enabled on this server." : null,
    sources.playbooks === "unavailable" ? "Playbook_Action entries: SOC playbooks are not enabled on this server." : null,
  ].filter((x): x is string => x !== null);

  return (
    <div className="hx-view livelog" role="region" aria-labelledby="livelog-title">
      <ViewHeader
        id="livelog-title"
        eyebrow="Activity"
        title="Live log"
        sub="Everything HELIX does, as it happens: transactions in, cases opened, labels, antibodies and playbook actions."
      />
      <section className="livelog__card" aria-label="Live log stream">
        <div className="livelog__toolbar">
          <div className="livelog__status">
            <span className={`livelog__dot${frozen ? " livelog__dot--paused" : ""}`} aria-hidden="true" />
            <span className="livelog__state">{frozen ? "Paused" : "Streaming"}</span>
            <p className="livelog__meta" data-testid="livelog-meta">
              {shown.length} line{shown.length === 1 ? "" : "s"} (newest {cap} kept)
              {frozen ? `. Paused: ${held} new entr${held === 1 ? "y" : "ies"} held.` : ""}
            </p>
            {frozen ? (
              <span className="hx-chip livelog__held" aria-hidden="true">
                {held} held
              </span>
            ) : null}
          </div>
          <div className="livelog__actions">
            <button
              type="button"
              className={`hx-pill hx-pill--sm${frozen ? " hx-pill--primary" : ""}`}
              aria-pressed={frozen !== null}
              onClick={togglePause}
            >
              {frozen ? "Resume" : "Pause"}
            </button>
            <button type="button" className="hx-pill hx-pill--sm" onClick={clear}>
              Clear
            </button>
          </div>
        </div>
        {notes.map((n) => (
          <p key={n} className="livelog__note" data-testid="livelog-unavailable">
            {n}
          </p>
        ))}
        <div
          ref={listRef}
          className="livelog__scroll"
          role="log"
          aria-live="polite"
          aria-label="Live log entries"
          tabIndex={0}
        >
          {shown.length ? (
            <ol className="livelog__list">
              {shown.map((e) => (
                <li key={e.id} className={`livelog__line livelog__line--${e.kind}`}>
                  <time dateTime={e.ts} className="livelog__time">
                    {formatTime(e.ts)}
                  </time>{" "}
                  <span className="livelog__kind">{KIND_LABEL[e.kind]}</span> <span className="livelog__text">{e.text}</span>
                </li>
              ))}
            </ol>
          ) : (
            <p className="livelog__empty">No entries yet. They appear as the backend processes events.</p>
          )}
        </div>
      </section>
    </div>
  );
}

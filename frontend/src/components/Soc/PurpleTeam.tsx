import { useId, useState } from "react";
import { PATTERN_TYPES, socApi, type ExerciseSpec, type PatternTypeName, type ScorecardDoc } from "../../api/soc";
import { formatTime } from "../../format";
import { Message, SocSection, formatDuration, pct } from "./common";
import { useAction, useResource } from "./useResource";

/** Default exercise: one campaign of each of three common patterns, a little background traffic. */
export const DEFAULT_PATTERNS: PatternTypeName[] = ["FAN_OUT", "STRUCTURING", "MULE_CHAIN"];

export function exerciseSpec(patterns: readonly PatternTypeName[], background: number): ExerciseSpec {
  return {
    // The server caps the name at 120 characters.
    name: `Dashboard exercise: ${patterns.length} pattern${patterns.length === 1 ? "" : "s"}`,
    campaigns: patterns.map((ptype, i) => ({ ptype, seed: i + 1, count: 1 })),
    background_events: background,
  };
}

function Scorecard({ card }: { card: ScorecardDoc }) {
  const m = card.metrics;
  return (
    <div className="soc-sub" data-testid="scorecard">
      <h3 className="ev-section__title">
        Scorecard <span className="mono">{card.scorecard_id}</span> · {card.name}
      </h3>
      <dl className="kv">
        <div>
          <dt>Detection rate</dt>
          <dd>
            {pct(m.detection_rate)} of events ({m.detected_events}/{m.events})
          </dd>
        </div>
        <div>
          <dt>Campaigns caught</dt>
          <dd>
            {m.detected_campaigns} of {m.campaigns} ({pct(m.campaign_detection_rate)})
          </dd>
        </div>
        <div>
          <dt>Median detection latency</dt>
          <dd>{formatDuration(m.median_detection_latency_s)}</dd>
        </div>
        <div>
          <dt>Alert volume</dt>
          <dd>{m.alert_volume}</dd>
        </div>
        <div>
          <dt>False positives</dt>
          <dd>{m.false_positives}</dd>
        </div>
      </dl>
      {card.campaigns.length ? (
        <ul className="ev-list small" aria-label="Campaign results">
          {card.campaigns.map((c) => (
            <li key={c.campaign_id}>
              <span className="mono">{c.campaign_id}</span> {c.ptype}: {c.detected ? "detected" : "MISSED"} ({c.detected_events}/{c.events} events)
            </li>
          ))}
        </ul>
      ) : null}
      {card.notes?.length ? <p className="muted small">{card.notes.join(" ")}</p> : null}
    </div>
  );
}

function RunForm({ onDone }: { onDone: (card: ScorecardDoc) => void }) {
  const [patterns, setPatterns] = useState<PatternTypeName[]>(DEFAULT_PATTERNS);
  const [background, setBackground] = useState(200);
  const action = useAction();
  const bgId = useId();
  const groupId = useId();
  const toggle = (p: PatternTypeName) =>
    setPatterns((cur) => (cur.includes(p) ? cur.filter((x) => x !== p) : PATTERN_TYPES.filter((x) => x === p || cur.includes(x))));
  const run = () => {
    if (patterns.length === 0) return action.setMsg({ kind: "error", text: "Pick at least one attack pattern." });
    void action.run(async () => {
      const card = await socApi.runExercise(exerciseSpec(patterns, background));
      onDone(card);
      return `Exercise finished: scorecard ${card.scorecard_id}.`;
    });
  };
  return (
    <div className="soc-sub">
      <fieldset className="soc-fieldset" aria-describedby={groupId}>
        <legend>Attack patterns</legend>
        <p id={groupId} className="muted small">
          Runs against an isolated copy of HELIX; live cases, weights and antibodies are not touched.
        </p>
        {PATTERN_TYPES.map((p) => (
          <span key={p} className="soc-check">
            <input id={`${groupId}-${p}`} type="checkbox" checked={patterns.includes(p)} onChange={() => toggle(p)} />
            <label htmlFor={`${groupId}-${p}`}>{p}</label>
          </span>
        ))}
      </fieldset>
      <div className="field">
        <label htmlFor={bgId}>Background (legitimate) events</label>
        <input
          id={bgId}
          type="number"
          min={0}
          max={5000}
          step={50}
          value={background}
          onChange={(e) => setBackground(Math.max(0, Math.min(5000, Math.round(Number(e.target.value) || 0))))}
        />
      </div>
      <button type="button" className="btn btn--primary" disabled={action.pending} aria-busy={action.pending || undefined} onClick={run}>
        {action.pending ? "Running exercise…" : "Run purple-team exercise"}
      </button>
      <Message msg={action.msg} />
    </div>
  );
}

/** Purple_Team_Exercise (Req 37.1-37.4, 38.2): run an exercise and show its Exercise_Scorecard,
 *  plus the stored scorecards. */
export function PurpleTeamSection() {
  const r = useResource((sig) => socApi.scorecards(sig), "scorecards");
  const [card, setCard] = useState<ScorecardDoc | null>(null);
  return (
    <SocSection
      title="Purple team"
      area="soc-purple"
      explain="Fire known attacks at a copy of HELIX and score how many it catches, how fast, and how many false alarms it raises."
      resource={r}
      footer={
        <>
          <RunForm
            onDone={(c) => {
              setCard(c);
              r.reload();
            }}
          />
          {card ? <Scorecard card={card} /> : null}
        </>
      }
    >
      {(doc) =>
        doc.scorecards.length === 0 ? (
          <p className="empty">No exercises have been run yet.</p>
        ) : (
          <ul className="ev-list small" aria-label="Stored scorecards" data-testid="scorecards">
            {doc.scorecards.map((s) => (
              <li key={s.scorecard_id}>
                <span className="mono">{s.scorecard_id}</span> {s.name} · {formatTime(s.created_at)} · detection {pct(s.metrics.detection_rate)} ·
                false positives {s.metrics.false_positives}
              </li>
            ))}
          </ul>
        )
      }
    </SocSection>
  );
}

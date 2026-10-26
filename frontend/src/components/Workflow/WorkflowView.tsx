import { lazy, Suspense, useEffect, useId, useMemo, useState, type ReactNode } from "react";
import type { Tier } from "../../api/types";
import { useDashboard } from "../../store/store";
import { hasWebGL } from "../../viz/webgl";
import { ErrorBoundary } from "../common/ErrorBoundary";
import { ViewHeader } from "../common/ViewHeader";
import { STAGES, type StageId } from "./layout";
import { readPalette } from "./palette";
import {
  LOOP_S,
  SPEEDS,
  STAGE_INFO,
  STORY_STEPS,
  overlayNoteAt,
  seekTimeFor,
  stageIndex,
  viewDefaults,
  type Overlays,
  type Speed,
} from "./storyScript";
import { useSimDriver, type SimMode } from "./useSimDriver";
import "./workflow.css";

// three.js, @react-three/fiber and drei live in this chunk, fetched when the tab first opens.
const WorkflowScene = lazy(() => import("./WorkflowScene"));

export type CameraPreset = "overview" | "core" | "investigator";
export const CAMERA_PRESETS: ReadonlyArray<{ id: CameraPreset; label: string }> = [
  { id: "overview", label: "Overview" },
  { id: "core", label: "HELIX core" },
  { id: "investigator", label: "Investigator" },
];

export const SCENE_UNAVAILABLE = "3D view unavailable: showing the text view";
export const LIVE_UNAVAILABLE = "Live data unavailable: showing the story";

const SCENE_LABEL =
  "Animated 3D simulation of HELIX. Customers on the left send transactions as particles into the HELIX core, " +
  "which has six stage rings: sense, perceive, remember, reason, act and learn. Legit payments leave to receivers on the right; " +
  "suspicious ones become Case cards that fly to the investigator, whose label sends a learning pulse back to the core. " +
  "The caption panel and Text view describe the same steps in words.";

function prefersReducedMotion(): boolean {
  try {
    return typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  } catch {
    return false;
  }
}

const speedLabel = (s: Speed) => `${s}×`;
const tierText = (tier: Tier, risk: number | null) => `${tier}${risk !== null ? `, risk ${risk.toFixed(risk >= 0.95 ? 3 : 2)}` : ""}`;

/** Reports a scene failure to the view once, then renders nothing. */
function SceneFailed({ onFail }: { onFail: () => void }) {
  useEffect(onFail, [onFail]);
  return null;
}

function ToggleGroup({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="wf-group" role="group" aria-label={label}>
      <span className="wf-group__label" aria-hidden="true">
        {label}
      </span>
      <div className="wf-seg">{children}</div>
    </div>
  );
}

function StageIcon({ i }: { i: number }) {
  return (
    <svg className="wf-icon" viewBox="0 0 20 20" aria-hidden="true" focusable="false">
      <circle cx="10" cy="10" r="7.5" className="wf-icon__ring" />
      <text x="10" y="13.5" textAnchor="middle" className="wf-icon__num">
        {i + 1}
      </text>
    </svg>
  );
}

type Shape = "legit" | "fraud" | "case" | "attack" | "shield" | "pulse";
function ShapeIcon({ shape }: { shape: Shape }) {
  const body = {
    legit: <circle cx="10" cy="10" r="5.5" className="wf-shape wf-shape--legit" />,
    fraud: <polygon points="10,1.5 12,8 18.5,10 12,12 10,18.5 8,12 1.5,10 8,8" className="wf-shape wf-shape--fraud" />,
    case: <rect x="2.5" y="5" width="15" height="10" rx="1.5" className="wf-shape wf-shape--case" />,
    attack: <polygon points="10,1.5 15,10 10,18.5 5,10" className="wf-shape wf-shape--attack" />,
    shield: <polygon points="10,2 17,6 17,14 10,18 3,14 3,6" className="wf-shape wf-shape--shield" />,
    pulse: <circle cx="10" cy="10" r="4" className="wf-shape wf-shape--pulse" />,
  }[shape];
  return (
    <svg className="wf-icon" viewBox="0 0 20 20" aria-hidden="true" focusable="false">
      {body}
    </svg>
  );
}

const SHAPES: ReadonlyArray<{ shape: Shape; text: string }> = [
  { shape: "legit", text: "Round dot: a transaction (cyan in the core, green with a ✓ when it reaches a receiver as legit)" },
  { shape: "fraud", text: "Spiked shape: a suspicious transaction (red)" },
  { shape: "case", text: "Card: a Case with its id and tier; it flips to show the investigator's label" },
  { shape: "pulse", text: "Violet pulse: learning from a label, from the investigator through LEARN to PERCEIVE" },
  { shape: "attack", text: "Tall diamond labelled attack: traffic injected by the attacker (Red vs Blue overlay)" },
  { shape: "shield", text: "Hexagon: an Antibody shield on the PERCEIVE ring (Red vs Blue overlay)" },
];

function Legend() {
  const id = useId();
  return (
    <div className="wf-legend">
      <div className="wf-legend__col">
        <h3 id={`${id}-stages`} className="wf-legend__title">
          Stages
        </h3>
        <ol className="wf-legend__list" aria-labelledby={`${id}-stages`}>
          {STAGES.map((s, i) => (
            <li key={s}>
              <StageIcon i={i} /> <span>
                <strong>{s}</strong>: {STAGE_INFO[s]}
              </span>
            </li>
          ))}
        </ol>
      </div>
      <div className="wf-legend__col">
        <h3 id={`${id}-shapes`} className="wf-legend__title">
          Shapes
        </h3>
        <ul className="wf-legend__list" aria-labelledby={`${id}-shapes`}>
          {SHAPES.map((s) => (
            <li key={s.shape}>
              <ShapeIcon shape={s.shape} /> <span>{s.text}</span>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

function TextView({ current }: { current: number | null }) {
  const id = useId();
  return (
    <div className="wf-textview" data-testid="wf-textview">
      <h3 id={id} className="wf-legend__title">
        The story, step by step ({Math.round(LOOP_S)} s loop at 1×)
      </h3>
      <ol className="wf-steps" aria-labelledby={id}>
        {STORY_STEPS.map((s, i) => (
          <li key={i} aria-current={current === i ? "step" : undefined} className={current === i ? "wf-steps__now" : undefined}>
            <p className="wf-steps__head">
              <span className="wf-steps__stage">{s.stage}</span> <strong>{s.title}</strong>{" "}
              <span className="muted">({s.t} s)</span>
              {current === i ? <span className="wf-steps__mark"> (current step)</span> : null}
            </p>
            <p className="wf-steps__text">{s.text}</p>
          </li>
        ))}
      </ol>
    </div>
  );
}

/** Workflow tab (Req 43): an animated 3D simulation of the whole HELIX loop with captions,
 *  a text view, Story_Mode (scripted, offline) and Live_Mode (driven by the WebSocket store). */
export function WorkflowView() {
  const [reducedMotion] = useState(prefersReducedMotion);
  const defaults = useMemo(() => viewDefaults(reducedMotion), [reducedMotion]);
  const [webgl] = useState(hasWebGL);
  const [sceneFailed, setSceneFailed] = useState(false);
  const [mode, setMode] = useState<SimMode>(defaults.mode);
  const [playing, setPlaying] = useState(defaults.playing);
  const [speed, setSpeed] = useState<Speed>(defaults.speed);
  const [overlays, setOverlays] = useState<Overlays>({ redBlue: false, soc: false });
  const [camera, setCamera] = useState<{ preset: CameraPreset; nonce: number }>({ preset: "overview", nonce: 0 });
  const [textView, setTextView] = useState(false);
  const palette = useMemo(() => readPalette(), []);
  const connection = useDashboard((s) => s.connection);

  const liveOpen = connection === "open";
  const effectiveMode: SimMode = mode === "live" && liveOpen ? "live" : "story";
  const { driver, stepIdx, stats, seek, restart } = useSimDriver({ playing, speed, mode: effectiveMode, overlays });

  // Reduced motion opens paused on a settled first step, so the still frame shows something.
  useEffect(() => {
    if (!defaults.playing) driver.seek(seekTimeFor(0));
  }, [driver, defaults.playing]);

  const sceneOk = webgl && !sceneFailed;
  const showText = textView || !sceneOk;
  const story = effectiveMode === "story";
  const step = STORY_STEPS[stepIdx]!;
  // Overlay note: the latest one that happens by the end of this step, if recent.
  const stepEnd = (STORY_STEPS[stepIdx + 1]?.t ?? LOOP_S) - 0.01;
  const latestNote = story ? overlayNoteAt(stepEnd, overlays) : null;
  const note = latestNote && stepEnd - latestNote.t <= 12 ? latestNote : null;
  const liveStage: StageId = STAGES[stats.focusStage ?? 0]!;
  const stage: StageId = story ? step.stage : liveStage;

  const announce = story
    ? `Step ${stepIdx + 1} of ${STORY_STEPS.length}, ${step.stage}: ${step.title}. ${step.text}`
    : stats.latestCase
      ? `Live: Case ${stats.latestCase.id}, ${stats.latestCase.tier}${stats.latestCase.label ? `, labelled ${stats.latestCase.label === "CONFIRMED_FRAUD" ? "confirmed fraud" : "false positive"}` : ""}.`
      : "Live: waiting for transactions.";

  const toggleOverlay = (k: keyof Overlays) => setOverlays((o) => ({ ...o, [k]: !o[k] }));

  return (
    <section className="hx-view wf" aria-labelledby="wf-title">
      <ViewHeader
        id="wf-title"
        eyebrow="How it works"
        title="Workflow"
        sub="How HELIX handles real payments, from a customer to the investigator's label and back."
      />

      <div className="wf-toolbar">
        <ToggleGroup label="Playback">
          <button type="button" className="btn btn--small" onClick={() => setPlaying((p) => !p)}>
            {playing ? "Pause" : "Play"}
          </button>
          <button type="button" className="btn btn--small" onClick={restart}>
            Restart
          </button>
          {story ? (
            <>
              <button type="button" className="btn btn--small" onClick={() => seek(Math.max(0, stepIdx - 1))} disabled={stepIdx === 0}>
                Previous step
              </button>
              <button type="button" className="btn btn--small" onClick={() => seek((stepIdx + 1) % STORY_STEPS.length)}>
                Next step
              </button>
            </>
          ) : null}
        </ToggleGroup>
        <ToggleGroup label="Speed">
          {SPEEDS.map((s) => (
            <button key={s} type="button" className="btn btn--small" aria-pressed={speed === s} onClick={() => setSpeed(s)}>
              {speedLabel(s)}
            </button>
          ))}
        </ToggleGroup>
        <ToggleGroup label="Mode">
          <button type="button" className="btn btn--small" aria-pressed={mode === "story"} onClick={() => setMode("story")}>
            Story
          </button>
          <button type="button" className="btn btn--small" aria-pressed={mode === "live"} onClick={() => setMode("live")}>
            Live
          </button>
        </ToggleGroup>
        <ToggleGroup label="Overlays">
          <button type="button" className="btn btn--small" aria-pressed={overlays.redBlue} onClick={() => toggleOverlay("redBlue")}>
            Red vs Blue
          </button>
          <button type="button" className="btn btn--small" aria-pressed={overlays.soc} onClick={() => toggleOverlay("soc")}>
            SOC
          </button>
        </ToggleGroup>
        <ToggleGroup label="Camera">
          {CAMERA_PRESETS.map((c) => (
            <button
              key={c.id}
              type="button"
              className="btn btn--small"
              aria-pressed={camera.preset === c.id}
              disabled={!sceneOk || textView}
              onClick={() => setCamera((v) => ({ preset: c.id, nonce: v.nonce + 1 }))}
            >
              {c.label}
            </button>
          ))}
        </ToggleGroup>
        <ToggleGroup label="View">
          <button type="button" className="btn btn--small" aria-pressed={showText} disabled={!sceneOk} onClick={() => setTextView((v) => !v)}>
            Text view
          </button>
        </ToggleGroup>
      </div>

      {mode === "live" && !liveOpen ? (
        <p className="wf-note" role="status" data-testid="wf-live-unavailable">
          {LIVE_UNAVAILABLE}
        </p>
      ) : null}
      {!sceneOk ? (
        <p className="wf-note" role="status" data-testid="wf-scene-unavailable">
          {SCENE_UNAVAILABLE}
        </p>
      ) : null}

      <div className="wf-body">
        <div className="wf-main">
          {showText ? (
            <TextView current={story ? stepIdx : null} />
          ) : (
            <div className="wf-stage" role="img" aria-label={SCENE_LABEL} data-testid="wf-stage">
              <ErrorBoundary fallback={() => <SceneFailed onFail={() => setSceneFailed(true)} />}>
                <Suspense fallback={<p className="muted wf-loading">Loading 3D view…</p>}>
                  <WorkflowScene
                    sim={driver}
                    overlays={overlays}
                    camera={camera}
                    reducedMotion={reducedMotion}
                    palette={palette}
                    highlight={story ? stageIndex(step.stage) : null}
                  />
                </Suspense>
              </ErrorBoundary>
            </div>
          )}
        </div>

        <section className="wf-caption" aria-labelledby="wf-caption-title">
          <h3 id="wf-caption-title" className="wf-caption__title">
            Now
          </h3>
          {/* Announced to screen readers only: the same facts are shown below, so a visible
              copy would repeat the step text twice in the panel. */}
          <p className="sr-only" aria-live="polite" data-testid="wf-announce">
            {announce}
          </p>
          <dl className="wf-caption__kv">
            <dt>Stage</dt>
            <dd data-testid="wf-stage-name">
              <StageIcon i={stageIndex(stage)} /> <strong>{stage}</strong>: {STAGE_INFO[stage]}
            </dd>
            {story ? (
              <>
                <dt>Step</dt>
                <dd>
                  {stepIdx + 1} of {STORY_STEPS.length}: {step.title}
                </dd>
                {step.tx ? (
                  <>
                    <dt>Transaction</dt>
                    <dd className="mono">{step.tx}</dd>
                  </>
                ) : null}
                {step.case ? (
                  <>
                    <dt>Case</dt>
                    <dd>
                      <span className="mono">{step.case.id}</span>, {tierText(step.case.tier, step.case.risk)}
                    </dd>
                  </>
                ) : null}
              </>
            ) : (
              <>
                <dt>Transaction</dt>
                <dd>
                  {stats.focus ? (
                    <>
                      <span className="mono">{stats.focus.txId ?? "?"}</span>, risk {stats.focus.risk.toFixed(2)},{" "}
                      {stats.focus.outcome === "suspicious" ? "suspicious path" : "legit path"}
                    </>
                  ) : (
                    "none in flight"
                  )}
                </dd>
                <dt>Case</dt>
                <dd>
                  {stats.latestCase ? (
                    <>
                      <span className="mono">{stats.latestCase.id}</span>, {tierText(stats.latestCase.tier, stats.latestCase.risk)}
                    </>
                  ) : (
                    "none yet"
                  )}
                </dd>
                <dt>Particles</dt>
                <dd data-testid="wf-live-stats">
                  {stats.active} in flight, {stats.drawn} frames drawn, {stats.dropped} frames not drawn (at most 20 per second)
                </dd>
              </>
            )}
          </dl>
          {story ? <p className="wf-caption__text">{step.text}</p> : null}
          {note ? (
            <p className="wf-caption__note" data-testid="wf-overlay-note">
              <strong>{note.overlay === "redBlue" ? "Red vs Blue" : "SOC"}:</strong> {note.text}
            </p>
          ) : null}
        </section>
      </div>
      <Legend />
    </section>
  );
}

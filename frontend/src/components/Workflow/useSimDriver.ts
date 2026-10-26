// Simulation clock of the Workflow view. It runs outside the 3D scene, so the captions keep
// working without WebGL, and it stops whenever the view is paused, unmounted (tab not
// selected) or the document is hidden (Req 43.15). The scene subscribes and redraws on ticks.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { dashboard } from "../../store/store";
import { adaptLive, initialLiveState, type LiveAdapterState, type LiveInput } from "./liveAdapter";
import { initialSim, stageOf, step, type Particle, type SimCase, type SimState } from "./simModel";
import { NO_OVERLAYS, advanceStory, seekTimeFor, simulateStory, stepIndexAt, type Overlays } from "./storyScript";

export type SimMode = "story" | "live";

/** What the scene reads each frame. */
export interface SimHandle {
  get(): SimState;
  /** Seconds the simulation clock has run (for camera drift and idle animation). */
  time(): number;
  subscribe(fn: () => void): () => void;
}

export class SimDriver implements SimHandle {
  state: SimState = initialSim();
  /** Story time within the loop. */
  t = 0;
  elapsed = 0;
  live: LiveAdapterState = initialLiveState();
  mode: SimMode = "story";
  overlays: Overlays = NO_OVERLAYS;
  private subs = new Set<() => void>();

  get = () => this.state;
  time = () => this.elapsed;
  subscribe = (fn: () => void) => {
    this.subs.add(fn);
    return () => {
      this.subs.delete(fn);
    };
  };
  notify() {
    for (const f of this.subs) f();
  }

  setMode(m: SimMode) {
    if (m === this.mode) return;
    this.mode = m;
    this.reset();
  }

  reset() {
    this.state = initialSim();
    this.t = 0;
    this.live = initialLiveState();
    this.notify();
  }

  seek(t: number) {
    this.state = simulateStory(t, this.overlays);
    this.t = t;
    this.notify();
  }

  tick(dt: number, readLive: () => Omit<LiveInput, "active">) {
    this.elapsed += dt;
    if (this.mode === "story") {
      const r = advanceStory(this.state, this.t, dt, this.overlays);
      this.state = r.state;
      this.t = r.t;
    } else {
      const out = adaptLive(this.live, { ...readLive(), active: this.state.particles.length });
      this.live = out.state;
      this.state = step(this.state, dt, out.events);
    }
    this.notify();
  }
}

export interface LiveStats {
  active: number;
  drawn: number;
  dropped: number;
  focus: Particle | null;
  focusStage: number | null;
  latestCase: SimCase | null;
}

const EMPTY_STATS: LiveStats = { active: 0, drawn: 0, dropped: 0, focus: null, focusStage: null, latestCase: null };

function weightsOf(v: unknown): Record<string, number> | null {
  if (typeof v !== "object" || v === null) return null;
  const out: Record<string, number> = {};
  for (const [k, x] of Object.entries(v)) if (typeof x === "number" && Number.isFinite(x)) out[k] = x;
  return Object.keys(out).length ? out : null;
}

export function readLiveStore(): Omit<LiveInput, "active"> {
  const s = dashboard.getState();
  return { feed: s.feed, cases: s.cases, weights: weightsOf(s.metrics?.weights), now: Date.now() };
}

function statsOf(d: SimDriver): LiveStats {
  const ps = d.state.particles;
  let focus: Particle | null = null;
  for (const p of ps) if (p.kind === "tx" && (!focus || (p.outcome === "suspicious" && focus.outcome !== "suspicious") || (p.outcome === focus.outcome && p.id > focus.id))) focus = p;
  const latestCase = d.state.cases.reduce<SimCase | null>((m, c) => (!m || c.seq > m.seq ? c : m), null);
  return { active: ps.length, drawn: d.live.drawn, dropped: d.live.dropped, focus, focusStage: focus ? stageOf(focus) : null, latestCase };
}

const sameStats = (a: LiveStats, b: LiveStats) =>
  a.active === b.active &&
  a.drawn === b.drawn &&
  a.dropped === b.dropped &&
  a.focus?.id === b.focus?.id &&
  a.focusStage === b.focusStage &&
  a.latestCase?.id === b.latestCase?.id &&
  a.latestCase?.phase === b.latestCase?.phase &&
  a.latestCase?.tier === b.latestCase?.tier;

interface DriverOptions {
  playing: boolean;
  speed: number;
  mode: SimMode;
  overlays: Overlays;
  readLive?: () => Omit<LiveInput, "active">;
}

export function useSimDriver({ playing, speed, mode, overlays, readLive = readLiveStore }: DriverOptions) {
  const driver = useMemo(() => new SimDriver(), []);
  const [stepIdx, setStepIdx] = useState(0);
  const [stats, setStats] = useState<LiveStats>(EMPTY_STATS);
  const stepRef = useRef(0);
  const speedRef = useRef(speed);
  speedRef.current = speed;
  const readRef = useRef(readLive);
  readRef.current = readLive;
  driver.overlays = overlays;

  const syncStep = useCallback(() => {
    const i = stepIndexAt(driver.t);
    if (i !== stepRef.current) {
      stepRef.current = i;
      setStepIdx(i);
    }
  }, [driver]);

  useEffect(() => {
    driver.setMode(mode);
    stepRef.current = 0;
    setStepIdx(0);
    setStats(EMPTY_STATS);
  }, [driver, mode]);

  useEffect(() => {
    if (!playing) return;
    const raf: (cb: (t: number) => void) => number =
      typeof window.requestAnimationFrame === "function"
        ? (cb) => window.requestAnimationFrame(cb)
        : (cb) => window.setTimeout(() => cb(performance.now()), 16);
    const cancel: (h: number) => void =
      typeof window.cancelAnimationFrame === "function" ? (h) => window.cancelAnimationFrame(h) : (h) => window.clearTimeout(h);
    let handle: number | null = null;
    let last = performance.now();
    let lastStats = 0;
    const loop = (now: number) => {
      handle = null;
      if (document.hidden) return; // resumed by visibilitychange
      const dt = Math.min(0.1, Math.max(0, (now - last) / 1000));
      last = now;
      driver.tick(dt * speedRef.current, readRef.current);
      if (driver.mode === "story") syncStep();
      if (now - lastStats > 250) {
        lastStats = now;
        const next = statsOf(driver);
        setStats((prev) => (sameStats(prev, next) ? prev : next));
      }
      handle = raf(loop);
    };
    const onVisible = () => {
      if (!document.hidden && handle === null) {
        last = performance.now();
        handle = raf(loop);
      }
    };
    document.addEventListener("visibilitychange", onVisible);
    handle = raf(loop);
    return () => {
      document.removeEventListener("visibilitychange", onVisible);
      if (handle !== null) cancel(handle);
    };
  }, [playing, driver, syncStep]);

  const seek = useCallback(
    (i: number) => {
      driver.seek(seekTimeFor(i));
      stepRef.current = i;
      setStepIdx(i);
    },
    [driver],
  );

  const restart = useCallback(() => {
    driver.reset();
    stepRef.current = 0;
    setStepIdx(0);
    setStats(EMPTY_STATS);
  }, [driver]);

  return { driver, stepIdx, stats, seek, restart };
}

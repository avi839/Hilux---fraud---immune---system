import { useEffect, useState } from "react";
import { HelixLogo } from "./Logo";

/** sessionStorage key: the splash plays once per browser session. */
export const SPLASH_KEY = "helix:splash-seen";
/** When the overlay starts fading out (and stops taking pointer events), in ms. */
export const SPLASH_FADE_AT = 1150;
/** When the overlay is removed from the DOM, in ms. */
export const SPLASH_DONE_AT = 1800;

const reducedMotion = () => typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function seen(): boolean {
  try {
    return window.sessionStorage.getItem(SPLASH_KEY) === "1";
  } catch {
    return false; // storage blocked: still play, it is harmless
  }
}

function markSeen(): void {
  try {
    window.sessionStorage.setItem(SPLASH_KEY, "1");
  } catch {
    // storage blocked: nothing to remember
  }
}

/** Decide once, at first render, whether the splash plays. */
export function shouldPlaySplash(enabled: boolean): boolean {
  return enabled && typeof window !== "undefined" && !reducedMotion() && !seen();
}

interface SplashProps {
  /** Off under Vitest so tests never see the overlay; on everywhere else. */
  enabled?: boolean;
}

/** First-load overlay: the mark draws itself, the wordmark fades up, then the whole overlay
 *  fades away (≈1.8 s). The app renders underneath from the start; the overlay is decorative
 *  (aria-hidden), so live regions and focus are unaffected, and it stops taking pointer
 *  events as soon as it starts fading. Skipped under prefers-reduced-motion and after the
 *  first time in a browser session. */
export function Splash({ enabled = import.meta.env.MODE !== "test" }: SplashProps) {
  const [phase, setPhase] = useState<"in" | "out" | "done">(() => (shouldPlaySplash(enabled) ? "in" : "done"));

  useEffect(() => {
    if (phase === "done") return;
    markSeen();
    const fade = window.setTimeout(() => setPhase("out"), SPLASH_FADE_AT);
    const done = window.setTimeout(() => setPhase("done"), SPLASH_DONE_AT);
    return () => {
      window.clearTimeout(fade);
      window.clearTimeout(done);
    };
    // Runs once on mount: the timers drive the phases from here.
  }, []);

  if (phase === "done") return null;
  return (
    <div className={`hx-splash${phase === "out" ? " hx-splash--out" : ""}`} aria-hidden="true" data-testid="splash">
      <div className="hx-starfield hx-splash__stars" />
      <div className="hx-spotlight hx-splash__spot" />
      <div className="hx-splash__center">
        <HelixLogo size={88} animated />
        <span className="hx-splash__word">HELIX</span>
      </div>
    </div>
  );
}

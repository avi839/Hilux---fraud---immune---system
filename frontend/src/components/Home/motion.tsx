// Motion helpers for the Home page: scroll reveal, pointer parallax, reduced-motion checks.
// Everything degrades to "content shown, nothing moves" when IntersectionObserver or
// matchMedia is missing (jsdom) or the user prefers reduced motion.

import { useEffect, useRef, useState, type CSSProperties, type ElementType, type ReactNode, type RefObject } from "react";

function mediaMatches(query: string): boolean {
  return typeof window !== "undefined" && typeof window.matchMedia === "function" && window.matchMedia(query).matches;
}

export const prefersReducedMotion = (): boolean => mediaMatches("(prefers-reduced-motion: reduce)");

/** True when scroll reveal can run; otherwise content renders already revealed. */
export function canReveal(): boolean {
  return typeof window !== "undefined" && typeof window.IntersectionObserver === "function" && !prefersReducedMotion();
}

interface RevealProps {
  as?: ElementType;
  className?: string;
  /** Stagger step (multiplied by 70 ms in home.css). */
  step?: number;
  children: ReactNode;
  [attr: string]: unknown;
}

/** Wraps content in `.hx-reveal`; adds `.is-in` once it scrolls into view. The class lives in
 *  React state, so later re-renders never drop it. */
export function Reveal({ as: Tag = "div", className = "", step = 0, children, ...rest }: RevealProps) {
  const ref = useRef<HTMLElement | null>(null);
  const [shown, setShown] = useState(() => !canReveal());

  useEffect(() => {
    if (shown) return;
    const el = ref.current;
    if (!el || !canReveal()) {
      setShown(true);
      return;
    }
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          setShown(true);
          io.disconnect();
        }
      },
      { rootMargin: "0px 0px -8% 0px", threshold: 0.08 },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [shown]);

  const style = { ...(rest.style as CSSProperties | undefined), "--step": step, "--hx-reveal-delay": `${step * 70}ms` } as CSSProperties;
  delete rest.style;
  return (
    <Tag ref={ref} className={`hx-reveal${shown ? " is-in" : ""}${className ? ` ${className}` : ""}`} style={style} {...rest}>
      {children}
    </Tag>
  );
}

/** Gentle pointer tilt (at most ``maxDeg``) on ``target``, driven by pointer moves over
 *  ``host``. Only with a fine pointer and without reduced motion. Writes CSS custom
 *  properties directly so React never re-renders on pointer moves. */
export function useParallax(target: RefObject<HTMLElement | null>, host: RefObject<HTMLElement | null>, maxDeg = 4) {
  useEffect(() => {
    const el = target.current;
    const area = host.current ?? el;
    if (!el || !area) return;
    if (prefersReducedMotion() || !mediaMatches("(pointer: fine)")) return;

    let raf = 0;
    let nx = 0;
    let ny = 0;
    const apply = () => {
      raf = 0;
      el.style.setProperty("--tilt-x", `${(-ny * maxDeg).toFixed(2)}deg`);
      el.style.setProperty("--tilt-y", `${(nx * maxDeg).toFixed(2)}deg`);
    };
    const clamp = (v: number) => Math.max(-1, Math.min(1, v));
    const onMove = (e: PointerEvent) => {
      const r = el.getBoundingClientRect();
      nx = clamp((e.clientX - (r.left + r.width / 2)) / (window.innerWidth / 2));
      ny = clamp((e.clientY - (r.top + r.height / 2)) / (window.innerHeight / 2));
      if (!raf) raf = requestAnimationFrame(apply);
    };
    const onLeave = () => {
      nx = 0;
      ny = 0;
      if (!raf) raf = requestAnimationFrame(apply);
    };
    el.dataset.parallax = "on";
    area.addEventListener("pointermove", onMove);
    area.addEventListener("pointerleave", onLeave);
    return () => {
      area.removeEventListener("pointermove", onMove);
      area.removeEventListener("pointerleave", onLeave);
      if (raf) cancelAnimationFrame(raf);
      delete el.dataset.parallax;
    };
  }, [target, host, maxDeg]);
}

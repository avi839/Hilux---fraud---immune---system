import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { AA_NON_TEXT, AA_TEXT, contrastRatio, cssVariables, parseColor, type RGB } from "./contrast";

// HELIX theme (Req 42, navy rebrand): the design tokens exist with their specified values,
// and the text colours keep WCAG AA contrast on the real (translucent) surfaces, computed
// for the brightest point of the spotlight. Colour pairs only; not a WCAG conformance
// assessment.
const css = readFileSync("src/styles.css", "utf8");
const main = readFileSync("src/main.tsx", "utf8");
const vars = cssVariables(css);
const v = (name: string) => {
  const c = vars[name];
  if (!c) throw new Error(`${name} is not declared in :root of styles.css`);
  return c;
};

const TOKENS: Record<string, string> = {
  "--hx-bg": "#05060d",
  "--hx-bg-elev": "#0a0d1a",
  "--hx-surface": "rgba(148, 163, 255, 0.04)",
  "--hx-surface-hover": "rgba(148, 163, 255, 0.08)",
  "--hx-border": "rgba(170, 185, 255, 0.1)",
  "--hx-border-strong": "rgba(170, 185, 255, 0.2)",
  "--hx-line": "rgba(170, 185, 255, 0.08)",
  "--hx-text": "#e8ecff",
  "--hx-text-muted": "#9aa3c7",
  "--hx-accent": "#8fa8ff",
  "--hx-accent-2": "#c7d2fe",
  "--hx-cyan": "#7dd3fc",
  "--hx-fraud": "#f43f5e",
  "--hx-legit": "#34d399",
  "--hx-warn": "#f59e0b",
  "--hx-radius": "16px",
  "--hx-radius-sm": "10px",
  "--hx-wordmark": "linear-gradient(180deg, #f7f8ff 10%, #b9c6ff 60%, #8fa8ff)",
  "--hx-ease": "cubic-bezier(0.22, 1, 0.36, 1)",
  "--hx-dur": "420ms",
};

// Worst case for text: the page background under the spotlight with every gradient layer
// of --hx-spot at its peak at once (the cone's brightest stop plus its source glow), in the
// palest colour used. Panels then add their translucent base and brightest sheen.
const over = (fg: string, base: RGB) => parseColor(fg, base);
const spotLayers = v("--hx-spot").split(/(?=(?:conic|radial|linear)-gradient\()/);
const spotPeak = spotLayers.reduce((sum, layer) => {
  const alphas = [...layer.matchAll(/rgba\([^)]*,\s*([\d.]+)\)/g)].map((m) => Number(m[1]));
  return sum + Math.max(0, ...alphas);
}, 0);
const bg = parseColor(v("--hx-bg"));
const spotlit = over(`rgba(220, 228, 255, ${spotPeak})`, bg);
const glass = over(v("--hx-surface-hover"), over(v("--hx-glass-base"), spotlit));
const zebraHover = over("rgba(143, 168, 255, 0.07)", over("rgba(148, 163, 255, 0.025)", glass));
const selected = over("rgba(143, 168, 255, 0.13)", glass);
const header = over("rgba(5, 6, 13, 0.66)", spotlit);
const tabPill = parseColor("#e8ecff");

const SURFACES: Array<[string, RGB]> = [
  ["--hx-bg", bg],
  ["--hx-bg-elev", parseColor(v("--hx-bg-elev"))],
  ["--panel", parseColor(v("--panel"))],
  ["--panel-2", parseColor(v("--panel-2"))],
  ["page under the spotlight peak", spotlit],
  ["glass panel under the spotlight peak", glass],
  ["hovered zebra row on glass", zebraHover],
  ["selected queue item on glass", selected],
  ["header under the spotlight peak", header],
];

describe("HELIX design tokens", () => {
  it.each(Object.entries(TOKENS))("%s is %s", (name, value) => {
    expect(v(name).replace(/\s+/g, " ")).toBe(value);
  });

  it("declares the spotlight, the grid line and the font stacks", () => {
    expect(v("--hx-spot")).toMatch(/^conic-gradient\(.*rgba\(220, 228, 255/);
    expect(spotPeak).toBeGreaterThan(0);
    expect(spotPeak).toBeLessThanOrEqual(0.25);
    expect(v("--hx-font")).toMatch(/^"Inter Variable",.*system-ui/);
    expect(v("--hx-font-display")).toMatch(/^"Sora Variable", "Inter Variable",.*system-ui/);
  });

  it("has no violet left in the stylesheet", () => {
    expect(css).not.toMatch(/rgba\(139, 92, 246|rgba\(99, 102, 241|#8b5cf6|#6366f1|#a78bfa|#6d28d9|#4338ca/i);
  });

  it("re-maps the legacy palette onto the tokens", () => {
    expect(v("--bg")).toBe(v("--hx-bg"));
    expect(v("--text")).toBe(v("--hx-text"));
    expect(v("--muted")).toBe(v("--hx-text-muted"));
    expect(v("--accent")).toBe(v("--hx-accent"));
    expect(v("--border")).toBe(v("--hx-border"));
    expect(v("--border-strong")).toBe(v("--hx-border-strong"));
    expect(v("--ok")).toBe(v("--hx-legit"));
    expect(v("--warn")).toBe(v("--hx-warn"));
    expect(v("--focus")).toBe(v("--hx-accent-2"));
    expect(v("--mass-f")).toBe(v("--hx-fraud"));
    expect(v("--radius")).toBe(v("--hx-radius"));
  });

  it("--panel and --panel-2 are the opaque surfaces over --hx-bg-elev", () => {
    const elev = parseColor(v("--hx-bg-elev"));
    const close = (a: RGB, b: RGB) => a.every((x, i) => Math.abs(x - b[i]!) <= 1);
    expect(close(parseColor(v("--panel")), over(v("--hx-surface"), elev))).toBe(true);
    expect(close(parseColor(v("--panel-2")), over(v("--hx-surface-hover"), elev))).toBe(true);
  });
});

describe("theme text keeps WCAG AA contrast on the real surfaces", () => {
  const body = ["--text", "--muted"] as const;
  const status = ["--accent", "--danger", "--ok", "--warn", "--tier-critical", "--tier-high", "--tier-medium", "--error-text"] as const;

  it.each(SURFACES.flatMap(([s, rgb]) => body.map((fg) => [fg, s, rgb] as const)))("%s on %s ≥ 4.5:1", (fg, _s, rgb) => {
    expect(contrastRatio(parseColor(v(fg)), rgb)).toBeGreaterThanOrEqual(AA_TEXT);
  });

  it.each(status.map((fg) => [fg] as const))("%s on glass under the spotlight peak ≥ 4.5:1", (fg) => {
    expect(contrastRatio(parseColor(v(fg)), glass)).toBeGreaterThanOrEqual(AA_TEXT);
  });

  it("primary button text on both gradient stops ≥ 4.5:1", () => {
    expect(contrastRatio(v("--btn-primary-text"), v("--btn-primary-from"))).toBeGreaterThanOrEqual(AA_TEXT);
    expect(contrastRatio(v("--btn-primary-text"), v("--btn-primary-to"))).toBeGreaterThanOrEqual(AA_TEXT);
  });

  it("selected tab text on the light tab pill, and the pill against the header, are AA", () => {
    expect(contrastRatio(parseColor("#070912"), tabPill)).toBeGreaterThanOrEqual(AA_TEXT);
    expect(contrastRatio(tabPill, header)).toBeGreaterThanOrEqual(AA_NON_TEXT);
  });

  it("wordmark gradient stops are legible on the page and under the spotlight", () => {
    for (const stop of v("--hx-wordmark").match(/#[0-9a-f]{6}/gi) ?? []) {
      expect(contrastRatio(parseColor(stop), spotlit)).toBeGreaterThanOrEqual(AA_TEXT);
    }
  });

  it("focus ring and mass-bar segments on glass ≥ 3:1", () => {
    for (const fg of ["--focus", "--mass-f", "--mass-l"]) {
      expect(contrastRatio(parseColor(v(fg)), glass)).toBeGreaterThanOrEqual(AA_NON_TEXT);
    }
  });

  it("focus ring is visible next to the light tab pill and the primary button", () => {
    // The ring sits outside the element (outline-offset), so it is seen against the header.
    expect(contrastRatio(parseColor(v("--focus")), header)).toBeGreaterThanOrEqual(AA_NON_TEXT);
  });
});

describe("motion and assets", () => {
  it("turns every animation and transition off under prefers-reduced-motion", () => {
    const block = /@media \(prefers-reduced-motion: reduce\) \{([\s\S]*?)\n\}/.exec(css)?.[1] ?? "";
    expect(block).toMatch(/animation: none !important/);
    expect(block).toMatch(/transition: none !important/);
    expect(block).toMatch(/transform: none !important/);
    expect(block).toMatch(/\.hx-reveal \{[^}]*opacity: 1 !important/);
  });

  it("staggers panels through --i and slides the selected-tab pill", () => {
    expect(css).toMatch(/animation-delay: calc\(var\(--i, 0\) \* \d+ms\)/);
    expect(css).toMatch(/\.hx-tabs__pill \{[^}]*transform: translateX\(var\(--pill-x, 0\)\)/);
    expect(css).toMatch(/\.hx-tabs--measured \.hx-tabs__pill \{[^}]*transition:/);
  });

  it("defines the shared brand utilities", () => {
    for (const cls of [
      "hx-starfield",
      "hx-spotlight",
      "hx-grid-frame",
      "hx-eyebrow",
      "hx-wordmark",
      "hx-sub",
      "hx-card",
      "hx-pill",
      "hx-pill--primary",
      "hx-section",
      "hx-section__title",
      "hx-reveal",
      "hx-kpi",
      "hx-chip",
    ]) {
      expect(css, cls).toMatch(new RegExp(`\\.${cls} \\{`));
    }
    expect(css).toMatch(/\.hx-reveal\.is-in \{[^}]*opacity: 1/);
  });

  it("self-hosts both fonts and makes no external requests", () => {
    expect(main).toMatch(/import "@fontsource-variable\/inter";/);
    expect(main).toMatch(/import "@fontsource-variable\/sora";/);
    expect(css).not.toMatch(/@import/);
    expect(css).not.toMatch(/url\(\s*["']?(https?:)?\/\//);
  });
});

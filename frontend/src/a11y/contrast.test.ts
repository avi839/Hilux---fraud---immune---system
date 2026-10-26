import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { AA_NON_TEXT, AA_TEXT, contrastRatio, cssVariables, parseColor } from "./contrast";

// Colour pairs actually used in styles.css. Passing this test means those pairs meet the
// WCAG AA contrast ratios; it does not establish WCAG conformance of the dashboard.
// (Read from disk: with `css: false`, Vitest stubs CSS imports, including `?raw`, to "".)
// Relative to the Vitest root (frontend/); import.meta.url is not a file: URL under jsdom.
const css = readFileSync("src/styles.css", "utf8");
const vars = cssVariables(css);
const v = (name: string) => {
  const c = vars[name];
  if (!c) throw new Error(`${name} is not declared in :root of styles.css`);
  return c;
};

const SURFACES = ["--bg", "--hx-bg-elev", "--panel", "--panel-2"] as const;

/** [label, foreground, background] text pairs (4.5:1). */
const TEXT_PAIRS: Array<[string, string, string]> = [
  ...SURFACES.flatMap((s) => [
    [`text on ${s}`, v("--text"), v(s)] as [string, string, string],
    [`muted on ${s}`, v("--muted"), v(s)] as [string, string, string],
  ]),
  ...(["--panel", "--panel-2"] as const).flatMap((s) =>
    (["--accent", "--ok", "--warn", "--error-text", "--down-text", "--tier-critical", "--tier-high", "--tier-medium"] as const).map(
      (fg) => [`${fg} on ${s}`, v(fg), v(s)] as [string, string, string],
    ),
  ),
  ...(["--tier-low", "--tier-medium", "--tier-high", "--tier-critical"] as const).map(
    (t) => [`tier label on ${t}`, v("--tier-text"), v(t)] as [string, string, string],
  ),
  ["text in inputs", v("--text"), v("--input-bg")],
  ["muted in the summary box", v("--muted"), v("--input-bg")],
  ["button text", v("--text"), v("--btn-bg")],
  ["primary button text (top stop)", v("--btn-primary-text"), v("--btn-primary-from")],
  ["primary button text (bottom stop)", v("--btn-primary-text"), v("--btn-primary-to")],
  ["selected tab text on the tab pill", "#070912", "#e8ecff"],
  ["primary pill text", "#070912", "#eef1ff"],
  ["accent on --bg", v("--accent"), v("--bg")],
  ["skip link", "#111111", v("--focus")],
];

/** Non-text pairs (3:1): focus ring and the mass-bar segments against the panel surfaces. */
const NON_TEXT_PAIRS: Array<[string, string, string]> = (["--bg", "--hx-bg-elev", "--panel", "--panel-2"] as const).flatMap((s) =>
  (["--focus", "--accent", "--mass-f", "--mass-l", "--mass-t"] as const).map((fg) => [`${fg} on ${s}`, v(fg), v(s)] as [string, string, string]),
);

describe("contrast helper", () => {
  it("matches known WCAG reference values", () => {
    expect(contrastRatio("#000", "#fff")).toBeCloseTo(21, 5);
    expect(contrastRatio("#fff", "#fff")).toBeCloseTo(1, 5);
    // #767676 on white is the classic 4.54:1 AA boundary grey.
    expect(contrastRatio("#767676", "#ffffff")).toBeCloseTo(4.54, 2);
    expect(contrastRatio("#777777", "#ffffff")).toBeLessThan(AA_TEXT);
  });

  it("composites translucent colours over a base", () => {
    expect(parseColor("rgba(255, 255, 255, 0.5)", [0, 0, 0])).toEqual([127.5, 127.5, 127.5]);
    expect(() => parseColor("rgba(1, 2, 3, 0.5)")).toThrow(/base/);
  });

  it("reads the :root custom properties of styles.css", () => {
    expect(css.length).toBeGreaterThan(0);
    expect(v("--panel")).toMatch(/^#/);
  });
});

describe("palette meets WCAG AA contrast", () => {
  it.each(TEXT_PAIRS)("%s ≥ 4.5:1", (_label, fg, bg) => {
    expect(contrastRatio(fg, bg)).toBeGreaterThanOrEqual(AA_TEXT);
  });

  it.each(NON_TEXT_PAIRS)("%s ≥ 3:1", (_label, fg, bg) => {
    expect(contrastRatio(fg, bg)).toBeGreaterThanOrEqual(AA_NON_TEXT);
  });

  it("selected queue item keeps text contrast over its translucent highlight", () => {
    const panel = parseColor(v("--panel"));
    const highlight = parseColor("rgba(143, 168, 255, 0.13)", panel);
    expect(contrastRatio(parseColor(v("--text")), highlight)).toBeGreaterThanOrEqual(AA_TEXT);
    expect(contrastRatio(parseColor(v("--muted")), highlight)).toBeGreaterThanOrEqual(AA_TEXT);
  });
});

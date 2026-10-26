// WCAG 2.x relative luminance and contrast ratio, used by the palette test. This checks
// colour pairs only; it is not a WCAG conformance assessment.

export type RGB = [number, number, number];

/** Parse "#rgb", "#rrggbb" or "rgba(r, g, b, a)" (alpha composited over ``base``). */
export function parseColor(c: string, base?: RGB): RGB {
  const s = c.trim().toLowerCase();
  const hex = /^#([0-9a-f]{3}|[0-9a-f]{6})$/.exec(s);
  if (hex) {
    let h = hex[1]!;
    if (h.length === 3) h = h.replace(/./g, (x) => x + x);
    return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
  }
  const rgba = /^rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)$/.exec(s);
  if (rgba) {
    const rgb: RGB = [Number(rgba[1]), Number(rgba[2]), Number(rgba[3])];
    const a = rgba[4] === undefined ? 1 : Number(rgba[4]);
    if (a >= 1) return rgb;
    if (!base) throw new Error(`translucent colour ${c} needs a base colour`);
    return rgb.map((v, i) => v * a + base[i]! * (1 - a)) as RGB;
  }
  throw new Error(`unsupported colour: ${c}`);
}

function channel(v: number): number {
  const c = v / 255;
  return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
}

export function luminance([r, g, b]: RGB): number {
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}

export function contrastRatio(a: RGB | string, b: RGB | string): number {
  const la = luminance(typeof a === "string" ? parseColor(a) : a);
  const lb = luminance(typeof b === "string" ? parseColor(b) : b);
  const [hi, lo] = la >= lb ? [la, lb] : [lb, la];
  return (hi + 0.05) / (lo + 0.05);
}

/** Custom properties declared in the first ``:root { ... }`` block of a stylesheet. */
export function cssVariables(css: string): Record<string, string> {
  const root = /:root\s*\{([^}]*)\}/.exec(css);
  const out: Record<string, string> = {};
  if (!root) return out;
  for (const m of root[1]!.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) out[m[1]!] = m[2]!.trim();
  return out;
}

/** WCAG 2.x AA thresholds: 4.5:1 for normal text, 3:1 for UI components and graphics. */
export const AA_TEXT = 4.5;
export const AA_NON_TEXT = 3;

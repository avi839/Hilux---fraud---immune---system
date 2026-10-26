// Scene colours from the --hx-* design tokens, with the token defaults as fallbacks.

export interface Palette {
  accent: string;
  accent2: string;
  cyan: string;
  fraud: string;
  legit: string;
  bg: string;
}

export const DEFAULT_PALETTE: Palette = {
  accent: "#8b5cf6",
  accent2: "#6366f1",
  cyan: "#22d3ee",
  fraud: "#f43f5e",
  legit: "#34d399",
  bg: "#070912",
};

const TOKENS: Record<Exclude<keyof Palette, "bg">, string> = {
  accent: "--hx-accent",
  accent2: "--hx-accent-2",
  cyan: "--hx-cyan",
  fraud: "--hx-fraud",
  legit: "--hx-legit",
};

const HEX = /^#(?:[0-9a-f]{3}|[0-9a-f]{6})$/i;

/** Read the tokens from ``:root``; anything missing or not a plain hex colour falls back. */
export function readPalette(read: (name: string) => string = cssVar): Palette {
  const out: Palette = { ...DEFAULT_PALETTE };
  for (const [key, name] of Object.entries(TOKENS) as Array<[keyof typeof TOKENS, string]>) {
    const v = read(name).trim();
    if (HEX.test(v)) out[key] = v;
  }
  return out;
}

function cssVar(name: string): string {
  if (typeof window === "undefined" || typeof getComputedStyle !== "function") return "";
  try {
    return getComputedStyle(document.documentElement).getPropertyValue(name);
  } catch {
    return "";
  }
}

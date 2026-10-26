import { useId } from "react";

interface HelixLogoProps {
  /** Rendered width and height in px. */
  size?: number;
  /** Draw the strands in (stroke-dash) and then pulse a soft glow. CSS-only; off under
   *  prefers-reduced-motion, where the mark is simply shown fully drawn. */
  animated?: boolean;
  /** Accessible name. Without one the mark is decorative and hidden from assistive tech. */
  title?: string;
}

// Two strands of a double helix, each split at the middle so the crossings can interlace:
// strand A passes over B at the upper crossing, B passes over A at the lower one.
const A1 = "M16 9C16 16 32 17 32 24";
const A2 = "M32 24C32 31 16 32 16 39";
const B1 = "M32 9C32 16 16 17 16 24";
const B2 = "M16 24C16 31 32 32 32 39";

/** HELIX mark: an original double-helix glyph inside a thin ring, pale periwinkle stroke. */
export function HelixLogo({ size = 28, animated = false, title }: HelixLogoProps) {
  // useId output contains ":", which some engines reject inside url(#…) references.
  const uid = useId().replace(/[^a-zA-Z0-9_-]/g, "");
  const grad = `hx-logo-grad-${uid}`;
  const ring = `hx-logo-ring-${uid}`;
  const titleId = `hx-logo-title-${uid}`;
  const a11y = title ? { role: "img", "aria-labelledby": titleId } : { "aria-hidden": true as const, focusable: "false" as const };

  const strand = (d: string, over: boolean, order: number) => (
    <g key={d}>
      {over ? <path d={d} className="hx-logo__gap" pathLength={1} style={{ ["--o" as string]: order }} /> : null}
      <path d={d} className="hx-logo__strand" stroke={`url(#${grad})`} pathLength={1} style={{ ["--o" as string]: order }} />
    </g>
  );

  return (
    <svg
      className={`hx-logo${animated ? " hx-logo--animated" : ""}`}
      width={size}
      height={size}
      viewBox="0 0 48 48"
      fill="none"
      data-testid="helix-logo"
      {...a11y}
    >
      {title ? <title id={titleId}>{title}</title> : null}
      <defs>
        <linearGradient id={grad} x1="0" y1="6" x2="0" y2="42" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="#f7f8ff" />
          <stop offset="0.6" stopColor="#c7d2fe" />
          <stop offset="1" stopColor="#8fa8ff" />
        </linearGradient>
        <linearGradient id={ring} x1="6" y1="4" x2="42" y2="44" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="#c7d2fe" stopOpacity="0.9" />
          <stop offset="1" stopColor="#8fa8ff" stopOpacity="0.35" />
        </linearGradient>
      </defs>
      <circle className="hx-logo__ring" cx="24" cy="24" r="21.25" stroke={`url(#${ring})`} pathLength={1} />
      <g className="hx-logo__rungs" stroke={`url(#${grad})`}>
        <path d="M19.4 12.6H28.6M17.6 24H30.4M19.4 35.4H28.6" />
      </g>
      {strand(B1, false, 0)}
      {strand(A1, true, 0)}
      {strand(A2, false, 1)}
      {strand(B2, true, 1)}
    </svg>
  );
}

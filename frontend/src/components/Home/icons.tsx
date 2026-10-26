// Small stroke icons for the Home page. Decorative only (aria-hidden); every icon sits next to
// a visible text label, so no meaning depends on the drawing.

import type { ReactNode } from "react";

export type IconName =
  | "sense"
  | "perceive"
  | "remember"
  | "reason"
  | "act"
  | "learn"
  | "fusion"
  | "dna"
  | "graph"
  | "ranked"
  | "loop"
  | "arena"
  | "soc"
  | "ledger"
  | "data";

const PATHS: Record<IconName, ReactNode> = {
  // antenna with two arcs: the ingest edge listening for events
  sense: (
    <>
      <circle cx="12" cy="12" r="1.6" />
      <path d="M8.2 15.8a5.4 5.4 0 0 1 0-7.6M15.8 8.2a5.4 5.4 0 0 1 0 7.6" />
      <path d="M5.4 18.6a9.3 9.3 0 0 1 0-13.2M18.6 5.4a9.3 9.3 0 0 1 0 13.2" />
    </>
  ),
  perceive: (
    <>
      <path d="M2.8 12s3.4-6.2 9.2-6.2 9.2 6.2 9.2 6.2-3.4 6.2-9.2 6.2S2.8 12 2.8 12Z" />
      <circle cx="12" cy="12" r="2.8" />
    </>
  ),
  remember: (
    <>
      <path d="M7 3c0 6 10 6 10 12s-10 3-10 6M17 3c0 6-10 6-10 12s10 3 10 6" />
      <path d="M8.6 7.5h6.8M8.6 16.5h6.8" />
    </>
  ),
  reason: (
    <>
      <circle cx="5.5" cy="6" r="2.2" />
      <circle cx="18.5" cy="6" r="2.2" />
      <circle cx="12" cy="18" r="2.2" />
      <path d="M7.7 6h8.6M6.6 8l4.3 7.9M17.4 8l-4.3 7.9" />
    </>
  ),
  act: (
    <>
      <path d="M5 21V4" />
      <path d="M5 4h11l-2.2 4L16 12H5" />
    </>
  ),
  learn: (
    <>
      <path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3" />
      <path d="M19.8 3.8v3.6h-3.6" />
      <path d="M9.2 12.2l2 2 3.8-4" />
    </>
  ),
  fusion: (
    <>
      <circle cx="9" cy="12" r="5.5" />
      <circle cx="15" cy="12" r="5.5" />
    </>
  ),
  dna: (
    <>
      <path d="M6 3c0 5 12 6 12 9s-12 4-12 9M18 3c0 5-12 6-12 9s12 4 12 9" />
    </>
  ),
  graph: (
    <>
      <circle cx="4.5" cy="12" r="2" />
      <circle cx="12" cy="5" r="2" />
      <circle cx="12" cy="19" r="2" />
      <circle cx="19.5" cy="12" r="2" />
      <path d="M6.2 10.7l4.3-4.2M6.2 13.3l4.3 4.2M13.5 6.5l4.4 4.2M13.5 17.5l4.4-4.2" />
    </>
  ),
  ranked: (
    <>
      <path d="M4 6h16M4 12h11M4 18h6" />
    </>
  ),
  loop: (
    <>
      <path d="M4 12a8 8 0 0 1 13.7-5.6L20 8.7" />
      <path d="M20 4v4.7h-4.7" />
      <path d="M20 12a8 8 0 0 1-13.7 5.6L4 15.3" />
      <path d="M4 20v-4.7h4.7" />
    </>
  ),
  arena: (
    <>
      <path d="M12 3l7 3v5.5c0 4.4-3 8-7 9.5-4-1.5-7-5.1-7-9.5V6l7-3Z" />
      <path d="M12 3v18" />
    </>
  ),
  soc: (
    <>
      <rect x="3.5" y="4.5" width="17" height="13" rx="2" />
      <path d="M7.5 9.5l2.5 2-2.5 2M12.5 13.5h4M8 21h8" />
    </>
  ),
  ledger: (
    <>
      <rect x="2.8" y="8.6" width="8.4" height="6.8" rx="3.4" />
      <rect x="12.8" y="8.6" width="8.4" height="6.8" rx="3.4" />
      <path d="M9 12h6" />
    </>
  ),
  data: (
    <>
      <ellipse cx="12" cy="5.5" rx="7" ry="2.5" />
      <path d="M5 5.5v13c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5v-13" />
      <path d="M5 12c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5" />
    </>
  ),
};

export function Icon({ name, size = 20 }: { name: IconName; size?: number }) {
  return (
    <svg
      className="home-icon"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {PATHS[name]}
    </svg>
  );
}

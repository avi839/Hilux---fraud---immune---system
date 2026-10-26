import type { Tier } from "../../api/types";

/** Severity_Tier as a text label plus colour, never colour alone (Req 22.10). */
export function TierBadge({ tier }: { tier: Tier }) {
  return <span className={`tier tier--${tier.toLowerCase()}`}>{tier}</span>;
}

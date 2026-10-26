// Client-side range checks of the runtime settings (Req 14.10, 22.14). The ranges mirror
// backend/helix/config/schema.py PrioritiserConfig.

export const ALERT_BUDGET_RANGE = { lo: 1, hi: 1_000 } as const;
export const INVESTIGATION_COST_RANGE = { lo: 0, hi: 1e9 } as const;
export const INVESTIGATOR_MAX = 64;

export type Checked<T> = { ok: true; value: T } | { ok: false; message: string };

export const ALERT_BUDGET_MESSAGE = "Alert budget must be a whole number from 1 to 1,000.";
export const INVESTIGATION_COST_MESSAGE = "Investigation cost must be a number from 0 to 1,000,000,000.";

export function checkAlertBudget(raw: string): Checked<number> {
  const s = raw.trim();
  if (!/^\d+$/.test(s)) return { ok: false, message: ALERT_BUDGET_MESSAGE };
  const n = Number(s);
  if (!Number.isSafeInteger(n) || n < ALERT_BUDGET_RANGE.lo || n > ALERT_BUDGET_RANGE.hi) {
    return { ok: false, message: ALERT_BUDGET_MESSAGE };
  }
  return { ok: true, value: n };
}

export function checkInvestigationCost(raw: string): Checked<number> {
  const s = raw.trim();
  if (!/^\d+(\.\d+)?$/.test(s)) return { ok: false, message: INVESTIGATION_COST_MESSAGE };
  const n = Number(s);
  if (!Number.isFinite(n) || n < INVESTIGATION_COST_RANGE.lo || n > INVESTIGATION_COST_RANGE.hi) {
    return { ok: false, message: INVESTIGATION_COST_MESSAGE };
  }
  return { ok: true, value: n };
}

export function checkInvestigator(raw: string): Checked<string> {
  const s = raw.trim();
  if (s.length === 0) return { ok: false, message: "Enter your investigator name first." };
  if (s.length > INVESTIGATOR_MAX) return { ok: false, message: `Investigator name must be at most ${INVESTIGATOR_MAX} characters.` };
  return { ok: true, value: s };
}

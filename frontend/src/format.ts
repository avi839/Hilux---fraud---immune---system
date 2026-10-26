// Display formatting. Amounts arrive as exact decimal strings, so they are grouped by string
// manipulation instead of going through floating point.

export function groupDigits(amount: string): string {
  const neg = amount.startsWith("-");
  const body = neg ? amount.slice(1) : amount;
  const [int = "0", frac] = body.split(".");
  const grouped = int.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return `${neg ? "-" : ""}${grouped}${frac !== undefined ? `.${frac}` : ""}`;
}

export function formatMoney(amount: string, currency: string): string {
  return `${currency} ${groupDigits(amount)}`;
}

export function formatScore(v: number | null | undefined, dp = 2): string {
  return typeof v === "number" && Number.isFinite(v) ? v.toFixed(dp) : "n/a";
}

/** "HH:MM:SS.mmm" in UTC plus the date when it is not today's (UTC) date. */
export function formatTime(iso: string, now: Date = new Date()): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const s = d.toISOString(); // YYYY-MM-DDTHH:MM:SS.mmmZ
  const date = s.slice(0, 10);
  const time = s.slice(11, 23);
  return date === now.toISOString().slice(0, 10) ? time : `${date} ${time}`;
}

export function formatAccounts(accounts: readonly string[], max = 3): string {
  if (accounts.length <= max) return accounts.join(", ");
  return `${accounts.slice(0, max).join(", ")} +${accounts.length - max} more`;
}

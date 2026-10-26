// Typed view-model of the HELIX vs baselines benchmark (benchmarks/results/summary.json, copied
// to ./data/summary.json). Every number the Benchmark tab shows is derived here from that file:
// nothing is hard-coded, so the page follows the file when the benchmark is re-run. Winner
// badges are computed per metric, so HELIX only gets a badge where the data gives it one.

import summaryJson from "./data/summary.json";

export type Capacity = "25" | "50" | "100";
export const CAPACITIES: Capacity[] = ["25", "50", "100"];

/** One system at one capacity (top N alerts per clock hour). */
export interface CapacityResult {
  alerts: number;
  tp: number;
  fp: number;
  alert_precision: number;
  precision_at_n: number;
  tx_recall: number;
  value_captured_usd: number;
  value_captured_frac: number;
  investigator_hours: number;
  value_per_inv_hour_usd: number;
  cost_per_true_catch_usd: number | null;
  wasted_hours: number;
  value_capped_usd: number;
  value_capped_frac: number;
  value_capped_per_inv_hour_usd: number;
  /** [min, max] over the seeds, for the seeded baselines; null for deterministic systems. */
  seed_range: Record<string, number[]> | null;
}

export interface SystemResult {
  candidate_alerts?: number;
  by_capacity: Record<string, CapacityResult>;
  roc_auc: number[];
  pr_auc: number[];
}

export interface BenchmarkSummary {
  dataset: string;
  windows: { baseline_fit: string[]; helix_warmup: string[]; test: string[]; timezone?: string };
  rows: {
    fit: number;
    fit_laundering: number;
    test: number;
    test_laundering: number;
    test_laundering_usd: number;
    helix_warmup?: number;
    helix_test?: number;
    test_rows_helix_cannot_ingest?: { rows: number; laundering: number; laundering_usd: number; laundering_value_share: number };
  };
  test_hours: number;
  assumptions: {
    minutes_per_case: number;
    analyst_cost_per_hour?: number;
    cost_per_case?: number;
    cost_per_alert?: number;
    currency: string;
    value_cap_usd: number;
    value_cap_note?: string;
    helix_config?: string;
    helix_alert_budget_per_hour?: number;
    capacity?: string;
    alert_unit?: string;
    true_positive?: string;
    scope_note?: string;
  };
  seeds: number[];
  headline_seed?: number;
  systems: Record<string, SystemResult>;
}

/** The measured results shipped with the app. */
export const SUMMARY: BenchmarkSummary = summaryJson;

// --- systems -------------------------------------------------------------------------------

export type SystemId = "helix" | "helix-backlog" | "rule" | "iforest" | "xgboost" | string;

export interface SystemView {
  /** Key in summary.systems (the full descriptive name). */
  key: string;
  id: SystemId;
  /** Short display name. */
  label: string;
  /** HELIX itself (or a variant) vs a comparison baseline. */
  family: "helix" | "baseline";
  /** Trained on labelled laundering history. */
  labelsNeeded: boolean;
  result: SystemResult;
}

const KNOWN: { test: RegExp; id: string; label: string; family: "helix" | "baseline"; labelsNeeded: boolean }[] = [
  { test: /^HELIX \+ Backlog/i, id: "helix-backlog", label: "HELIX + Backlog", family: "helix", labelsNeeded: false },
  { test: /^HELIX/i, id: "helix", label: "HELIX", family: "helix", labelsNeeded: false },
  { test: /^Rule/i, id: "rule", label: "Rule baseline", family: "baseline", labelsNeeded: false },
  { test: /^Isolation Forest/i, id: "iforest", label: "Isolation Forest", family: "baseline", labelsNeeded: false },
  { test: /^XGBoost/i, id: "xgboost", label: "XGBoost", family: "baseline", labelsNeeded: true },
];

/** The systems in file order, with display names and whether they need labels. */
export function systemsOf(summary: BenchmarkSummary): SystemView[] {
  return Object.entries(summary.systems).map(([key, result], i) => {
    const k = KNOWN.find((m) => m.test.test(key));
    if (k) return { key, id: k.id, label: k.label, family: k.family, labelsNeeded: k.labelsNeeded, result };
    return { key, id: `system-${i}`, label: key, family: "baseline", labelsNeeded: /supervised/i.test(key), result };
  });
}

export function at(s: SystemView, n: Capacity): CapacityResult | null {
  return s.result.by_capacity[n] ?? null;
}

// --- formatting ----------------------------------------------------------------------------

export const int = (v: number) => Math.round(v).toLocaleString("en-US");
export const pct = (v: number) => `${(v * 100).toFixed(1)}%`;
export const usd = (v: number) => `$${Math.round(v).toLocaleString("en-US")}`;
export const hours = (v: number) => `${v.toFixed(1)} h`;
export const dec3 = (v: number) => v.toFixed(3);
export const dec5 = (v: number) => v.toFixed(5);

// --- metrics and winner badges ---------------------------------------------------------------

export type Better = "higher" | "lower";

export interface MetricDef {
  key: keyof Omit<CapacityResult, "seed_range">;
  label: string;
  /** null: a count that is not ranked (more alerts is neither better nor worse). */
  better: Better | null;
  format: (v: number) => string;
}

export const TABLE_METRICS: MetricDef[] = [
  { key: "alerts", label: "Alerts", better: null, format: int },
  { key: "tp", label: "True positives", better: "higher", format: int },
  { key: "fp", label: "False positives", better: "lower", format: int },
  { key: "alert_precision", label: "Alert precision", better: "higher", format: pct },
  { key: "tx_recall", label: "Recall", better: "higher", format: pct },
  { key: "value_capped_frac", label: "Capped value share", better: "higher", format: pct },
  { key: "value_capped_per_inv_hour_usd", label: "Capped value / investigator-hour", better: "higher", format: usd },
  { key: "cost_per_true_catch_usd", label: "Cost per true catch", better: "lower", format: usd },
  { key: "wasted_hours", label: "Wasted hours", better: "lower", format: hours },
];

export const metric = (key: MetricDef["key"]): MetricDef => TABLE_METRICS.find((m) => m.key === key)!;

export type Badge = "overall" | "label-free";

export const BADGE_TEXT: Record<Badge, string> = {
  overall: "Best overall",
  "label-free": "Best without labels",
};

const isBetter = (a: number, b: number, better: Better) => (better === "higher" ? a > b : a < b);

function bestValue(values: number[], better: Better): number | null {
  return values.reduce<number | null>((best, v) => (best === null || isBetter(v, best, better) ? v : best), null);
}

/** Winner badges for one metric: "Best overall" to every system tied for the best value, and
 *  "Best without labels" to the best label-free systems when no label-free system is best
 *  overall. Systems with no value get none. */
export function badges(systems: SystemView[], value: (s: SystemView) => number | null | undefined, better: Better | null): Map<string, Badge> {
  const out = new Map<string, Badge>();
  if (better === null) return out;
  const scored = systems.flatMap((s) => {
    const v = value(s);
    return typeof v === "number" && Number.isFinite(v) ? [{ s, v }] : [];
  });
  const overall = bestValue(
    scored.map((x) => x.v),
    better,
  );
  if (overall === null) return out;
  for (const x of scored) if (x.v === overall) out.set(x.s.id, "overall");
  const free = scored.filter((x) => !x.s.labelsNeeded);
  const freeBest = bestValue(
    free.map((x) => x.v),
    better,
  );
  if (freeBest !== null && freeBest !== overall) for (const x of free) if (x.v === freeBest) out.set(x.s.id, "label-free");
  return out;
}

/** Badges for every table column at capacity ``n``, keyed by metric then system id. */
export function tableBadges(systems: SystemView[], n: Capacity): Record<string, Map<string, Badge>> {
  const out: Record<string, Map<string, Badge>> = {};
  for (const m of TABLE_METRICS) out[m.key] = badges(systems, (s) => at(s, n)?.[m.key], m.better);
  return out;
}

/** "[lo, hi]" over the seeds for a metric, if the system has a seed range for it. */
export function seedRange(r: CapacityResult, key: string): [number, number] | null {
  const v = r.seed_range?.[key];
  return v && v.length === 2 && typeof v[0] === "number" && typeof v[1] === "number" ? [v[0], v[1]] : null;
}

// --- KPI tiles -----------------------------------------------------------------------------

export interface KpiView {
  metric: MetricDef;
  title: string;
  helix: number | null;
  /** The best label-free comparison baseline (not a HELIX variant). */
  rival: { label: string; value: number } | null;
  /** HELIX vs that baseline, in plain words. */
  verdict: "better" | "worse" | "equal" | null;
  supervised: { label: string; value: number } | null;
}

export const KPI_METRICS: { key: MetricDef["key"]; title: string }[] = [
  { key: "alert_precision", title: "HELIX alert precision" },
  { key: "cost_per_true_catch_usd", title: "Cost per true catch" },
  { key: "value_capped_per_inv_hour_usd", title: "Capped value per investigator-hour" },
  { key: "wasted_hours", title: "Wasted investigator hours" },
];

export function kpis(systems: SystemView[], n: Capacity): KpiView[] {
  const helix = systems.find((s) => s.id === "helix") ?? systems.find((s) => s.family === "helix") ?? null;
  const baselines = systems.filter((s) => s.family === "baseline" && !s.labelsNeeded);
  const sup = systems.find((s) => s.labelsNeeded) ?? null;
  return KPI_METRICS.map(({ key, title }) => {
    const m = metric(key);
    const better = m.better ?? "higher";
    const h = helix ? at(helix, n)?.[key] ?? null : null;
    let rival: KpiView["rival"] = null;
    for (const b of baselines) {
      const v = at(b, n)?.[key];
      if (typeof v === "number" && (rival === null || isBetter(v, rival.value, better))) rival = { label: b.label, value: v };
    }
    const verdict = h === null || rival === null ? null : h === rival.value ? "equal" : isBetter(h, rival.value, better) ? "better" : "worse";
    const sv = sup ? at(sup, n)?.[key] : null;
    return { metric: m, title, helix: h, rival, verdict, supervised: sup && typeof sv === "number" ? { label: sup.label, value: sv } : null };
  });
}

// --- ranking metrics -----------------------------------------------------------------------

export interface RankingRow {
  system: SystemView;
  roc: number | null;
  pr: number | null;
  rocRange: [number, number] | null;
  prRange: [number, number] | null;
}

function headline(values: number[], seeds: number[], headlineSeed: number | undefined): number | null {
  if (values.length === 0) return null;
  const i = headlineSeed === undefined ? 0 : seeds.indexOf(headlineSeed);
  return values[i >= 0 && i < values.length ? i : 0] ?? null;
}

const range = (v: number[]): [number, number] | null => (v.length > 1 ? [Math.min(...v), Math.max(...v)] : null);

export function rankingRows(summary: BenchmarkSummary, systems: SystemView[]): RankingRow[] {
  return systems.map((s) => ({
    system: s,
    roc: headline(s.result.roc_auc, summary.seeds, summary.headline_seed),
    pr: headline(s.result.pr_auc, summary.seeds, summary.headline_seed),
    rocRange: range(s.result.roc_auc),
    prRange: range(s.result.pr_auc),
  }));
}

/** PR-AUC of a random ranker: the laundering share of the test rows. */
export function randomPrAuc(summary: BenchmarkSummary): number {
  return summary.rows.test > 0 ? summary.rows.test_laundering / summary.rows.test : 0;
}

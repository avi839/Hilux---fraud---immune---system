// Pure view-model helpers for the EvidencePanel. They read only the Evidence_Chain and the
// explanation returned by GET /cases/{id}/evidence, mirroring helix/explain/engine.py.

import type { ChainEntry, EvidenceChainDoc, ExplanationDoc, ExplanationItemDoc, MassDoc } from "../../api/types";

export type Bucket = "reason" | "mitigating" | "insufficient";

/** Discounted Mass_Function of one contributor entry. */
export interface Discounted {
  f: number; // f' = w·f, mass on {FRAUD}
  l: number; // l' = w·l, mass on {LEGIT}
  theta: number; // Θ' = 1 − f' − l', Uncertainty_Mass on {FRAUD, LEGIT}
}

export interface ContributorBar extends Discounted {
  name: string;
  label: string;
  bucket: Bucket;
  weight: number;
  /** Transaction of the representative entry the bar shows. */
  txId: string;
  /** Number of Case transactions this contributor emitted evidence for. */
  txCount: number;
}

export interface UncertaintyCondition {
  sensor: string;
  label: string;
  /** e.g. "missing field lat, lon" or "insufficient history (2 prior events)". */
  reason: string;
  txIds: string[];
}

const clamp01 = (x: number) => (Number.isFinite(x) ? Math.min(1, Math.max(0, x)) : 0);

/** f' = w·f, l' = w·l, Θ' = 1 − f' − l' (Shafer discounting). Inputs are clamped to [0, 1]
 *  so a malformed entry can never produce a bar wider or narrower than 100%. */
export function discount(mass: MassDoc, weight: number): Discounted {
  const w = clamp01(weight);
  let f = clamp01(w * mass.f);
  let l = clamp01(w * mass.l);
  const s = f + l;
  if (s > 1) {
    f /= s;
    l /= s;
  }
  return { f, l, theta: Math.max(0, 1 - f - l) };
}

/** Segment widths in percent. Θ' takes the remainder, so the three always add up to 100. */
export function segmentPercents(d: Discounted): [number, number, number] {
  const f = d.f * 100;
  const l = d.l * 100;
  return [f, l, Math.max(0, 100 - f - l)];
}

export function isContributor(e: ChainEntry): e is ChainEntry & { mass: MassDoc; weight: number } {
  return (e.kind === "CONTRIBUTOR" || e.kind === "MOTIF") && e.mass !== null && typeof e.weight === "number";
}

function labelIndex(ex: ExplanationDoc | null | undefined): Map<string, ExplanationItemDoc> {
  const m = new Map<string, ExplanationItemDoc>();
  if (!ex) return m;
  for (const it of [...ex.reasons, ...ex.mitigating, ...ex.insufficient]) m.set(it.name, it);
  return m;
}

export function sensorLabel(name: string): string {
  return name ? name.charAt(0).toUpperCase() + name.slice(1) : name;
}

/**
 * One stacked bar per contributor. A contributor that emitted evidence for several Case
 * transactions is shown by its REPRESENTATIVE entry, chosen with the Explanation_Engine's
 * rule: the entry with the largest f' for a reason (F' > L'), the largest l' for a mitigating
 * factor (L' > F'), else the first entry; ties go to the first entry in chain order. The bar
 * is that entry's own discounted Mass_Function, so its three segments are a real mass
 * function that sums to 1 (taking max f' and max l' from different entries would not), and
 * for reasons/mitigating factors its decisive segment equals the F'/L' the explanation cites.
 *
 * Bars follow the explanation's order (reasons, mitigating, insufficient).
 */
export function contributorBars(chain: EvidenceChainDoc | null | undefined, ex?: ExplanationDoc | null): ContributorBar[] {
  if (!chain) return [];
  const groups = new Map<string, Array<ChainEntry & { mass: MassDoc; weight: number }>>();
  for (const e of chain.entries) {
    if (!isContributor(e)) continue;
    const g = groups.get(e.contributor_id);
    if (g) g.push(e);
    else groups.set(e.contributor_id, [e]);
  }
  const items = labelIndex(ex);
  const bars: ContributorBar[] = [];
  for (const [name, g] of groups) {
    const fs = g.map((e) => e.weight * e.mass.f);
    const ls = g.map((e) => e.weight * e.mass.l);
    const F = Math.max(...fs);
    const L = Math.max(...ls);
    const bucket: Bucket = F > L ? "reason" : L > F ? "mitigating" : "insufficient";
    const idx = bucket === "reason" ? fs.indexOf(F) : bucket === "mitigating" ? ls.indexOf(L) : 0;
    const rep = g[idx] ?? g[0]!;
    bars.push({
      name,
      label: items.get(name)?.label ?? sensorLabel(name),
      bucket,
      weight: rep.weight,
      txId: rep.tx_id,
      txCount: new Set(g.map((e) => e.tx_id)).size,
      ...discount(rep.mass, rep.weight),
    });
  }
  const order = new Map<string, number>();
  if (ex) [...ex.reasons, ...ex.mitigating, ...ex.insufficient].forEach((it, i) => order.set(it.name, i));
  const rank = { reason: 0, mitigating: 1, insufficient: 2 } as const;
  return bars.sort(
    (a, b) =>
      (order.get(a.name) ?? Number.MAX_SAFE_INTEGER) - (order.get(b.name) ?? Number.MAX_SAFE_INTEGER) ||
      rank[a.bucket] - rank[b.bucket] ||
      (a.name < b.name ? -1 : a.name > b.name ? 1 : 0),
  );
}

/** Same wording as the engine's ``_insufficient_reason`` for one ``vacuous_reason``. */
export function describeVacuousReason(r: string): string[] {
  if (r.startsWith("missing:")) {
    const fields = r.slice("missing:".length).split(",").filter(Boolean);
    const hist = fields.filter((f) => f.startsWith("dna.")).map((f) => f.slice("dna.".length));
    const ev = fields.filter((f) => !f.startsWith("dna."));
    const out: string[] = [];
    if (ev.length) out.push(`missing field ${ev.join(", ")}`);
    if (hist.length) out.push(`missing history for ${hist.join(", ")}`);
    return out;
  }
  if (r.startsWith("history:")) return [`insufficient history (${r.slice("history:".length)} prior events)`];
  return []; // sensor failures are not missing-field / insufficient-history conditions
}

const CONDITION_RE = /^(missing field|missing history|insufficient history)/;

/**
 * Missing-field and insufficient-history conditions that contributed to the Case's
 * Uncertainty_Mass, each paired with the Sensor_Agent (Req 7.7, 7.8). Sources, deduplicated
 * by (sensor, reason):
 *  1. contributor entries whose ``detail.vacuous_reason`` is "missing:..." or "history:N";
 *  2. MISSING_FIELD records (``detail.field`` and the comma-separated ``detail.sensors``);
 *  3. insufficient-evidence items whose comparison names such a condition (covers chains
 *     whose entries lack ``detail``).
 * An empty result means "none contributed" (Req 7.9).
 */
export function uncertaintyConditions(chain: EvidenceChainDoc | null | undefined, ex?: ExplanationDoc | null): UncertaintyCondition[] {
  const items = labelIndex(ex);
  const byKey = new Map<string, UncertaintyCondition>();
  const add = (sensor: string, reason: string, tx: string) => {
    const key = `${sensor}\u0000${reason}`;
    const c = byKey.get(key);
    if (c) {
      if (!c.txIds.includes(tx)) c.txIds.push(tx);
    } else {
      byKey.set(key, { sensor, label: items.get(sensor)?.label ?? sensorLabel(sensor), reason, txIds: tx ? [tx] : [] });
    }
  };
  const entries = chain?.entries ?? [];
  for (const e of entries) {
    if (!isContributor(e)) continue;
    const r = e.detail["vacuous_reason"];
    if (typeof r === "string" && r) for (const reason of describeVacuousReason(r)) add(e.contributor_id, reason, e.tx_id);
  }
  for (const e of entries) {
    if (e.kind === "MISSING_FIELD") {
      const field = e.detail["field"];
      const sensors = e.detail["sensors"];
      if (typeof field !== "string" || typeof sensors !== "string") continue;
      for (const s of sensors.split(",").filter(Boolean)) {
        // The contributor entry already reports "missing field lat, lon" for this sensor and
        // transaction; only add a per-field condition when nothing covers it yet.
        const covered = [...byKey.values()].some(
          (c) => c.sensor === s && c.reason.startsWith("missing field") && c.reason.split(/[ ,]+/).includes(field) && c.txIds.includes(e.tx_id),
        );
        if (!covered) add(s, `missing field ${field}`, e.tx_id);
      }
    }
  }
  for (const it of ex?.insufficient ?? []) {
    if ([...byKey.values()].some((c) => c.sensor === it.name)) continue;
    for (const part of it.comparison.split("; ")) if (CONDITION_RE.test(part)) for (const tx of it.tx_ids) add(it.name, part, tx);
  }
  return [...byKey.values()].sort((a, b) => (a.sensor < b.sensor ? -1 : a.sensor > b.sensor ? 1 : 0));
}

/** Conditions grouped per sensor, in sensor order, for one list item each. */
export function groupBySensor(conds: UncertaintyCondition[]): Array<{ sensor: string; label: string; conditions: UncertaintyCondition[] }> {
  const out: Array<{ sensor: string; label: string; conditions: UncertaintyCondition[] }> = [];
  for (const c of conds) {
    const last = out[out.length - 1];
    if (last && last.sensor === c.sensor) last.conditions.push(c);
    else out.push({ sensor: c.sensor, label: c.label, conditions: [c] });
  }
  return out;
}

export const fmt = (x: number, dp = 3) => (Number.isFinite(x) ? x.toFixed(dp) : "n/a");

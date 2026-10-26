// View-model for the Red-vs-Blue arena (Req 38.3). Reads GET /attacker and GET /immune
// (backend helix/api/immune_routes.py attacker_doc / immune_doc). Both documents have the
// same keys whether or not the Attacker_Agent / Immune_System is attached; anything the
// server does not report stays null and is shown as "n/a". Nothing is invented.
//
// Shared shape handling (pool size, running flag) is reused from the LoopPanel view-model.

import { attackerRunning, poolSize } from "../LoopPanel/loop";

const isObj = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const int0 = (v: unknown): number => num(v) ?? 0;
const str = (v: unknown): string | null => (typeof v === "string" && v !== "" ? v : null);
const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
const strs = (v: unknown): string[] => arr(v).filter((x): x is string => typeof x === "string");

// --- Attacker_Agent ------------------------------------------------------------------------

export interface CampaignView {
  id: string;
  genomeId: string;
  patternType: string | null;
  startedAt: string | null;
  /** Planned events. */
  events: number;
  injected: number;
  /** Not injected: the label write failed or the Stream_Ingestor did not accept the event. */
  withheld: number;
  /** Fraction of the campaign's injected Transaction_Events not detected (null until scored). */
  evasion: number | null;
  completed: boolean;
  windowEnd: string | null;
}

export interface GenomeView {
  id: string;
  patternType: string | null;
  parentId: string | null;
  generation: number | null;
  evasion: number | null;
  scoredCampaigns: number | null;
  retired: boolean;
  /** Genome parameters, when the server reports them (current GET /attacker does not). */
  params: Record<string, number> | null;
}

export interface AttackerView {
  configured: boolean;
  /** The attacker can be started (a Synthetic_Generator exists) even if not yet attached. */
  available: boolean;
  running: boolean | null;
  seed: number | null;
  injectedTx: number;
  withheld: number;
  genomesTotal: number;
  genomesActive: number;
  campaignsTotal: number;
  campaignsCompleted: number;
  campaignsLive: number;
  meanEvasion: number | null;
  /** Newest first, as served. */
  campaigns: CampaignView[];
  /** Top active scored genomes by evasion, as served. */
  genomes: GenomeView[];
}

function campaignOf(v: unknown): CampaignView | null {
  if (!isObj(v)) return null;
  const id = str(v.campaign_id);
  if (id === null) return null;
  return {
    id,
    genomeId: str(v.genome_id) ?? "n/a",
    patternType: str(v.pattern_type),
    startedAt: str(v.started_at),
    events: int0(v.events),
    injected: int0(v.injected),
    withheld: int0(v.withheld),
    evasion: num(v.evasion),
    completed: v.completed === true,
    windowEnd: str(v.window_end),
  };
}

function paramsOf(v: unknown): Record<string, number> | null {
  if (!isObj(v)) return null;
  const out: Record<string, number> = {};
  for (const [k, x] of Object.entries(v)) {
    const n = num(x);
    if (n !== null) out[k] = n;
  }
  return Object.keys(out).length ? out : null;
}

function genomeOf(v: unknown): GenomeView | null {
  if (!isObj(v)) return null;
  const id = str(v.genome_id);
  if (id === null) return null;
  return {
    id,
    patternType: str(v.pattern_type),
    parentId: str(v.parent_id),
    generation: num(v.generation),
    evasion: num(v.evasion),
    scoredCampaigns: num(v.scored_campaigns),
    retired: v.retired === true,
    params: paramsOf(v.params),
  };
}

/** Normalise a GET /attacker document; null when it is not an object. */
export function normaliseAttacker(doc: unknown): AttackerView | null {
  if (!isObj(doc)) return null;
  return {
    configured: doc.configured === true,
    available: doc.available === true || doc.configured === true,
    running: attackerRunning(doc),
    seed: num(doc.seed),
    injectedTx: int0(doc.injected_tx_total),
    withheld: int0(doc.withheld_total),
    genomesTotal: int0(doc.genomes_total),
    genomesActive: int0(doc.genomes_active),
    campaignsTotal: int0(doc.campaigns_total),
    campaignsCompleted: int0(doc.campaigns_completed),
    campaignsLive: int0(doc.campaigns_live),
    meanEvasion: num(doc.mean_evasion),
    campaigns: arr(doc.recent_campaigns).map(campaignOf).filter((c): c is CampaignView => c !== null),
    genomes: arr(doc.top_genomes).map(genomeOf).filter((g): g is GenomeView => g !== null),
  };
}

// --- Immune_System ---------------------------------------------------------------------------

export interface AntibodyView {
  id: string;
  status: string;
  /** "miss:<tx_id>" or "case:<case_id>". */
  origin: string | null;
  parentId: string | null;
  createdAt: string | null;
  activatedAt: string | null;
  tags: string[];
  dims: string[];
  selfMatchRate: number | null;
  matches: number;
  confirmedFraudMatches: number;
  falsePositiveMatches: number;
  endedAt: string | null;
  endReason: string | null;
}

export interface MissView {
  eventId: string;
  campaignId: string | null;
  missTime: string | null;
}

export interface ImmuneView {
  enabled: boolean;
  missDetection: boolean;
  pool: { size: number; max: number | null } | null;
  held: number;
  selfSetSize: number;
  selfSetMin: number | null;
  detectionDeadlineS: number | null;
  misses: number;
  pendingAttackEvents: number;
  triggers: number;
  discards: number;
  retirements: number;
  byStatus: Record<string, number>;
  /** Antibodies ever created (sum of by_status), or null when not reported. */
  antibodiesTotal: number | null;
  /** Lineage records, newest first, as served. */
  antibodies: AntibodyView[];
  /** Newest first, as served. */
  recentMisses: MissView[];
}

function antibodyOf(v: unknown): AntibodyView | null {
  if (!isObj(v)) return null;
  const id = str(v.ab_id) ?? str(v.antibody_id);
  if (id === null) return null;
  return {
    id,
    status: str(v.status) ?? "UNKNOWN",
    origin: str(v.origin),
    parentId: str(v.parent_id),
    createdAt: str(v.created_at),
    activatedAt: str(v.activated_at),
    tags: strs(v.tags),
    dims: strs(v.dims),
    selfMatchRate: num(v.self_match_rate),
    matches: int0(v.matches),
    confirmedFraudMatches: int0(v.confirmed_fraud_matches),
    falsePositiveMatches: int0(v.false_positive_matches),
    endedAt: str(v.ended_at),
    endReason: str(v.end_reason),
  };
}

function missOf(v: unknown): MissView | null {
  if (!isObj(v)) return null;
  const eventId = str(v.event_id);
  if (eventId === null) return null;
  return { eventId, campaignId: str(v.campaign_id), missTime: str(v.miss_time) };
}

/** Normalise a GET /immune document; null when it is not an object. */
export function normaliseImmune(doc: unknown): ImmuneView | null {
  if (!isObj(doc)) return null;
  const byStatus: Record<string, number> = {};
  if (isObj(doc.by_status)) {
    for (const [k, x] of Object.entries(doc.by_status)) {
      const n = num(x);
      if (n !== null) byStatus[k] = n;
    }
  }
  const counts = Object.values(byStatus);
  return {
    enabled: doc.enabled === true,
    missDetection: doc.miss_detection === true,
    pool: poolSize(doc),
    held: int0(doc.held),
    selfSetSize: int0(doc.self_set_size),
    selfSetMin: num(doc.self_set_min),
    detectionDeadlineS: num(doc.detection_deadline_s),
    misses: int0(doc.misses),
    pendingAttackEvents: int0(doc.pending_attack_events),
    triggers: int0(doc.triggers),
    discards: int0(doc.discards),
    retirements: int0(doc.retirements),
    byStatus,
    antibodiesTotal: counts.length ? counts.reduce((a, b) => a + b, 0) : null,
    antibodies: arr(doc.recent_antibodies).map(antibodyOf).filter((a): a is AntibodyView => a !== null),
    recentMisses: arr(doc.recent_misses).map(missOf).filter((m): m is MissView => m !== null),
  };
}

// --- derived views ---------------------------------------------------------------------------

const timeOf = (iso: string | null): number => {
  const t = iso === null ? NaN : Date.parse(iso);
  return Number.isNaN(t) ? Number.POSITIVE_INFINITY : t;
};

/** Oldest first by start time, ties (and unparseable times) by campaign id. */
export function chronological(campaigns: readonly CampaignView[]): CampaignView[] {
  return [...campaigns].sort((a, b) => timeOf(a.startedAt) - timeOf(b.startedAt) || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
}

export type MissOutcome = "fully missed" | "partially missed";

export interface MissedCampaign {
  campaign: CampaignView;
  outcome: MissOutcome;
  /** Miss records for this campaign among the served recent misses (Req 21.1). */
  missRecords: number;
}

/** Completed Attack_Campaigns HELIX did not fully detect (evasion > 0), newest first.
 *  Evasion 1 = no injected Transaction_Event was detected by the end of the window. */
export function missedCampaigns(campaigns: readonly CampaignView[], misses: readonly MissView[] = []): MissedCampaign[] {
  const perCampaign = new Map<string, number>();
  for (const m of misses) if (m.campaignId !== null) perCampaign.set(m.campaignId, (perCampaign.get(m.campaignId) ?? 0) + 1);
  return chronological(campaigns)
    .reverse()
    .filter((c) => c.completed && c.evasion !== null && c.evasion > 0)
    .map((c) => ({
      campaign: c,
      outcome: c.evasion! >= 1 ? "fully missed" : "partially missed",
      missRecords: perCampaign.get(c.id) ?? 0,
    }));
}

export interface DetectionPoint {
  campaignId: string;
  startedAt: string | null;
  /** 1 - evasion of this campaign, in [0, 1]. */
  rate: number;
  /** Running mean of ``rate`` over this and every earlier scored campaign. */
  cumulative: number;
}

/** Detection rate over time: one point per scored (completed) campaign, oldest first. */
export function detectionSeries(campaigns: readonly CampaignView[]): DetectionPoint[] {
  const out: DetectionPoint[] = [];
  let sum = 0;
  for (const c of chronological(campaigns)) {
    if (!c.completed || c.evasion === null) continue;
    const rate = Math.min(1, Math.max(0, 1 - c.evasion));
    sum += rate;
    out.push({ campaignId: c.id, startedAt: c.startedAt, rate, cumulative: sum / (out.length + 1) });
  }
  return out;
}

/** Root-first genome ids from the oldest known ancestor to ``id``; ancestors the server did not
 *  serve end the chain at the last known ``parentId``. Cycles are cut. */
export function genomeChain(id: string, byId: ReadonlyMap<string, GenomeView>): string[] {
  const chain: string[] = [];
  const seen = new Set<string>();
  let cur: string | null = id;
  while (cur !== null && !seen.has(cur)) {
    seen.add(cur);
    chain.unshift(cur);
    cur = byId.get(cur)?.parentId ?? null;
  }
  return chain;
}

export interface ParamChange {
  param: string;
  from: number;
  to: number;
}

/** Parameters in which a genome differs from its parent, or null when either side's
 *  parameters are unknown (GET /attacker does not currently serve genome parameters). */
export function mutationsOf(g: GenomeView, byId: ReadonlyMap<string, GenomeView>): ParamChange[] | null {
  if (g.parentId === null || g.params === null) return null;
  const p = byId.get(g.parentId)?.params;
  if (!p) return null;
  return Object.keys(g.params)
    .sort()
    .filter((k) => p[k] !== g.params![k])
    .map((k) => ({ param: k, from: p[k] ?? NaN, to: g.params![k]! }));
}

/** "62.5%" for a ratio in [0, 1], else "n/a". */
export function pct(r: number | null | undefined, dp = 1): string {
  return typeof r === "number" && Number.isFinite(r) ? `${(r * 100).toFixed(dp)}%` : "n/a";
}

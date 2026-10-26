// GET /api/v1/trace/{tx_id} (Req 38.4, 38.5): the path of one Transaction_Event through
// SENSE, PERCEIVE, REMEMBER, REASON, ACT and LEARN, as built by helix/api/app.py.

import { READ_TIMEOUT_MS, request } from "../../api/client";
import type { Tier } from "../../api/types";

/** Pipeline order in which the trace view renders the stages (Req 38.6). */
export const STAGE_ORDER = ["SENSE", "PERCEIVE", "REMEMBER", "REASON", "ACT", "LEARN"] as const;
export type StageName = (typeof STAGE_ORDER)[number];

export interface TraceMass {
  fraud: number;
  legit: number;
  uncertainty: number;
}

export interface TraceAssessment {
  belief: number;
  plausibility: number;
  uncertainty: number;
  conflict: number;
  high_conflict?: boolean;
}

export type FeatureValue = number | string | null;

export interface TraceSensor {
  name: string;
  outcome: string;
  mass: TraceMass | null;
  features?: Record<string, FeatureValue>;
  vacuous_reason?: string | null;
  error?: string | null;
}

export interface TraceMotifHit {
  contributor_id: string;
  mass: TraceMass;
  weight: number;
}

export interface TraceStages {
  SENSE?: { received_at?: string | null; lateness?: { kind: string; seconds: number } | null };
  PERCEIVE?: { sensor_results?: TraceSensor[]; stage1_assessment?: TraceAssessment | null };
  REMEMBER?: {
    dna_comparison?: Record<string, Record<string, FeatureValue>>;
    change_point?: { kind: string; short_run_mass: number; est_start?: string } | null;
  };
  REASON?: { motifs?: TraceMotifHit[]; truncated_motif_types?: string[]; stage2_assessment?: TraceAssessment | null };
  ACT?: { risk_score?: number; severity_tier?: Tier; case_id?: string | null };
  LEARN?: { label?: string | null; note?: string };
}

export interface TraceDoc {
  tx_id: string;
  timestamp: string | null;
  /** "pipeline": the event is still held in memory (full detail); "storage": rebuilt from
   *  stored assessments after eviction (no per-sensor features). */
  source?: "pipeline" | "storage";
  event: {
    src: string;
    dst: string;
    amount: string;
    currency: string;
    channel?: string | null;
    mcc?: string | null;
    device_id?: string | null;
  } | null;
  stages: TraceStages;
}

export function getTrace(txId: string, signal?: AbortSignal): Promise<TraceDoc> {
  return request<TraceDoc>(`/trace/${encodeURIComponent(txId)}`, { signal, timeoutMs: READ_TIMEOUT_MS });
}

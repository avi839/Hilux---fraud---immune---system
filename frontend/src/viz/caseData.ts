// Shared, de-duplicated loads of per-Case documents used by several panels (the timeline is
// read by the replay slider, the map and the network; the evidence by the network and the
// map). Concurrent callers with the same key share one request; a failed request is
// dropped from the cache so Retry refetches. Keys include the Case version (updated_at),
// so a newer Case triggers a fresh load.

import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api/client";
import type { EvidenceDoc, TimelineDoc } from "../api/types";
import { useDashboard } from "../store/store";

/** Minimum gap between refetches of the selected Case's documents. A busy Case is updated
 *  by the live stream many times a second; refetching megabytes of evidence/timeline on
 *  every update would never finish and would load the server. */
export const VERSION_THROTTLE_MS = 5_000;

/**
 * ``value``, throttled per ``key``: a new key passes its value through at once, as does the
 * first change after a quiet period; further changes within ``ms`` of the last emitted one are
 * coalesced into one trailing update carrying the latest value.
 */
export function useThrottledValue<T>(value: T, key: unknown, ms = VERSION_THROTTLE_MS): T {
  const [out, setOut] = useState(value);
  const last = useRef<{ key: unknown; at: number }>({ key, at: 0 });
  useEffect(() => {
    const now = Date.now();
    if (last.current.key !== key) {
      // A new key starts with no throttle debt, so its first change is also immediate.
      last.current = { key, at: 0 };
      setOut(value);
      return;
    }
    if (now - last.current.at >= ms) {
      if (!Object.is(out, value)) {
        last.current = { key, at: now };
        setOut(value);
      }
      return;
    }
    const timer = setTimeout(() => {
      last.current = { key, at: Date.now() };
      setOut(value);
    }, last.current.at + ms - now);
    return () => clearTimeout(timer);
    // ``out`` is read only to skip no-op emits; it must not re-run the effect.
  }, [value, key, ms]);
  return last.current.key === key ? out : value;
}

/** ``updated_at`` of a Case from the store, throttled to VERSION_THROTTLE_MS; used as the
 *  cache/refetch key of the Case's documents. */
export function useCaseVersion(caseId: string | null): string | undefined {
  const version = useDashboard((s) => (caseId ? s.cases[caseId]?.updated_at : undefined));
  return useThrottledValue(version, caseId);
}

const MAX_ENTRIES = 32;
const cache = new Map<string, Promise<unknown>>();

export function cachedLoad<T>(key: string, load: () => Promise<T>): Promise<T> {
  const hit = cache.get(key);
  if (hit) return hit as Promise<T>;
  const p = load();
  cache.set(key, p);
  p.catch(() => {
    if (cache.get(key) === p) cache.delete(key);
  });
  while (cache.size > MAX_ENTRIES) cache.delete(cache.keys().next().value as string);
  return p;
}

/** Test hook: forget every cached document. */
export function clearCaseDataCache(): void {
  cache.clear();
}

export type Loaded<T> =
  | { status: "idle" }
  | { status: "loading"; key: string }
  | { status: "ready"; key: string; data: T }
  | { status: "error"; key: string; error: unknown; message: string };

export function describeError(err: unknown): string {
  return err instanceof ApiError ? err.describe() : err instanceof Error ? err.message : String(err);
}

/** True for the 503 the API_Server returns when replay has no Storage. */
export function isNoStorage(err: unknown): boolean {
  return err instanceof ApiError && err.kind === "http" && err.status === 503;
}

export function useCached<T>(key: string | null, load: () => Promise<T>, reload = 0): Loaded<T> {
  const [state, setState] = useState<Loaded<T>>({ status: "idle" });
  useEffect(() => {
    if (!key) {
      setState({ status: "idle" });
      return;
    }
    let live = true;
    setState((prev) => (prev.status === "ready" && prev.key === key ? prev : { status: "loading", key }));
    cachedLoad(key, load).then(
      (data) => live && setState({ status: "ready", key, data }),
      (error: unknown) => live && setState({ status: "error", key, error, message: describeError(error) }),
    );
    return () => {
      live = false;
    };
    // ``load`` is derived from ``key``; depending on its identity would refetch every render.
  }, [key, reload]);
  // A state left from a previous key is reported as loading for the current key.
  if (key && state.status !== "idle" && state.key !== key) return { status: "loading", key };
  return key ? state : { status: "idle" };
}

/** The timeline is read from Storage, which the server writes in batches, so a Case opened a
 *  moment ago can answer with no entries yet. An empty timeline is fetched again this often,
 *  up to EMPTY_TIMELINE_RETRIES times (a Case that keeps changing refetches anyway). */
export const EMPTY_TIMELINE_RETRY_MS = 2_000;
export const EMPTY_TIMELINE_RETRIES = 5;

export function useTimeline(caseId: string | null, version: string | undefined, reload = 0): Loaded<TimelineDoc> {
  const [retry, setRetry] = useState({ base: "", n: 0 });
  const base = `${caseId ?? ""}|${version ?? ""}`;
  const n = retry.base === base ? retry.n : 0;
  const res = useCached(caseId ? `timeline|${base}|${n}` : null, () => api.getTimeline(caseId!), reload);
  const empty = res.status === "ready" && res.data.entries.length === 0;
  useEffect(() => {
    if (!empty || n >= EMPTY_TIMELINE_RETRIES) return;
    const timer = setTimeout(() => setRetry({ base, n: n + 1 }), EMPTY_TIMELINE_RETRY_MS);
    return () => clearTimeout(timer);
  }, [empty, base, n]);
  return res;
}

export function useEvidenceDoc(caseId: string | null, version: string | undefined): Loaded<EvidenceDoc> {
  return useCached(caseId ? `evidence|${caseId}|${version ?? ""}` : null, () => api.getEvidence(caseId!));
}

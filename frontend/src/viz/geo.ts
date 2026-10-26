// Case events → map points, per-account paths and hop speeds (Req 22.5). Located events
// are grouped by SOURCE account: lat/lon is where the payer transacted, which is also how
// the Geo sensor builds its impossible-travel comparison (backend helix/sensors/agents.py).

import type { EvidenceChainDoc, TimelineEntryDoc } from "../api/types";
import { atOrBefore, toMs } from "./time";

/** Mean Earth radius used by the Geo sensor (IUGG), so distances match the evidence. */
export const EARTH_RADIUS_KM = 6371.0088;

/** A Case event as the map needs it; lat/lon are absent when the event has no location. */
export interface GeoEvent {
  tx_id: string;
  ts: string;
  account: string;
  dst?: string;
  lat?: number | null;
  lon?: number | null;
  amount?: string;
  currency?: string;
  /** Transaction is one of the Case's contributing transactions. */
  inCase?: boolean;
}

export interface GeoPoint {
  tx_id: string;
  ts: string;
  tMs: number;
  account: string;
  dst?: string;
  lat: number;
  lon: number;
  amount?: string;
  currency?: string;
  inCase: boolean;
}

export interface GeoHop {
  account: string;
  fromTx: string;
  toTx: string;
  /** [lon, lat] of the two events: one map line per hop (LineLayer). */
  from: [number, number];
  to: [number, number];
  distanceKm: number;
  dtH: number;
  /** 0 when d = 0, Infinity when dt = 0 and d > 0 (same rule as the Geo sensor). */
  speedKmh: number;
}

export interface GeoPath {
  account: string;
  txIds: string[];
  /** [lon, lat] pairs in ts order. */
  path: Array<[number, number]>;
}

export interface GeoModel {
  points: GeoPoint[];
  paths: GeoPath[];
  hops: GeoHop[];
  /** Events (at or before the replay position) left off the map for lack of location. */
  omitted: number;
  total: number;
}

export function validLocation(lat: unknown, lon: unknown): boolean {
  return (
    typeof lat === "number" &&
    typeof lon === "number" &&
    Number.isFinite(lat) &&
    Number.isFinite(lon) &&
    Math.abs(lat) <= 90 &&
    Math.abs(lon) <= 180
  );
}

const rad = (d: number) => (d * Math.PI) / 180;

export function haversineKm(lat1: number, lon1: number, lat2: number, lon2: number): number {
  const p1 = rad(lat1);
  const p2 = rad(lat2);
  const dp = p2 - p1;
  const dl = rad(lon2 - lon1);
  const h = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
  return 2 * EARTH_RADIUS_KM * Math.asin(Math.sqrt(Math.min(1, Math.max(0, h))));
}

export function impliedSpeedKmh(dKm: number, dtH: number): number {
  if (dKm <= 0) return 0;
  const dt = Math.abs(dtH);
  return dt === 0 ? Infinity : dKm / dt;
}

const byTime = (a: GeoPoint, b: GeoPoint) => a.tMs - b.tMs || (a.tx_id < b.tx_id ? -1 : a.tx_id > b.tx_id ? 1 : 0);

/** Points, per-account paths (two or more located events) and consecutive hops. Events
 *  after ``t`` are excluded entirely; the rest without a valid location are counted as
 *  omitted. A tx_id is counted once. */
export function buildGeo(events: readonly GeoEvent[], t: number | null = null): GeoModel {
  const seen = new Set<string>();
  const points: GeoPoint[] = [];
  let total = 0;
  for (const e of events) {
    if (seen.has(e.tx_id) || !atOrBefore(e.ts, t)) continue;
    seen.add(e.tx_id);
    total++;
    if (!validLocation(e.lat, e.lon)) continue;
    points.push({
      tx_id: e.tx_id,
      ts: e.ts,
      tMs: toMs(e.ts),
      account: e.account,
      dst: e.dst,
      lat: e.lat as number,
      lon: e.lon as number,
      amount: e.amount,
      currency: e.currency,
      inCase: e.inCase ?? true,
    });
  }
  points.sort(byTime);

  const byAccount = new Map<string, GeoPoint[]>();
  for (const p of points) {
    const g = byAccount.get(p.account);
    if (g) g.push(p);
    else byAccount.set(p.account, [p]);
  }
  const paths: GeoPath[] = [];
  const hops: GeoHop[] = [];
  for (const account of [...byAccount.keys()].sort()) {
    const g = byAccount.get(account)!;
    if (g.length >= 2) paths.push({ account, txIds: g.map((p) => p.tx_id), path: g.map((p) => [p.lon, p.lat]) });
    for (let i = 1; i < g.length; i++) {
      const a = g[i - 1]!;
      const b = g[i]!;
      const d = haversineKm(a.lat, a.lon, b.lat, b.lon);
      const dtH = Math.abs(b.tMs - a.tMs) / 3_600_000;
      hops.push({
        account,
        fromTx: a.tx_id,
        toTx: b.tx_id,
        from: [a.lon, a.lat],
        to: [b.lon, b.lat],
        distanceKm: d,
        dtH,
        speedKmh: impliedSpeedKmh(d, dtH),
      });
    }
  }
  return { points, paths, hops, omitted: total - points.length, total };
}

/** Map events from the Replay_Timeline; ``caseTx`` marks the Case's contributing events. */
export function eventsFromTimeline(entries: readonly TimelineEntryDoc[], caseTx?: ReadonlySet<string>): GeoEvent[] {
  return entries.map(({ event: e }) => ({
    tx_id: e.tx_id,
    ts: e.ts,
    account: e.src,
    dst: e.dst,
    lat: e.lat,
    lon: e.lon,
    amount: e.amount,
    currency: e.currency,
    inCase: caseTx ? caseTx.has(e.tx_id) : true,
  }));
}

const num = (v: unknown): number | undefined => (typeof v === "number" && Number.isFinite(v) ? v : undefined);
const str = (v: unknown): string | undefined => (typeof v === "string" && v ? v : undefined);

/**
 * Fallback when the timeline is unavailable (503, no Storage): one event per Case
 * transaction in the Evidence_Chain, located only if some entry of that transaction carries
 * numeric ``lat``/``lon`` features. The current Geo sensor records distance and speed but not
 * coordinates, so with today's backend this usually locates nothing and every event is
 * reported as omitted. The account is the ``src`` feature when present, otherwise "unknown".
 */
export function eventsFromChain(chain: EvidenceChainDoc | null | undefined): GeoEvent[] {
  const byTx = new Map<string, GeoEvent>();
  for (const e of chain?.entries ?? []) {
    if (!e.tx_id) continue;
    let ev = byTx.get(e.tx_id);
    if (!ev) {
      ev = { tx_id: e.tx_id, ts: e.ts, account: "unknown", inCase: true };
      byTx.set(e.tx_id, ev);
    }
    // A MOTIF entry is stamped with the motif's last transaction; other entries carry the
    // transaction's own ts. Keep the earliest non-motif ts.
    if (e.kind !== "MOTIF" && toMs(e.ts) < toMs(ev.ts)) ev.ts = e.ts;
    const f = e.features;
    const lat = num(f["lat"]);
    const lon = num(f["lon"]);
    if (lat !== undefined && lon !== undefined && !validLocation(ev.lat, ev.lon)) {
      ev.lat = lat;
      ev.lon = lon;
    }
    const src = str(f["src"]);
    if (src && ev.account === "unknown") ev.account = src;
  }
  return [...byTx.values()];
}

/** Bounding box [[minLon, minLat], [maxLon, maxLat]] of the points, or null. */
export function bounds(points: readonly GeoPoint[]): [[number, number], [number, number]] | null {
  if (points.length === 0) return null;
  let [x0, y0, x1, y1] = [Infinity, Infinity, -Infinity, -Infinity];
  for (const p of points) {
    x0 = Math.min(x0, p.lon);
    y0 = Math.min(y0, p.lat);
    x1 = Math.max(x1, p.lon);
    y1 = Math.max(y1, p.lat);
  }
  return [
    [x0, y0],
    [x1, y1],
  ];
}

export function formatKm(d: number): string {
  return Number.isFinite(d) ? `${d.toFixed(1)} km` : "n/a";
}

export function formatSpeed(v: number): string {
  if (v === Infinity) return "instantaneous (Δt = 0)";
  return Number.isFinite(v) ? `${Math.round(v)} km/h` : "n/a";
}

export function formatHours(h: number): string {
  return Number.isFinite(h) ? `${h.toFixed(2)} h` : "n/a";
}

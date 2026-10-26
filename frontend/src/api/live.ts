// Wires the WebSocket channels and REST snapshot into the store. Events are buffered and
// flushed every 100 ms so a fast stream re-renders the feed at most ~10 times a second while
// still showing each event well within 1 s of publication (Req 22.1).

import type { DashboardStore, SimAnalyst } from "../store/store";
import { toFeedItem, type FeedItem } from "../store/reducers";
import { api as defaultApi, type Api } from "./client";
import {
  ALERT_BUDGET,
  INVESTIGATION_COST,
  type CaseUpdatedMessage,
  type Channel,
  type MetricsMessage,
  type ScoredEventMessage,
  type StatusDoc,
} from "./types";
import { WsClient, type SocketFactory } from "./ws";

export const FLUSH_MS = 100;
/** Period of the GET /status refresh of the generator state. */
export const STATUS_POLL_MS = 10_000;

function isObj(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null;
}

const count = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? v : 0);
const numOrNull = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? v : null);

/** The optional GET /status "sim_investigator" block; null when the server does not report it. */
export function simAnalystOf(status: unknown): SimAnalyst | null {
  if (!isObj(status) || !isObj(status.sim_investigator)) return null;
  const s = status.sim_investigator;
  return {
    enabled: s.enabled === true,
    labelsTotal: count(s.labels_total),
    confirmed: count(s.confirmed),
    falsePositive: count(s.false_positive),
    errorRate: numOrNull(s.error_rate),
    intervalS: numOrNull(s.interval_s),
  };
}

/** Apply one GET /status document: generator state and the simulated investigator. */
function applyStatus(store: DashboardStore, st: StatusDoc) {
  const s = store.getState();
  s.setConfirmed({ generatorRunning: st.generator?.configured ? st.generator.running : null });
  s.setSimAnalyst(simAnalystOf(st));
}

/** Fetch the REST snapshot: Cases + backlog, confirmed config, generator state. Each part is
 *  independent, so one failing endpoint does not block the others. */
export async function refreshFromRest(store: DashboardStore, api: Pick<Api, "allCases" | "getConfig" | "getStatus"> = defaultApi) {
  const s = store.getState();
  // The Prioritiser reports pending changes by short name ("alert_budget"); accept the
  // dotted config path too. A pending value is the confirmed one for the next period.
  const pending = (p: Record<string, number>, path: string) => p[path] ?? p[path.split(".").pop() ?? path];
  await Promise.allSettled([
    api.allCases().then((r) => s.replaceCases(r.items, r.backlog_count)),
    api.getConfig().then((c) =>
      s.setConfirmed({
        alertBudget: pending(c.pending, ALERT_BUDGET) ?? c.effective[ALERT_BUDGET],
        investigationCost: pending(c.pending, INVESTIGATION_COST) ?? c.effective[INVESTIGATION_COST],
        appliesFromPeriod: Object.keys(c.pending).length ? c.current_period + 1 : null,
      }),
    ),
    api.getStatus().then((st) => applyStatus(store, st)),
  ]);
}

export interface LiveOptions {
  store: DashboardStore;
  api?: Pick<Api, "allCases" | "getConfig" | "getStatus">;
  factory?: SocketFactory;
  url?: (ch: Channel) => string;
}

export function startLive({ store, api = defaultApi, factory, url }: LiveOptions): () => void {
  let buffer: FeedItem[] = [];
  let flushTimer: ReturnType<typeof setTimeout> | null = null;
  const flush = () => {
    flushTimer = null;
    const batch = buffer;
    buffer = [];
    if (batch.length) store.getState().applyEvents(batch);
  };

  const onMessage = (ch: Channel, msg: unknown) => {
    if (!isObj(msg)) return;
    if (ch === "events" && msg.type === "event.scored" && isObj(msg.event)) {
      buffer.push(toFeedItem(msg as unknown as ScoredEventMessage));
      if (!flushTimer) flushTimer = setTimeout(flush, FLUSH_MS);
    } else if (ch === "cases" && msg.type === "case.updated" && isObj(msg.case)) {
      store.getState().applyCaseMessage(msg as unknown as CaseUpdatedMessage);
    } else if (ch === "metrics") {
      store.getState().setMetrics(msg as MetricsMessage);
    }
  };

  const client = new WsClient({
    channels: ["events", "cases", "metrics"],
    factory,
    url,
    onMessage,
    onStatus: (st) => store.getState().setConnection(st),
    onReconnect: () => void refreshFromRest(store, api),
  });
  void refreshFromRest(store, api);
  client.connect();
  // The generator can be started/stopped from another tab or client; keep its state current.
  const statusTimer = setInterval(() => {
    api
      .getStatus()
      .then((st) => applyStatus(store, st))
      .catch(() => undefined);
  }, STATUS_POLL_MS);

  return () => {
    clearInterval(statusTimer);
    client.stop();
    if (flushTimer) clearTimeout(flushTimer);
    flush();
  };
}

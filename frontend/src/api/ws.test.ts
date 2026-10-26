import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createDashboardStore } from "../store/store";
import { caseSummary, scored } from "../test/fixtures";
import { backoffDelay } from "./backoff";
import { startLive } from "./live";
import type { Channel } from "./types";
import { ReconnectingSocket, WsClient, type SocketLike } from "./ws";

class FakeSocket implements SocketLike {
  onopen: ((ev: unknown) => void) | null = null;
  onclose: ((ev: unknown) => void) | null = null;
  onerror: ((ev: unknown) => void) | null = null;
  onmessage: ((ev: { data: unknown }) => void) | null = null;
  closed = false;
  constructor(readonly url: string) {}
  open() {
    this.onopen?.({});
  }
  drop() {
    this.onclose?.({});
  }
  send(msg: unknown) {
    this.onmessage?.({ data: JSON.stringify(msg) });
  }
  close() {
    this.closed = true;
  }
}

function harness() {
  const sockets: FakeSocket[] = [];
  const factory = (url: string) => {
    const s = new FakeSocket(url);
    sockets.push(s);
    return s;
  };
  return { sockets, factory, last: () => sockets[sockets.length - 1]! };
}

describe("backoffDelay", () => {
  it("is 1 s, doubling, capped at 30 s", () => {
    expect(Array.from({ length: 9 }, (_, i) => backoffDelay(i))).toEqual([
      1000, 2000, 4000, 8000, 16000, 30000, 30000, 30000, 30000,
    ]);
    expect(backoffDelay(1000)).toBe(30000);
    expect(backoffDelay(-1)).toBe(1000);
  });

  it("is monotone non-decreasing and within [1 s, 30 s] for every attempt", () => {
    for (let a = 0; a < 200; a++) {
      const d = backoffDelay(a);
      expect(d).toBeGreaterThanOrEqual(1000);
      expect(d).toBeLessThanOrEqual(30000);
      expect(d).toBeGreaterThanOrEqual(backoffDelay(a - 1 < 0 ? 0 : a - 1));
    }
  });
});

describe("ReconnectingSocket", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("reports down at once and retries after 1, 2, 4, 8, 16, 30, 30 s", () => {
    const h = harness();
    const statuses: string[] = [];
    const rs = new ReconnectingSocket({ url: "ws://x/ws/events", factory: h.factory, onMessage: () => {}, onStatus: (s) => statuses.push(s) });
    rs.connect();
    h.last().open();
    expect(statuses).toEqual(["open"]);

    h.last().drop();
    expect(statuses.at(-1)).toBe("down"); // synchronous: well within the 2 s budget

    const expected = [1000, 2000, 4000, 8000, 16000, 30000, 30000];
    for (const delay of expected) {
      const before = h.sockets.length;
      vi.advanceTimersByTime(delay - 1);
      expect(h.sockets.length).toBe(before); // not yet
      vi.advanceTimersByTime(1);
      expect(h.sockets.length).toBe(before + 1); // reconnect attempted exactly at the delay
      h.last().drop(); // attempt fails
      expect(rs.status).toBe("down");
    }
    rs.stop();
  });

  it("resets the backoff after a successful reconnect", () => {
    const h = harness();
    const rs = new ReconnectingSocket({ url: "u", factory: h.factory, onMessage: () => {}, onStatus: () => {} });
    rs.connect();
    h.last().drop();
    vi.advanceTimersByTime(1000);
    h.last().drop();
    vi.advanceTimersByTime(2000);
    h.last().open();
    expect(rs.status).toBe("open");
    h.last().drop();
    const before = h.sockets.length;
    vi.advanceTimersByTime(1000);
    expect(h.sockets.length).toBe(before + 1);
    rs.stop();
  });

  it("does not reconnect after stop", () => {
    const h = harness();
    const rs = new ReconnectingSocket({ url: "u", factory: h.factory, onMessage: () => {}, onStatus: () => {} });
    rs.connect();
    h.last().open();
    rs.stop();
    expect(h.last().closed).toBe(true);
    vi.advanceTimersByTime(60_000);
    expect(h.sockets).toHaveLength(1);
  });
});

describe("WsClient + startLive", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("calls onReconnect only once every channel is open again after a drop", () => {
    const h = harness();
    const onReconnect = vi.fn();
    const statuses: string[] = [];
    const client = new WsClient({
      channels: ["events", "cases"],
      factory: h.factory,
      url: (ch) => ch,
      onMessage: () => {},
      onStatus: (s) => statuses.push(s),
      onReconnect,
    });
    client.connect();
    h.sockets.forEach((s) => s.open());
    expect(client.status).toBe("open");
    expect(onReconnect).not.toHaveBeenCalled(); // first open is not a reconnect

    h.sockets[0]!.drop();
    expect(client.status).toBe("down");
    vi.advanceTimersByTime(1000);
    h.last().open();
    expect(statuses).toEqual(["open", "down", "open"]);
    expect(onReconnect).toHaveBeenCalledTimes(1);
    client.stop();
  });

  it("keeps last data on disconnect, shows down, and refetches REST on reconnect", async () => {
    const h = harness();
    const store = createDashboardStore();
    const api = {
      allCases: vi.fn(async () => ({ items: [caseSummary("C-7")], backlog_count: 2 })),
      getConfig: vi.fn(async () => ({
        effective: { "prioritiser.alert_budget": 20, "prioritiser.investigation_cost": 50 },
        pending: { alert_budget: 30 } as Record<string, number>, // server uses short names here
        current_period: 0,
      })),
      getStatus: vi.fn(async () => ({ status: "ok", generator: { configured: true, running: false } })),
    };
    const sock = (ch: Channel) => h.sockets.filter((s) => s.url === ch).at(-1)!;
    const stop = startLive({ store, api, factory: h.factory, url: (ch) => ch });
    await vi.waitFor(() => expect(api.allCases).toHaveBeenCalledTimes(1)); // initial snapshot

    h.sockets.forEach((s) => s.open());
    expect(store.getState().connection).toBe("open");
    sock("events").send(scored(1));
    vi.advanceTimersByTime(100); // feed flush interval
    expect(store.getState().feed).toHaveLength(1);

    sock("cases").drop();
    expect(store.getState().connection).toBe("down");
    expect(store.getState().feed).toHaveLength(1); // last data retained

    vi.advanceTimersByTime(1000);
    sock("cases").open();
    expect(store.getState().connection).toBe("open");
    await vi.waitFor(() => expect(api.allCases).toHaveBeenCalledTimes(2));
    expect(api.getConfig).toHaveBeenCalledTimes(2);
    await vi.waitFor(() => expect(store.getState().queue).toEqual(["C-7"]));
    expect(store.getState().confirmed.alertBudget).toBe(30); // pending value wins over effective
    expect(store.getState().confirmed.investigationCost).toBe(50);
    expect(store.getState().confirmed.appliesFromPeriod).toBe(1);
    expect(store.getState().confirmed.generatorRunning).toBe(false);
    stop();
  });
});

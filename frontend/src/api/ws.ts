// WebSocket client (design §22): one reconnecting socket per channel, aggregated into a
// single connection status. On drop the status goes "down" at once (Req 22.12) and the last
// data stays in the store; reconnects follow backoffDelay; when every channel is open again
// after a drop, onReconnect fires so the caller can refetch REST state (Req 22.13).

import { backoffDelay } from "./backoff";
import type { Channel } from "./types";

export type SocketStatus = "connecting" | "open" | "down";

/** Minimal WebSocket surface used here, so tests can inject a fake. */
export interface SocketLike {
  onopen: ((ev: unknown) => void) | null;
  onclose: ((ev: unknown) => void) | null;
  onerror: ((ev: unknown) => void) | null;
  onmessage: ((ev: { data: unknown }) => void) | null;
  close(): void;
}

export type SocketFactory = (url: string) => SocketLike;

export const browserSocketFactory: SocketFactory = (url) => new WebSocket(url) as unknown as SocketLike;

export function channelUrl(channel: Channel, loc: Pick<Location, "protocol" | "host"> = window.location): string {
  const scheme = loc.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${loc.host}/ws/${channel}`;
}

export interface ReconnectingSocketOptions {
  url: string;
  factory: SocketFactory;
  onMessage: (msg: unknown) => void;
  onStatus: (s: SocketStatus) => void;
}

export class ReconnectingSocket {
  status: SocketStatus = "connecting";
  /** Failed attempts since the last successful open; drives the backoff delay. */
  attempt = 0;
  private ws: SocketLike | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private stopped = false;

  constructor(private readonly opts: ReconnectingSocketOptions) {}

  connect(): void {
    if (this.stopped || this.ws) return;
    let ws: SocketLike;
    try {
      ws = this.opts.factory(this.opts.url);
    } catch {
      this.handleClose();
      return;
    }
    this.ws = ws;
    ws.onopen = () => {
      this.attempt = 0;
      this.setStatus("open");
    };
    ws.onmessage = (ev) => {
      if (typeof ev.data !== "string") return;
      let msg: unknown;
      try {
        msg = JSON.parse(ev.data);
      } catch {
        return; // ignore malformed frames; the server only sends JSON
      }
      this.opts.onMessage(msg);
    };
    ws.onerror = () => {
      /* a close event always follows */
    };
    ws.onclose = () => {
      if (this.ws === ws) this.handleClose();
    };
  }

  /** Delay of the currently scheduled reconnect, for display/tests. */
  nextDelay(): number {
    return backoffDelay(this.attempt);
  }

  stop(): void {
    this.stopped = true;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    const ws = this.ws;
    this.ws = null;
    if (ws) {
      ws.onclose = null;
      ws.close();
    }
  }

  private handleClose(): void {
    this.ws = null;
    if (this.stopped) return;
    this.setStatus("down");
    const delay = backoffDelay(this.attempt);
    this.attempt += 1;
    this.timer = setTimeout(() => {
      this.timer = null;
      this.connect();
    }, delay);
  }

  private setStatus(s: SocketStatus): void {
    if (this.status === s) return;
    this.status = s;
    this.opts.onStatus(s);
  }
}

export function aggregateStatus(statuses: SocketStatus[]): SocketStatus {
  if (statuses.some((s) => s === "down")) return "down";
  if (statuses.length > 0 && statuses.every((s) => s === "open")) return "open";
  return "connecting";
}

export interface WsClientOptions {
  channels: Channel[];
  factory?: SocketFactory;
  url?: (ch: Channel) => string;
  onMessage: (ch: Channel, msg: unknown) => void;
  onStatus: (s: SocketStatus) => void;
  /** Called when all channels are open again after having been down. */
  onReconnect: () => void;
}

export class WsClient {
  status: SocketStatus = "connecting";
  readonly sockets: Map<Channel, ReconnectingSocket> = new Map();
  private wasDown = false;

  constructor(private readonly opts: WsClientOptions) {
    const factory = opts.factory ?? browserSocketFactory;
    const url = opts.url ?? ((ch: Channel) => channelUrl(ch));
    for (const ch of opts.channels) {
      this.sockets.set(
        ch,
        new ReconnectingSocket({
          url: url(ch),
          factory,
          onMessage: (m) => opts.onMessage(ch, m),
          onStatus: () => this.recompute(),
        }),
      );
    }
  }

  connect(): void {
    for (const s of this.sockets.values()) s.connect();
  }

  stop(): void {
    for (const s of this.sockets.values()) s.stop();
  }

  private recompute(): void {
    const next = aggregateStatus([...this.sockets.values()].map((s) => s.status));
    if (next === this.status) return;
    const prev = this.status;
    this.status = next;
    if (next === "down") this.wasDown = true;
    this.opts.onStatus(next);
    if (next === "open" && (this.wasDown || prev === "down")) {
      this.wasDown = false;
      this.opts.onReconnect();
    }
  }
}

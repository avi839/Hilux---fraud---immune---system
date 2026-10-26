"""WebSocket pub/sub hub (design §21, Concurrency model).

Every connected client owns a bounded ``asyncio.Queue``. ``publish`` never blocks: when a
client's queue is full its oldest message is dropped (and counted), so a slow client cannot
stall the pipeline worker. With no clients, publishing is a no-op (Req 1.13).

``publish`` may be called from the event loop (the pipeline worker, request handlers) or
from another thread; off-loop calls are handed to the loop with ``call_soon_threadsafe``.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Mapping
from typing import Any, Optional

DEFAULT_QUEUE_SIZE = 256


class Client:
    __slots__ = ("channel", "queue", "dropped", "sent")

    def __init__(self, channel: str, maxsize: int) -> None:
        self.channel = channel
        self.queue: asyncio.Queue[Mapping[str, Any]] = asyncio.Queue(maxsize)
        self.dropped = 0
        self.sent = 0

    def offer(self, msg: Mapping[str, Any]) -> None:
        """Enqueue ``msg``, dropping the oldest queued messages while the queue is full."""
        q = self.queue
        while q.full():
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - full() and get raced
                break
            self.dropped += 1
        q.put_nowait(msg)


class WSHub:
    def __init__(self, maxsize: int = DEFAULT_QUEUE_SIZE) -> None:
        if maxsize < 1:
            raise ValueError("queue size must be >= 1")
        self.maxsize = int(maxsize)
        self._clients: dict[str, set[Client]] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[int] = None
        self.published = 0

    def bind(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """Remember the serving loop (called from inside it at startup)."""
        self._loop = loop if loop is not None else asyncio.get_running_loop()
        self._thread = threading.get_ident()

    def register(self, channel: str) -> Client:
        c = Client(channel, self.maxsize)
        self._clients.setdefault(channel, set()).add(c)
        return c

    def unregister(self, client: Client) -> None:
        self._clients.get(client.channel, set()).discard(client)

    def subscribers(self, channel: str) -> int:
        return len(self._clients.get(channel, ()))

    def publish(self, channel: str, msg: Mapping[str, Any]) -> None:
        if not self._clients.get(channel):
            return
        loop = self._loop
        if loop is not None and self._thread != threading.get_ident() and not loop.is_closed():
            loop.call_soon_threadsafe(self._deliver, channel, msg)
        else:
            self._deliver(channel, msg)

    def _deliver(self, channel: str, msg: Mapping[str, Any]) -> None:
        self.published += 1
        for c in list(self._clients.get(channel, ())):
            c.offer(msg)

    def stats(self) -> dict[str, Any]:
        return {
            "clients": {ch: len(cs) for ch, cs in sorted(self._clients.items())},
            "dropped": sum(c.dropped for cs in self._clients.values() for c in cs),
            "published": self.published,
            "queue_size": self.maxsize,
        }


__all__ = ["DEFAULT_QUEUE_SIZE", "Client", "WSHub"]

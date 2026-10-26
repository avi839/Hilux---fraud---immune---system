"""Stream_Ingestor: exactly-once, per-account-ordered admission with backpressure.

Covers Req 1.1-1.10, 2.8 (design §2).

- Dedup runs at `submit`, before enqueue: `seen[tx_id] = receipt time` plus a min-heap of
  `(receipt time, tx_id)`; entries older than `dedup_ttl_s` on the SimClock are evicted.
- Ordering: a FIFO of account tokens (one per queued event) plus a per-account min-heap keyed
  `(ts, arrival_seq)`. Popping a token for account A yields A's minimum, so the queued events of
  one account leave in non-decreasing ts with arrival-order ties.
- Lateness is classified at delivery against `latest[A]`, the latest delivered ts of A.
- Backpressure hysteresis: set when queue length > limit, cleared when length <= limit // 2.
  Events are never dropped.

The ingestor is not thread-safe; it is meant to be driven from one asyncio event loop. With a
single consumer (the pipeline worker) at most one event per account is in the loop at a time
(Req 1.3).

Auth_Events (Req 28.1, 28.3): ``submit`` also takes ``AuthEvent`` objects and ``submit_auth``
parses Auth_Event JSON (rejects go to the same rejected-events log). Auth_Events share the
per-account queue (keyed by ``account_id``), so an account's logins and payments leave in
timestamp order. They have their own dedup namespace (same TTL) and their own counters
(``auth_counters``), are never classified for lateness and never move ``latest[A]``, so
Transaction_Event behaviour and counters are unchanged by auth traffic.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional, Union

from helix.codec import AuthEvent, CodecError, TransactionEvent, parse, parse_auth
from helix.config.schema import IngestConfig
from helix.sim.clock import ManualClock, SimClock

from .rejected_log import InMemoryRejectedLog, RejectedLog, RejectedRecord

Raw = Union[str, bytes, bytearray, TransactionEvent, AuthEvent]
TRANSACTION = "transaction"
AUTH = "auth"


class IngestStatus(str, Enum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class IngestResult:
    status: IngestStatus
    tx_id: Optional[str] = None  # None when the document was rejected
    error: Optional[CodecError] = None  # set only when REJECTED
    backpressure: bool = False  # flag after this submit (REST returns it as X-Backpressure)
    kind: str = TRANSACTION  # "auth" for Auth_Events; ``tx_id`` then holds the event_id

    @property
    def accepted(self) -> bool:
        return self.status is IngestStatus.ACCEPTED


class LatenessKind(str, Enum):
    IN_ORDER = "in_order"
    OUT_OF_ORDER = "out_of_order"  # 0 < lag <= tolerance
    LATE = "late"  # lag > tolerance


@dataclass(frozen=True, slots=True)
class Lateness:
    kind: LatenessKind
    seconds: float = 0.0  # lag behind the latest delivered ts of the account; 0 unless LATE

    @property
    def late(self) -> bool:
        return self.kind is LatenessKind.LATE


IN_ORDER = Lateness(LatenessKind.IN_ORDER)


@dataclass(frozen=True, slots=True)
class AdmittedEvent:
    event: TransactionEvent  # or an AuthEvent (only when Auth_Events are submitted)
    lateness: Lateness
    arrival_seq: int  # admission order across all accounts
    received_at: datetime  # SimClock time of receipt


@dataclass(slots=True)
class IngestCounters:
    accepted: int = 0
    delivered: int = 0
    malformed: int = 0
    duplicate: int = 0
    out_of_order: int = 0
    late: int = 0


@dataclass(slots=True)
class AuthIngestCounters:
    accepted: int = 0
    delivered: int = 0
    malformed: int = 0
    duplicate: int = 0


def _as_text(raw: object) -> str:
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw).decode("utf-8", errors="replace")
    return raw if isinstance(raw, str) else repr(raw)


class StreamIngestor:
    def __init__(
        self,
        config: Optional[IngestConfig] = None,
        clock: Optional[SimClock] = None,
        rejected_log: Optional[RejectedLog] = None,
    ):
        self._cfg = config if config is not None else IngestConfig()
        self._clock: SimClock = clock if clock is not None else ManualClock()
        self.rejected_log: RejectedLog = rejected_log if rejected_log is not None else InMemoryRejectedLog()
        self._counters = IngestCounters()
        self._auth_counters = AuthIngestCounters()
        # Dedup
        self._seen: dict[str, datetime] = {}
        self._seen_heap: list[tuple[datetime, str]] = []
        self._seen_auth: dict[str, datetime] = {}
        self._seen_auth_heap: list[tuple[datetime, str]] = []
        # Ordering
        self._tokens: deque[str] = deque()
        self._heaps: dict[str, list[tuple[datetime, int, Union[TransactionEvent, AuthEvent], datetime]]] = {}
        self._seq = itertools.count()
        self._len = 0
        # Lateness
        self._latest: dict[str, datetime] = {}
        # Backpressure and async consumers
        self._backpressure = False
        self._waiters: list[asyncio.Future[None]] = []

    # ------------------------------------------------------------------ state

    @property
    def config(self) -> IngestConfig:
        return self._cfg

    @config.setter
    def config(self, cfg: IngestConfig) -> None:
        """Swap config at runtime (e.g. PATCH /config); re-evaluates the backpressure flag."""
        self._cfg = cfg
        self._update_backpressure()

    @property
    def backpressure(self) -> bool:
        return self._backpressure

    @property
    def counters(self) -> IngestCounters:
        """A snapshot copy; mutating it does not affect the ingestor."""
        return replace(self._counters)

    @property
    def auth_counters(self) -> AuthIngestCounters:
        """Auth_Event counters (snapshot copy); Transaction_Event counters exclude auth traffic."""
        return replace(self._auth_counters)

    def __len__(self) -> int:
        return self._len

    queue_length = property(__len__)

    def latest_processed(self, account: str) -> Optional[datetime]:
        return self._latest.get(account)

    # ----------------------------------------------------------------- submit

    def submit_auth(self, raw: Union[str, bytes, bytearray, AuthEvent]) -> IngestResult:
        """Admit one Auth_Event (object or JSON document, Req 28.1, 28.3)."""
        now = self._clock.now()
        if isinstance(raw, AuthEvent):
            return self._admit_auth(raw, now)
        parsed = parse_auth(raw)  # never raises
        if isinstance(parsed, CodecError):
            self._auth_counters.malformed += 1
            return self._reject(raw, parsed, now, kind=AUTH)
        return self._admit_auth(parsed, now)

    def _admit_auth(self, ev: AuthEvent, now: datetime) -> IngestResult:
        self._evict(now)
        if ev.event_id in self._seen_auth:
            self._auth_counters.duplicate += 1
            return IngestResult(IngestStatus.DUPLICATE, ev.event_id, backpressure=self._backpressure, kind=AUTH)
        self._seen_auth[ev.event_id] = now
        heapq.heappush(self._seen_auth_heap, (now, ev.event_id))
        self._enqueue(ev.account_id, ev.ts, ev, now)
        self._auth_counters.accepted += 1
        self._update_backpressure()
        self._wake()
        return IngestResult(IngestStatus.ACCEPTED, ev.event_id, backpressure=self._backpressure, kind=AUTH)

    def _enqueue(self, account: str, ts: datetime, ev: Union[TransactionEvent, AuthEvent], now: datetime) -> None:
        heap = self._heaps.get(account)
        if heap is None:
            heap = self._heaps[account] = []
        heapq.heappush(heap, (ts, next(self._seq), ev, now))
        self._tokens.append(account)
        self._len += 1

    def submit(self, raw: Raw) -> IngestResult:
        now = self._clock.now()
        if isinstance(raw, AuthEvent):
            return self._admit_auth(raw, now)
        if isinstance(raw, TransactionEvent):
            ev = raw
        else:
            parsed = parse(raw)  # type: ignore[arg-type]  # parse never raises
            if isinstance(parsed, CodecError):
                return self._reject(raw, parsed, now)
            ev = parsed

        self._evict(now)
        if ev.tx_id in self._seen:
            self._counters.duplicate += 1
            return IngestResult(IngestStatus.DUPLICATE, ev.tx_id, backpressure=self._backpressure)

        self._seen[ev.tx_id] = now
        heapq.heappush(self._seen_heap, (now, ev.tx_id))
        self._enqueue(ev.src, ev.ts, ev, now)
        self._counters.accepted += 1
        self._update_backpressure()
        self._wake()
        return IngestResult(IngestStatus.ACCEPTED, ev.tx_id, backpressure=self._backpressure)

    def _reject(self, raw: object, err: CodecError, now: datetime, kind: str = TRANSACTION) -> IngestResult:
        if kind == TRANSACTION:
            self._counters.malformed += 1
        self.rejected_log.append(
            RejectedRecord(raw=_as_text(raw), received_at=now, fields=tuple(sorted(err.fields)), reason=str(err))
        )
        return IngestResult(IngestStatus.REJECTED, error=err, backpressure=self._backpressure, kind=kind)

    def _evict(self, now: datetime) -> None:
        # Keep entries whose age <= dedup_ttl_s; drop older ones.
        cutoff = now - timedelta(seconds=self._cfg.dedup_ttl_s)
        for heap, seen in ((self._seen_heap, self._seen), (self._seen_auth_heap, self._seen_auth)):
            while heap and heap[0][0] < cutoff:
                seen_at, key = heapq.heappop(heap)
                if seen.get(key) == seen_at:
                    del seen[key]

    # ---------------------------------------------------------------- deliver

    def pop(self) -> Optional[AdmittedEvent]:
        """Synchronous delivery of the next event, or None if the queue is empty."""
        if not self._tokens:
            return None
        account = self._tokens.popleft()
        heap = self._heaps[account]
        ts, seq, ev, received_at = heapq.heappop(heap)
        if not heap:
            del self._heaps[account]
        self._len -= 1
        if isinstance(ev, AuthEvent):
            self._auth_counters.delivered += 1
            self._update_backpressure()
            return AdmittedEvent(ev, IN_ORDER, seq, received_at)  # type: ignore[arg-type]
        self._counters.delivered += 1
        lateness = self._classify(account, ts)
        self._update_backpressure()
        return AdmittedEvent(ev, lateness, seq, received_at)

    def drain(self) -> list[AdmittedEvent]:
        out = []
        while (item := self.pop()) is not None:
            out.append(item)
        return out

    async def next_event(self) -> AdmittedEvent:
        """Wait for and deliver the next event (pipeline worker entry point)."""
        while True:
            item = self.pop()
            if item is not None:
                return item
            fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            self._waiters.append(fut)
            try:
                await fut
            finally:
                if fut in self._waiters:
                    self._waiters.remove(fut)

    def _wake(self) -> None:
        # Wake every waiter; each re-checks the queue, so a cancelled waiter cannot lose a wake-up.
        waiters, self._waiters = self._waiters, []
        for fut in waiters:
            if not fut.done():
                fut.set_result(None)

    def _classify(self, account: str, ts: datetime) -> Lateness:
        latest = self._latest.get(account)
        if latest is None or ts >= latest:
            self._latest[account] = ts
            return IN_ORDER
        lag = (latest - ts).total_seconds()
        if lag > self._cfg.lateness_tolerance_s:
            self._counters.late += 1
            return Lateness(LatenessKind.LATE, lag)
        self._counters.out_of_order += 1
        return Lateness(LatenessKind.OUT_OF_ORDER)

    def _update_backpressure(self) -> None:
        limit = self._cfg.backpressure_limit
        if self._len > limit:
            self._backpressure = True
        elif self._len <= limit // 2:
            self._backpressure = False

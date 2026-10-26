"""Immune miss-detector (design §18; Req 21.1, 21.15).

The only Immune_System module allowed to read ground truth. It records each undetected
Attack_Campaign event at most once, with the event id, the campaign id and the miss time,
and hands only the event id to ``on_miss``; the Immune_System proper never sees a label.

Usage: call ``observe(tx_id, ts)`` for every ingested event and, periodically,
``record_misses(now - detection_deadline, now=now)``. An attack event (a FRAUD label with a
campaign id) is a miss when it has been observed, its ``ts <= deadline`` and
``is_detected(tx_id)`` is False at that check; if it is detected at that check it is settled
and never becomes a miss. Labels are pulled incrementally (only when the reader has grown),
so a check costs O(pending attack events), not O(all labels).

Memory: observed timestamps are kept only until the event's deadline has been checked. The
Attacker_Agent writes each label *before* it injects the event (Req 20.10), so by the time an
attack event's deadline passes its label has been pulled; a label written after that check
(not something the attacker does) is ignored.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

from helix.evaluation.ground_truth import GroundTruthReader

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Miss:
    event_id: str
    campaign_id: str
    miss_time: datetime


class MissDetector:
    def __init__(
        self,
        reader: GroundTruthReader,
        is_detected: Callable[[str], bool],
        on_miss: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._reader = reader
        self._is_detected = is_detected
        self._on_miss = on_miss
        self._seen: dict[str, datetime] = {}  # tx_id -> event ts (until its deadline is checked)
        self._order: deque[tuple[datetime, str]] = deque()  # observe order, for pruning _seen
        self._recorded: set[str] = set()
        self._attack: dict[str, str] = {}  # pending attack events: event_id -> campaign_id
        self._settled: set[str] = set()  # detected by their deadline: never a miss
        self._cursor: Optional[datetime] = None
        self._last_len: Optional[int] = None
        self.misses: list[Miss] = []
        self.hook_errors = 0

    def set_on_miss(self, fn: Optional[Callable[[str], None]]) -> None:
        self._on_miss = fn

    def observe(self, tx_id: str, ts: datetime) -> None:
        if tx_id in self._seen or tx_id in self._recorded or tx_id in self._settled:
            return
        self._seen[tx_id] = ts
        self._order.append((ts, tx_id))

    def _pull(self) -> None:
        n = len(self._reader)
        if n == self._last_len:
            return
        latest = self._cursor
        for v in self._reader.labels_since(self._cursor):
            if v.written_at is not None and (latest is None or v.written_at > latest):
                latest = v.written_at
            if v.event_id in self._recorded or v.event_id in self._settled:
                continue
            if v.is_fraud and v.campaign_id is not None:
                self._attack[v.event_id] = v.campaign_id
            else:
                self._attack.pop(v.event_id, None)
        self._cursor, self._last_len = latest, n

    def record_misses(self, deadline: datetime, now: Optional[datetime] = None) -> list[Miss]:
        """Record attack events with ``ts <= deadline`` (i.e. now - detection deadline) that
        are not Detected_Events. Each event is recorded once; ``miss_time`` is ``now``
        (``deadline`` when not given)."""
        self._pull()
        at = deadline if now is None else now
        out: list[Miss] = []
        for eid in sorted(self._attack):
            ts = self._seen.get(eid)
            if ts is None or ts > deadline:
                continue
            cid = self._attack.pop(eid)
            if self._is_detected(eid):
                self._settled.add(eid)
                continue
            self._recorded.add(eid)
            m = Miss(eid, cid, at)
            out.append(m)
            self.misses.append(m)
            if self._on_miss is not None:
                try:
                    self._on_miss(eid)  # only the event id crosses the boundary
                except Exception:  # noqa: BLE001 - one failing trigger must not lose later misses
                    self.hook_errors += 1
                    log.exception("miss detector: on_miss failed for %s", eid)
        self._prune(deadline)
        return out

    def _prune(self, deadline: datetime) -> None:
        """Forget timestamps of events whose deadline has been checked (all pending attack
        events with ``ts <= deadline`` were resolved above)."""
        order, seen = self._order, self._seen
        while order and order[0][0] <= deadline:
            _, tx = order.popleft()
            if tx not in self._attack:  # always true: kept as a guard
                seen.pop(tx, None)

    @property
    def pending(self) -> int:
        """Attack events whose deadline has not been checked yet."""
        return len(self._attack)

    @property
    def settled(self) -> frozenset[str]:
        """Attack events that were Detected_Events at their deadline check."""
        return frozenset(self._settled)

    @property
    def recorded(self) -> frozenset[str]:
        return frozenset(self._recorded)


__all__ = ["Miss", "MissDetector"]

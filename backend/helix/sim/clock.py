"""SimClock: the single source of stream time for every HELIX component."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Protocol, runtime_checkable

_ZERO = timedelta(0)


def _require_aware_utc(t: datetime) -> datetime:
    if t.tzinfo is None or t.utcoffset() is None:
        raise ValueError("SimClock times must be timezone-aware")
    return t.astimezone(timezone.utc)


def _require_non_negative(dt: timedelta) -> None:
    if dt < _ZERO:
        raise ValueError(f"cannot advance a clock backwards (dt={dt})")


@runtime_checkable
class SimClock(Protocol):
    def now(self) -> datetime:
        """Current stream time as an aware UTC datetime; never decreases."""
        ...

    def advance(self, dt: timedelta) -> None:
        """Move stream time forward by `dt` (>= 0); used by tests and fast replay."""
        ...


class ManualClock:
    """Deterministic clock that only moves when told to."""

    def __init__(self, start: Optional[datetime] = None):
        self._t = _require_aware_utc(start) if start is not None else datetime(2024, 1, 1, tzinfo=timezone.utc)
        self._lock = threading.Lock()

    def now(self) -> datetime:
        return self._t

    def advance(self, dt: timedelta) -> None:
        _require_non_negative(dt)
        with self._lock:
            self._t = self._t + dt

    def set(self, t: datetime) -> None:
        """Jump to `t`; raises if `t` is earlier than the current time."""
        t = _require_aware_utc(t)
        with self._lock:
            if t < self._t:
                raise ValueError(f"cannot move clock backwards from {self._t} to {t}")
            self._t = t


class WallClock:
    """Real UTC time plus an accumulated offset from `advance`.

    `now()` is clamped to be non-decreasing even if the system clock steps back.
    """

    def __init__(self, source: Optional[Callable[[], datetime]] = None):
        self._source = source or (lambda: datetime.now(timezone.utc))
        self._offset = _ZERO
        self._last: Optional[datetime] = None
        self._lock = threading.Lock()

    def now(self) -> datetime:
        with self._lock:
            t = _require_aware_utc(self._source()) + self._offset
            if self._last is not None and t < self._last:
                t = self._last
            self._last = t
            return t

    def advance(self, dt: timedelta) -> None:
        _require_non_negative(dt)
        with self._lock:
            self._offset += dt

"""WorkloadMeter (design §26, Req 41.10): investigator throughput on a monotonic wall clock.

Feedback_Labels are counted per Investigator (``label_by``) from a feedback hook; AUTO_CLEARED
closures from the Auto_Triage sweep. The trailing window is ``(now - 3600, now]`` seconds of
wall-clock time (human throughput, unlike the stream-time triage idle time). Entries older
than the window are evicted as the clock advances, so memory is bounded by one hour of
activity; the totals count every entry since start.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from typing import Any, Optional

WINDOW_S = 3_600.0


class WorkloadMeter:
    def __init__(self, now: Callable[[], float] = time.monotonic, window_s: float = WINDOW_S) -> None:
        self.now = now
        self.window_s = float(window_s)
        self._labels: deque[tuple[float, str]] = deque()
        self._clears: deque[float] = deque()
        self.labelled_total = 0
        self.auto_cleared_total = 0
        self.bulk_requests_total = 0

    # --- feeding -----------------------------------------------------------------------

    def on_label(self, investigator: Optional[str], at: Optional[float] = None) -> None:
        t = self.now() if at is None else float(at)
        self._labels.append((t, str(investigator or "unknown")))
        self.labelled_total += 1
        self._evict(t)

    def on_auto_clear(self, at: Optional[float] = None) -> None:
        t = self.now() if at is None else float(at)
        self._clears.append(t)
        self.auto_cleared_total += 1
        self._evict(t)

    def on_bulk_request(self) -> None:
        self.bulk_requests_total += 1

    def feedback_hook(self) -> Callable[[Any, Any], None]:
        """A ``Helix.feedback_hooks`` callable: one count per stored Feedback_Label."""

        def hook(case: Any, label: Any) -> None:
            self.on_label(getattr(case, "label_by", None) or getattr(case, "assignee", None))

        hook.workload_meter = self  # type: ignore[attr-defined]
        return hook

    # --- reading -----------------------------------------------------------------------

    def _evict(self, now: float) -> None:
        cutoff = now - self.window_s
        while self._labels and self._labels[0][0] <= cutoff:
            self._labels.popleft()
        while self._clears and self._clears[0] <= cutoff:
            self._clears.popleft()

    def snapshot(self, now: Optional[float] = None) -> dict[str, Any]:
        t = self.now() if now is None else float(now)
        self._evict(t)
        lo = t - self.window_s
        by: dict[str, int] = {}
        for ts, who in self._labels:
            if lo < ts <= t:
                by[who] = by.get(who, 0) + 1
        return {
            "labelled_last_hour": sum(by.values()),
            "labelled_last_hour_by": dict(sorted(by.items())),
            "labelled_total": self.labelled_total,
            "auto_cleared_total": self.auto_cleared_total,
            "auto_cleared_last_hour": sum(1 for ts in self._clears if lo < ts <= t),
            "bulk_requests_total": self.bulk_requests_total,
        }


__all__ = ["WINDOW_S", "WorkloadMeter"]

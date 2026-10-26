"""Replay service errors (Req 17.6, 17.7). None of them changes any HELIX state."""

from __future__ import annotations

from datetime import datetime


class ReplayError(ValueError):
    """Base class for replay request errors."""


class UnknownCaseError(ReplayError):
    def __init__(self, case_id: str) -> None:
        super().__init__(f"unknown case id {case_id!r}")
        self.case_id = case_id


class UnknownAccountError(ReplayError):
    """The account is not a node of the returned Related_Subgraph (Req 17.6)."""

    def __init__(self, account: str) -> None:
        super().__init__(f"account {account!r} is not in the related subgraph")
        self.account = account


class TimestampOutOfRangeError(ReplayError):
    """T is outside the Replay_Timeline span; reports the valid span (Req 17.7)."""

    def __init__(self, t: datetime, start: datetime, end: datetime) -> None:
        super().__init__(
            f"t={t.isoformat()} is outside the replay timeline "
            f"[{start.isoformat()}, {end.isoformat()}]"
        )
        self.t = t
        self.start = start
        self.end = end


class ReplayParameterError(ReplayError):
    """A hop depth / max node count / lead time outside its configured range (Req 17.9)."""

    def __init__(self, parameter: str, value: object, lo: float, hi: float) -> None:
        super().__init__(f"{parameter}={value!r} is outside [{lo:g}, {hi:g}]")
        self.parameter = parameter
        self.value = value

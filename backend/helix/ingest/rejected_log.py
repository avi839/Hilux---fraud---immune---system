"""Rejected-events log (Req 2.8): raw document, receipt time and offending field names.

`RejectedLog` is the small interface the Stream_Ingestor writes to. The in-memory
implementation is the default; Storage can supply a DuckDB-backed one later.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Iterator, Optional, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class RejectedRecord:
    raw: str  # the document as received (bytes decoded as UTF-8, invalid bytes replaced)
    received_at: datetime  # SimClock time of receipt
    fields: tuple[str, ...]  # offending field paths, sorted; "$" means the whole document
    reason: str = ""  # human-readable CodecError text (field: rule (message); ...)


@runtime_checkable
class RejectedLog(Protocol):
    def append(self, record: RejectedRecord) -> None: ...


class InMemoryRejectedLog:
    """Append-only list of rejected documents. `maxlen=None` keeps every record."""

    def __init__(self, maxlen: Optional[int] = None):
        self._records: deque[RejectedRecord] = deque(maxlen=maxlen)

    def append(self, record: RejectedRecord) -> None:
        self._records.append(record)

    def records(self) -> list[RejectedRecord]:
        return list(self._records)

    def __len__(self) -> int:
        return len(self._records)

    def __iter__(self) -> Iterator[RejectedRecord]:
        return iter(list(self._records))

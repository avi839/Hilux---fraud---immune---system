"""DuckDB-backed RejectedLog (Req 2.8): satisfies ``helix.ingest.RejectedLog``."""

from __future__ import annotations

from typing import Iterator

from helix.ingest.rejected_log import RejectedRecord

from .store import Storage


class DuckDBRejectedLog:
    """Appends go through the batched writer; ``records()`` reads what has been flushed."""

    def __init__(self, storage: Storage, source: str = "ingest") -> None:
        self._storage = storage
        self.source = source

    def append(self, record: RejectedRecord) -> None:
        self._storage.record_rejected(record, self.source)

    def records(self) -> list[RejectedRecord]:
        return self._storage.rejected_records()

    def __len__(self) -> int:
        return len(self.records())

    def __iter__(self) -> Iterator[RejectedRecord]:
        return iter(self.records())

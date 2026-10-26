"""Ground_Truth_Store read side (design §19, Req 20.12, 24.11).

Only ``helix.evaluation`` and ``helix.immune.miss_detector`` may import this module or
``helix.storage.ground_truth`` (Property 70, enforced by an AST scan in the tests). This
is an architectural guard inside one process, not a security boundary.

Two reader adapters normalise records into :class:`GTView`:

* :class:`StoreGroundTruthReader` over the DuckDB-backed ``GroundTruthStore`` (dict rows).
* :class:`InMemoryGroundTruthReader` over ``InMemoryGroundTruth.records`` (dataclasses).

:func:`ground_truth_channel` is the wiring helper for code outside the allowed set (the
pipeline): it returns a writer for producers and a reader for the Evaluation_Monitor, so the
caller never imports the store module itself.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Iterator, Mapping, Optional, Protocol, Union, runtime_checkable

from helix.sim.ground_truth import GroundTruthRecord, GroundTruthWriter, InMemoryGroundTruth
from helix.storage.ground_truth import GroundTruthStore

FRAUD = "FRAUD"


@dataclass(frozen=True, slots=True)
class GTView:
    """Read-only, normalised ground-truth record."""

    event_id: str
    label: str
    source: Optional[str] = None
    written_at: Optional[datetime] = None
    campaign_id: Optional[str] = None
    genome_id: Optional[str] = None
    parent_genome_id: Optional[str] = None
    pattern_id: Optional[str] = None
    pattern_type: Optional[str] = None

    @property
    def is_fraud(self) -> bool:
        return self.label == FRAUD


def _s(v: Any) -> Optional[str]:
    if v is None:
        return None
    return str(v.value) if isinstance(v, Enum) else str(v)


def to_view(rec: Union[GroundTruthRecord, Mapping[str, Any]]) -> GTView:
    """Normalise a GroundTruthRecord or a store dict row."""
    get = rec.get if isinstance(rec, Mapping) else (lambda k, d=None: getattr(rec, k, d))
    wa = get("written_at")
    return GTView(
        event_id=str(get("event_id")),
        label=str(_s(get("label"))),
        source=_s(get("source")),
        written_at=wa if isinstance(wa, datetime) else None,
        campaign_id=_s(get("campaign_id")),
        genome_id=_s(get("genome_id")),
        parent_genome_id=_s(get("parent_genome_id")),
        pattern_id=_s(get("pattern_id")),
        pattern_type=_s(get("pattern_type")),
    )


@runtime_checkable
class GroundTruthReader(Protocol):
    def labels_since(self, t: Optional[datetime]) -> Iterator[GTView]:
        """Records with ``written_at >= t`` (all records when ``t`` is None), in write order."""
        ...

    def __len__(self) -> int: ...


class StoreGroundTruthReader:
    """Reader over the DuckDB ground-truth table."""

    def __init__(self, store: GroundTruthStore) -> None:
        self._store = store

    def labels_since(self, t: Optional[datetime]) -> Iterator[GTView]:
        rows = self._store.all() if t is None else self._store.labels_since(t)
        if t is None:  # all() is ordered by event_id; restore write order
            rows = sorted(rows, key=lambda r: (r.get("written_at") is None, r.get("written_at") or datetime.min))
        return (to_view(r) for r in rows)

    def get(self, event_id: str) -> Optional[GTView]:
        r = self._store.get(event_id)
        return None if r is None else to_view(r)

    def __len__(self) -> int:
        return len(self._store)


class InMemoryGroundTruthReader:
    """Reader over an ``InMemoryGroundTruth`` sink (tests, demos without Storage)."""

    def __init__(self, sink: InMemoryGroundTruth) -> None:
        self._sink = sink

    def labels_since(self, t: Optional[datetime]) -> Iterator[GTView]:
        recs = list(self._sink.records)
        return (to_view(r) for r in recs if t is None or r.written_at >= t)

    def __len__(self) -> int:
        return len(self._sink.records)


class StoreGroundTruthWriter:
    """GroundTruthWriter over the store: converts GroundTruthRecord dataclasses to rows."""

    def __init__(self, store: GroundTruthStore) -> None:
        self._store = store

    def write(self, rec: Union[GroundTruthRecord, Mapping[str, Any]]) -> None:
        row = dict(rec) if isinstance(rec, Mapping) else dataclasses.asdict(rec)
        self._store.write({k: (v.value if isinstance(v, Enum) else v) for k, v in row.items()})


def ground_truth_channel(storage: Any) -> tuple[GroundTruthWriter, GroundTruthReader]:
    """(writer for producers, reader for the Evaluation_Monitor) over one Storage.

    ``storage=None`` gives an in-memory channel.
    """
    if storage is None:
        sink = InMemoryGroundTruth()
        return sink, InMemoryGroundTruthReader(sink)
    store = GroundTruthStore(storage)
    return StoreGroundTruthWriter(store), StoreGroundTruthReader(store)

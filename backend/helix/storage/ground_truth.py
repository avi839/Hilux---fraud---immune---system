"""Ground_Truth_Store table access (Req 24.11, design §19).

This is the only module in ``helix.storage`` that names the ground-truth table; the
detection-path :class:`~helix.storage.Storage` facade has no method for it and its
schema does not create it. Only ``helix.evaluation`` and ``helix.immune.miss_detector``
may import this module (enforced by the import-scan test, task 9.3). This is an
architectural guard inside one process, not a security boundary.

Writes are synchronous so a caller (attacker, generator, loader) sees a failure and can
withhold the event (Req 20.11).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Iterator, Mapping

from .serde import decode_json, encode_json
from .store import Storage, _db_ts, _typed

_COLUMNS: tuple[tuple[str, str], ...] = (
    ("event_id", "VARCHAR"),
    ("label", "VARCHAR"),
    ("source", "VARCHAR"),
    ("campaign_id", "VARCHAR"),
    ("genome_id", "VARCHAR"),
    ("parent_genome_id", "VARCHAR"),
    ("change_kind", "VARCHAR"),
    ("written_at", "TIMESTAMP"),
)
_DDL = (
    "CREATE TABLE IF NOT EXISTS ground_truth ("
    "event_id VARCHAR PRIMARY KEY, label VARCHAR NOT NULL, source VARCHAR, campaign_id VARCHAR, "
    "genome_id VARCHAR, parent_genome_id VARCHAR, change_kind VARCHAR, written_at TIMESTAMP, "
    "payload JSON NOT NULL)"
)
_INSERT = (
    "INSERT OR REPLACE INTO ground_truth "
    f"({', '.join(c for c, _ in _COLUMNS)}, payload) VALUES ({', '.join('?' * (len(_COLUMNS) + 1))})"
)


class GroundTruthStore:
    """Ground-truth records as dicts: typed columns for the known fields plus the full
    record as a JSON payload (the GroundTruthRecord model is not final)."""

    def __init__(self, storage: Storage) -> None:
        self._storage = storage
        with storage._locked() as con:
            con.execute(_DDL)

    @staticmethod
    def _params(rec: Mapping[str, Any]) -> tuple:
        row = dict(rec)
        if row.get("event_id") is None or row.get("label") is None:
            raise ValueError("ground truth record needs event_id and label")
        if row.get("written_at") is None:
            row["written_at"] = datetime.now(timezone.utc)
        typed = tuple(_typed(row.get(c), t, f"ground_truth.{c}") for c, t in _COLUMNS)
        return (*typed, encode_json(row))

    def write(self, rec: Mapping[str, Any]) -> None:
        """Persist one record now. Raises on failure."""
        self.write_many([rec])

    def write_many(self, recs: Iterable[Mapping[str, Any]]) -> int:
        """Persist records in one transaction (loader batches). Raises on failure."""
        rows = [self._params(r) for r in recs]
        if not rows:
            return 0
        with self._storage._locked() as con:
            con.begin()
            try:
                con.executemany(_INSERT, rows)
                con.commit()
            except BaseException:
                try:
                    con.rollback()
                except Exception:  # noqa: BLE001
                    pass
                raise
        return len(rows)

    def get(self, event_id: str) -> dict | None:
        with self._storage._locked() as con:
            r = con.execute("SELECT payload FROM ground_truth WHERE event_id = ?", [event_id]).fetchone()
        return None if r is None else decode_json(r[0])

    def labels_since(self, t: datetime) -> Iterator[dict]:
        """Records with ``written_at >= t``, ordered by (written_at, event_id)."""
        with self._storage._locked() as con:
            rows = con.execute(
                "SELECT payload FROM ground_truth WHERE written_at >= ? ORDER BY written_at, event_id",
                [_db_ts(t)],
            ).fetchall()
        return iter([decode_json(r[0]) for r in rows])

    def all(self) -> list[dict]:
        with self._storage._locked() as con:
            rows = con.execute("SELECT payload FROM ground_truth ORDER BY event_id").fetchall()
        return [decode_json(r[0]) for r in rows]

    def __len__(self) -> int:
        with self._storage._locked() as con:
            (n,) = con.execute("SELECT COUNT(*) FROM ground_truth").fetchone()
        return int(n)

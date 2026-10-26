"""DuckDB schema for the detection-path Storage (design §20, "DuckDB schema").

Events and assessments have real columns. Cases, Evidence_Chains, history, feedback,
weight history, antibodies, motif instances and config changes are stored as typed key
columns plus a JSON ``payload`` column, because their models are still evolving: the
payload is the whole row dict, encoded with :mod:`helix.storage.serde`.

The ground-truth table is deliberately absent here; it lives in
:mod:`helix.storage.ground_truth` so the detection-path schema never names it.

All timestamps are stored as UTC-naive ``TIMESTAMP`` (no pytz dependency); ``events``
also keeps the original UTC offset so restored events format identically.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

SCHEMA_VERSION = 1
SNAPSHOT_VERSION = 1


@dataclass(frozen=True, slots=True)
class JsonTable:
    """A table of typed key/index columns plus a JSON payload holding the full row."""

    name: str
    columns: tuple[tuple[str, str], ...]  # (name, SQL type), key columns included
    keys: tuple[str, ...]
    append_only: bool = False
    auto_key: str | None = None  # BIGINT key assigned by Storage when the row omits it

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(c for c, _ in self.columns)

    def ddl(self) -> str:
        cols = [
            f"{c} {t} NOT NULL" if c in self.keys else f"{c} {t}" for c, t in self.columns
        ]
        cols.append("payload JSON NOT NULL")
        cols.append(f"PRIMARY KEY ({', '.join(self.keys)})")
        return f"CREATE TABLE {self.name} ({', '.join(cols)})"

    def insert_sql(self) -> str:
        names = (*self.column_names, "payload")
        verb = "INSERT OR IGNORE" if self.append_only else "INSERT OR REPLACE"
        return f"{verb} INTO {self.name} ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})"


JSON_TABLES: dict[str, JsonTable] = {
    t.name: t
    for t in (
        JsonTable(
            "cases",
            (("case_id", "VARCHAR"), ("status", "VARCHAR"), ("updated_at", "TIMESTAMP")),
            ("case_id",),
        ),
        JsonTable("evidence_chains", (("case_id", "VARCHAR"), ("updated_at", "TIMESTAMP")), ("case_id",)),
        JsonTable(
            "case_history",
            (("case_id", "VARCHAR"), ("entry_no", "BIGINT"), ("ts", "TIMESTAMP")),
            ("case_id", "entry_no"),
            append_only=True,
        ),
        JsonTable(
            "feedback",
            (("case_id", "VARCHAR"), ("label", "VARCHAR"), ("ts", "TIMESTAMP")),
            ("case_id",),
        ),
        JsonTable(
            "weight_history",
            (
                ("record_no", "BIGINT"),
                ("contributor_id", "VARCHAR"),
                ("ts", "TIMESTAMP"),
                ("case_id", "VARCHAR"),
            ),
            ("record_no",),
            append_only=True,
            auto_key="record_no",
        ),
        JsonTable("antibodies", (("ab_id", "VARCHAR"), ("active", "BOOLEAN")), ("ab_id",)),
        JsonTable(
            "motif_instances",
            (("motif_id", "VARCHAR"), ("motif_type", "VARCHAR"), ("ts_end", "TIMESTAMP")),
            ("motif_id",),
        ),
        JsonTable(
            "config_changes",
            (("change_no", "BIGINT"), ("name", "VARCHAR"), ("ts", "TIMESTAMP")),
            ("change_no",),
            append_only=True,
            auto_key="change_no",
        ),
    )
}

EVENT_COLUMNS: tuple[str, ...] = (
    "seq_no", "tx_id", "ts", "ts_offset_s", "src", "dst", "amount", "currency",
    "channel", "mcc", "lat", "lon", "device_id", "session_id",
    "late", "lateness_s", "received_at",
)  # fmt: skip

ASSESSMENT_COLUMNS: tuple[str, ...] = (
    "tx_id", "stage", "f", "l", "t", "belief", "plausibility", "uncertainty",
    "conflict", "high_conflict", "risk", "contributions",
)  # fmt: skip

REJECTED_COLUMNS: tuple[str, ...] = ("reject_no", "received_at", "raw", "fields", "reason", "source")

CORE_DDL: tuple[str, ...] = (
    """CREATE TABLE schema_version (
        version INTEGER PRIMARY KEY, applied_at TIMESTAMP NOT NULL)""",
    """CREATE TABLE events (
        seq_no BIGINT NOT NULL, tx_id VARCHAR PRIMARY KEY, ts TIMESTAMP NOT NULL,
        ts_offset_s INTEGER NOT NULL, src VARCHAR NOT NULL, dst VARCHAR NOT NULL,
        amount DECIMAL(11,2) NOT NULL, currency VARCHAR NOT NULL,
        channel VARCHAR, mcc VARCHAR, lat DOUBLE, lon DOUBLE, device_id VARCHAR,
        session_id VARCHAR, late BOOLEAN NOT NULL, lateness_s DOUBLE NOT NULL,
        received_at TIMESTAMP)""",
    """CREATE TABLE assessments (
        tx_id VARCHAR NOT NULL, stage TINYINT NOT NULL, f DOUBLE NOT NULL, l DOUBLE NOT NULL,
        t DOUBLE NOT NULL, belief DOUBLE NOT NULL, plausibility DOUBLE NOT NULL,
        uncertainty DOUBLE NOT NULL, conflict DOUBLE NOT NULL, high_conflict BOOLEAN NOT NULL,
        risk DOUBLE, contributions JSON NOT NULL, PRIMARY KEY (tx_id, stage))""",
    """CREATE TABLE rejected_events (
        reject_no BIGINT PRIMARY KEY, received_at TIMESTAMP NOT NULL, raw VARCHAR NOT NULL,
        fields VARCHAR[] NOT NULL, reason VARCHAR NOT NULL, source VARCHAR NOT NULL)""",
    """CREATE TABLE snapshots (
        snapshot_no BIGINT NOT NULL, kind VARCHAR NOT NULL, version INTEGER NOT NULL,
        ts TIMESTAMP NOT NULL, blob BLOB NOT NULL, PRIMARY KEY (snapshot_no, kind))""",
)

EVENTS_INSERT = (
    f"INSERT OR REPLACE INTO events ({', '.join(EVENT_COLUMNS)}) "
    f"VALUES ({', '.join('?' * len(EVENT_COLUMNS))})"
)
ASSESSMENTS_INSERT = (
    f"INSERT OR REPLACE INTO assessments ({', '.join(ASSESSMENT_COLUMNS)}) "
    f"VALUES ({', '.join('?' * len(ASSESSMENT_COLUMNS))})"
)
REJECTED_INSERT = (
    f"INSERT OR IGNORE INTO rejected_events ({', '.join(REJECTED_COLUMNS)}) "
    f"VALUES ({', '.join('?' * len(REJECTED_COLUMNS))})"
)


def all_ddl() -> tuple[str, ...]:
    return CORE_DDL + tuple(t.ddl() for t in JSON_TABLES.values())


def column_layout(con) -> dict[str, tuple[tuple[str, str], ...]]:
    """``table -> ((column, data_type), ...)`` in ordinal order, for the main schema."""
    rows = con.execute(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'main' ORDER BY table_name, ordinal_position"
    ).fetchall()
    out: dict[str, list[tuple[str, str]]] = {}
    for table, col, typ in rows:
        out.setdefault(table, []).append((col, typ))
    return {k: tuple(v) for k, v in out.items()}


@lru_cache(maxsize=1)
def expected_layout() -> dict[str, tuple[tuple[str, str], ...]]:
    """Column layout of a fresh schema, built in an in-memory database."""
    import duckdb

    con = duckdb.connect(":memory:")
    try:
        for stmt in all_ddl():
            con.execute(stmt)
        return column_layout(con)
    finally:
        con.close()

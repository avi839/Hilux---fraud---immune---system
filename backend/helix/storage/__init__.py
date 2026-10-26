"""HELIX storage package: DuckDB batched writer, snapshots, restore (design §20).

``helix.storage.ground_truth`` is intentionally not re-exported here: only
``helix.evaluation`` and ``helix.immune.miss_detector`` may import it.
"""

from .rejected import DuckDBRejectedLog
from .schema import JSON_TABLES, SCHEMA_VERSION, SNAPSHOT_VERSION
from .serde import decode_json, encode_json, profile_store_from_dict, profile_store_to_dict
from .store import (
    RestoredState,
    SchemaMismatchError,
    Snapshot,
    Storage,
    StorageError,
    StorageOpenError,
    StorageStatus,
    StoredAssessment,
    StoredEvent,
)

__all__ = [
    "JSON_TABLES",
    "SCHEMA_VERSION",
    "SNAPSHOT_VERSION",
    "DuckDBRejectedLog",
    "RestoredState",
    "SchemaMismatchError",
    "Snapshot",
    "Storage",
    "StorageError",
    "StorageOpenError",
    "StorageStatus",
    "StoredAssessment",
    "StoredEvent",
    "decode_json",
    "encode_json",
    "profile_store_from_dict",
    "profile_store_to_dict",
]

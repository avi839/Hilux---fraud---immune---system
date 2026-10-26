"""HELIX datasets package: AMLworld Dataset_Loader (design §3a), Column_Mapping and
CSV_Adapter (§25), Dataset_Feeder and Warm_Up_Mode (§24, §25)."""

from .amlworld import (
    HEADER,
    DatasetLoader,
    LoaderError,
    LoadReport,
    ReplayScope,
    RowError,
    format_row,
    parse_row,
    parse_ts,
    resolve_tz,
    row_key,
    validate_speed,
)
from .amlworld import AmlworldCodec, LoaderProgress, RowCodec
from .csv_adapter import MappedRowParser, MappingCodec
from .currencies import CODE_TO_NAME, NAME_TO_CODE
from .feed import DatasetFeeder, DatasetSpec, DatasetStartupError, FeedProgress, FeedStatus
from .mapping import ColumnMapping, ColumnRef, MappingError, dump_mapping, load_mapping, validate_mapping
from .patterns import MOTIF_FOR_PATTERN, PATTERN_TYPES, motif_for, parse_patterns
from .warmup import WarmupReport

__all__ = [
    "AmlworldCodec",
    "CODE_TO_NAME",
    "ColumnMapping",
    "ColumnRef",
    "DatasetFeeder",
    "DatasetSpec",
    "DatasetStartupError",
    "FeedProgress",
    "FeedStatus",
    "LoaderProgress",
    "MappedRowParser",
    "MappingCodec",
    "MappingError",
    "RowCodec",
    "WarmupReport",
    "dump_mapping",
    "load_mapping",
    "validate_mapping",
    "DatasetLoader",
    "HEADER",
    "LoadReport",
    "LoaderError",
    "MOTIF_FOR_PATTERN",
    "NAME_TO_CODE",
    "PATTERN_TYPES",
    "ReplayScope",
    "RowError",
    "format_row",
    "motif_for",
    "parse_patterns",
    "parse_row",
    "parse_ts",
    "resolve_tz",
    "row_key",
    "validate_speed",
]

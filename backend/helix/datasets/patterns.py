"""AMLworld `*_Patterns.txt` parser (design §3a, Req 4.18, 4.19, 4.21).

The file is a sequence of blocks::

    BEGIN LAUNDERING ATTEMPT - FAN-OUT:  Max 16-degree Fan-Out
    2022/09/01 00:10,070,100428660,1124,800825340,1810.14,US Dollar,1810.14,US Dollar,ACH,1
    ...
    END LAUNDERING ATTEMPT - FAN-OUT

Rows inside a block use the `*_Trans.csv` column order. This module only splits the
file into blocks; matching rows to loaded transactions is done by the loader, which
owns the content index.
"""

from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional

from helix.motifs.models import MotifType

log = logging.getLogger("helix.datasets")

PATTERN_TYPES: frozenset[str] = frozenset(
    {"FAN-OUT", "FAN-IN", "CYCLE", "GATHER-SCATTER", "SCATTER-GATHER", "STACK", "RANDOM", "BIPARTITE"}
)

# Req 4.21: AMLworld types with a HELIX motif; the rest are reported as unmapped.
# GATHER-SCATTER (fan-in to a hub, then fan-out from it) and SCATTER-GATHER (fan-out to
# intermediaries that fan in to a sink) are compositions of the two fan motifs, so the fan
# detectors see them; each maps to its first stage (task 29.1). The mapping only labels the
# per-type report; detection is the same either way. STACK and BIPARTITE rows in HI-Small
# give every account one in- and one out-edge (no fan structure), and RANDOM has no fixed
# shape, so those three stay unmapped.
MOTIF_FOR_PATTERN: Mapping[str, MotifType] = {
    "CYCLE": MotifType.ROUND_TRIP,
    "FAN-OUT": MotifType.FAN_OUT,
    "FAN-IN": MotifType.FAN_IN,
    "GATHER-SCATTER": MotifType.FAN_IN,
    "SCATTER-GATHER": MotifType.FAN_OUT,
}

_BEGIN = re.compile(r"^\s*BEGIN\s+LAUNDERING\s+ATTEMPT\b\s*-?\s*(?P<rest>.*?)\s*$", re.IGNORECASE)
_END = re.compile(r"^\s*END\s+LAUNDERING\s+ATTEMPT\b", re.IGNORECASE)
_TYPE = re.compile(r"^(?P<type>[A-Za-z]+(?:-[A-Za-z]+)?)\s*(?P<detail>.*)$")


def motif_for(pattern_type: str) -> Optional[MotifType]:
    """HELIX motif for an AMLworld pattern type, or None when unmapped."""
    return MOTIF_FOR_PATTERN.get(pattern_type.upper())


@dataclass(frozen=True, slots=True)
class PatternRow:
    lineno: int  # 1-based line number in the patterns file
    fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PatternBlock:
    n: int  # 1-based block number in file order
    pattern_type: str  # upper-cased, e.g. "FAN-OUT"; "UNKNOWN" if the BEGIN line had no type
    detail: Optional[str]  # text after the type, e.g. "Max 16-degree Fan-Out"
    lineno: int
    rows: tuple[PatternRow, ...]

    @property
    def known(self) -> bool:
        return self.pattern_type in PATTERN_TYPES


@dataclass(slots=True)
class ParsedPatterns:
    blocks: list[PatternBlock] = field(default_factory=list)
    stray_rows: list[PatternRow] = field(default_factory=list)  # rows outside any block


def _split_type(rest: str) -> tuple[str, Optional[str]]:
    m = _TYPE.match(rest)
    if m is None:
        return "UNKNOWN", rest or None
    detail = m.group("detail").lstrip(":").strip()
    return m.group("type").upper(), detail or None


def parse_patterns(lines: Iterable[str]) -> ParsedPatterns:
    """Line-oriented state machine; tolerant of missing END lines and blank lines."""
    out = ParsedPatterns()
    cur: Optional[tuple[int, str, Optional[str], int, list[PatternRow]]] = None

    def close() -> None:
        nonlocal cur
        if cur is not None:
            n, ptype, detail, start, rows = cur
            out.blocks.append(PatternBlock(n, ptype, detail, start, tuple(rows)))
            cur = None

    n = 0
    for lineno, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        if (m := _BEGIN.match(line)) is not None:
            if cur is not None:
                log.warning("patterns line %d: BEGIN inside an open block; closing block %d", lineno, cur[0])
                close()
            n += 1
            ptype, detail = _split_type(m.group("rest"))
            if ptype not in PATTERN_TYPES:
                log.warning("patterns line %d: unknown pattern type %r", lineno, ptype)
            cur = (n, ptype, detail, lineno, [])
            continue
        if _END.match(line):
            if cur is None:
                log.warning("patterns line %d: END without BEGIN; ignored", lineno)
            close()
            continue
        fields = tuple(next(csv.reader([line]), []))
        row = PatternRow(lineno, fields)
        if cur is None:
            out.stray_rows.append(row)
        else:
            cur[4].append(row)
    if cur is not None:
        log.warning("patterns: block %d (line %d) has no END line", cur[0], cur[3])
        close()
    return out

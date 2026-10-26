"""Onboard_Validator (design §25, Req 40.9): dry-run a Column_Mapping over a CSV export.

``python -m helix.datasets.onboard --mapping M --trans F [--rows 1000]`` (wrapped by
``scripts\\onboard.ps1``). It parses at most the first N data rows with the CSV_Adapter, and
never starts the HELIX loop or writes to the Ground_Truth_Store. Exit 0 when the mapping is
valid (rejected rows are reported, not fatal), 1 on a mapping, file or header error.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence, TextIO, Union

from helix.codec.models import OPTIONAL_FIELDS, REQUIRED_FIELDS

from .amlworld import RowError
from .csv_adapter import MappedRowParser
from .mapping import ColumnMapping, MappingError, header_errors, load_mapping

DEFAULT_ROWS = 1_000
EXAMPLES = 4
EVENT_FIELDS: tuple[str, ...] = REQUIRED_FIELDS + OPTIONAL_FIELDS


@dataclass(slots=True)
class OnboardReport:
    mapping: ColumnMapping
    rows_read: int = 0
    parsed: int = 0
    rejected: Counter = field(default_factory=Counter)  # reason -> count
    examples: dict[str, list[int]] = field(default_factory=dict)  # reason -> first row numbers
    present: Counter = field(default_factory=Counter)  # event field -> parsed rows with a value
    labels: Counter = field(default_factory=Counter)  # FRAUD / LEGIT / none
    first_ts: Optional[datetime] = None
    last_ts: Optional[datetime] = None
    in_order: bool = True

    @property
    def rejected_total(self) -> int:
        return sum(self.rejected.values())

    def coverage(self) -> dict[str, float]:
        """Percentage of parsed rows containing each Transaction_Event field."""
        return {f: (100.0 * self.present[f] / self.parsed if self.parsed else 0.0) for f in EVENT_FIELDS}


def validate_file(
    mapping: Union[ColumnMapping, Path, str], trans: Union[Path, str], rows: int = DEFAULT_ROWS
) -> Union[OnboardReport, list[MappingError]]:
    """Parse at most ``rows`` data rows of ``trans``; mapping / header problems as errors."""
    if not isinstance(mapping, ColumnMapping):
        m = load_mapping(mapping)
        if isinstance(m, list):
            return m
        mapping = m
    trans = Path(trans)
    rep = OnboardReport(mapping)
    try:
        with open(trans, encoding=mapping.encoding, newline="") as fh:
            reader = csv.reader(fh, delimiter=mapping.delimiter)
            header = next(reader, None)
            errs = header_errors(mapping, header)
            if errs:
                return errs
            assert header is not None
            parser = MappedRowParser(mapping, header, trans.stem)
            prev: Optional[datetime] = None
            for row, fields in enumerate(reader, start=1):
                if row > rows:
                    break
                rep.rows_read += 1
                res = parser.parse(fields, row)
                if isinstance(res, RowError):
                    rep.rejected[res.reason] += 1
                    ex = rep.examples.setdefault(res.reason, [])
                    if len(ex) < EXAMPLES:
                        ex.append(row)
                    continue
                ev, label = res
                rep.parsed += 1
                for f in EVENT_FIELDS:
                    if getattr(ev, f) is not None:
                        rep.present[f] += 1
                rep.labels["none" if label is None else ("FRAUD" if label else "LEGIT")] += 1
                if prev is not None and ev.ts < prev:
                    rep.in_order = False
                prev = ev.ts
                rep.first_ts = ev.ts if rep.first_ts is None or ev.ts < rep.first_ts else rep.first_ts
                rep.last_ts = ev.ts if rep.last_ts is None or ev.ts > rep.last_ts else rep.last_ts
    except FileNotFoundError:
        return [MappingError("--trans", f"{trans}: file not found")]
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        return [MappingError("--trans", f"{trans}: unreadable: {exc}")]
    return rep


def _iso(ts: Optional[datetime]) -> str:
    return "-" if ts is None else ts.isoformat().replace("+00:00", "Z")


def render(rep: OnboardReport, out: TextIO) -> None:
    m = rep.mapping
    w = out.write
    w(f"Mapping {m.name} v{m.version} OK\n")
    w(f"Rows read {rep.rows_read}: parsed {rep.parsed}, rejected {rep.rejected_total}\n")
    for reason, n in sorted(rep.rejected.items(), key=lambda kv: (-kv[1], kv[0])):
        ex = rep.examples.get(reason, [])
        more = ", ..." if n > len(ex) else ""
        w(f"  {reason:<22} {n:>5}  rows {', '.join(map(str, ex))}{more}\n")
    cov = "  ".join(f"{f} {p:.1f}%" for f, p in rep.coverage().items())
    w(f"Field coverage (of parsed rows): {cov}\n")
    if m.label is not None:
        w(f"Labels: FRAUD {rep.labels['FRAUD']}, LEGIT {rep.labels['LEGIT']}, none {rep.labels['none']}\n")
    else:
        w("Labels: no label column mapped\n")
    order = "yes" if rep.in_order else "no, the loader will sort rows by timestamp"
    w(f"Timestamps {_iso(rep.first_ts)} .. {_iso(rep.last_ts)} (in order: {order})\n")


def main(argv: Optional[Sequence[str]] = None, out: TextIO = sys.stdout, err: TextIO = sys.stderr) -> int:
    p = argparse.ArgumentParser(prog="python -m helix.datasets.onboard",
                                description="Validate a Column_Mapping against a CSV export (no HELIX loop).")
    p.add_argument("--mapping", type=Path, required=True, help="Column_Mapping file (.yaml/.yml/.json)")
    p.add_argument("--trans", type=Path, required=True, help="CSV transaction export")
    p.add_argument("--rows", type=int, default=DEFAULT_ROWS, help=f"parse at most N rows (default {DEFAULT_ROWS})")
    a = p.parse_args(argv)
    if a.rows < 1:
        p.error("--rows must be >= 1")
    res = validate_file(a.mapping, a.trans, a.rows)
    if isinstance(res, list):
        err.write(f"Column_Mapping / file errors ({len(res)}):\n")
        for e in res:
            err.write(f"  {e}\n")
        return 1
    render(res, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

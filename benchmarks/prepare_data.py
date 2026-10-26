"""Extract the benchmark window from IBM AMLworld HI-Small once, with DuckDB.

The raw file is not time-ordered. This script reads it once with DuckDB (all columns as
text, so values are byte-identical), keeps the rows in [fit_start, test_end), sorts them by
(Timestamp, original file row) and writes ``benchmarks/.cache/window.csv`` with the original
AMLworld header. Both HELIX (export_helix.py) and the baselines read this one file, so they
see exactly the same rows, and HELIX's tx ids (``aml-window-<row>``) join to it by row number.

    backend\\.venv\\Scripts\\python.exe benchmarks\\prepare_data.py            # extract
    backend\\.venv\\Scripts\\python.exe benchmarks\\prepare_data.py --profile  # rows per day/hour
"""

from __future__ import annotations

import argparse
import csv
import json
import time

import duckdb

from common import CACHE, RAW_TRANS, WINDOW_CSV, load_config

HEADER = ["Timestamp", "From Bank", "Account", "To Bank", "Account", "Amount Received",
          "Receiving Currency", "Amount Paid", "Payment Currency", "Payment Format", "Is Laundering"]


def _src() -> str:
    return str(RAW_TRANS).replace("'", "''")


def profile() -> None:
    con = duckdb.connect()
    q = (f"SELECT substr(Timestamp, 1, {{n}}) AS k, count(*) AS rows, "
         f"sum(CAST(\"Is Laundering\" AS INT)) AS laundering "
         f"FROM read_csv('{_src()}', header=true, all_varchar=true) GROUP BY k ORDER BY k")
    for k, n, l in con.execute(q.format(n=10)).fetchall():
        print(f"{k}  rows {n:>9,}  laundering {l:>6,}")
    print("--- hourly, 2022/09/01 .. 2022/09/04")
    for k, n, l in con.execute(q.format(n=13)).fetchall():
        if k < "2022/09/05":
            print(f"{k}h  rows {n:>8,}  laundering {l:>5,}")


def extract(fit_start: str, test_end: str) -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=1")  # keep file order for row_number() OVER ()
    con.execute("SET preserve_insertion_order=true")
    # Columns come back as Timestamp, From Bank, Account, To Bank, Account_1, ...
    rows = con.execute(
        f"WITH t AS (SELECT row_number() OVER () AS r, * "
        f"FROM read_csv('{_src()}', header=true, all_varchar=true)) "
        f"SELECT * EXCLUDE (r) FROM t WHERE Timestamp >= ? AND Timestamp < ? ORDER BY Timestamp, r",
        [fit_start, test_end],
    ).fetchall()
    with open(WINDOW_CSV, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(HEADER)
        w.writerows(rows)
    meta = {"fit_start": fit_start, "test_end": test_end, "rows": len(rows), "source": RAW_TRANS.name}
    (CACHE / "window.meta.json").write_text(json.dumps(meta, indent=2))
    print(f"wrote {WINDOW_CSV} ({len(rows):,} rows)")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--profile", action="store_true")
    a = p.parse_args()
    t0 = time.monotonic()
    if a.profile:
        profile()
    else:
        w = load_config()["windows"]
        extract(w["fit_start"], w["test_end"])
    print(f"took {time.monotonic() - t0:.1f} s")


if __name__ == "__main__":
    main()

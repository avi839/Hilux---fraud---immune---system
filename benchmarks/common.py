"""Shared paths, config and the cached-window reader for the AMLworld benchmark.

Nothing here imports HELIX: the benchmark only reads the cached window and the HELIX export.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "benchmarks"
CACHE = BENCH / ".cache"
RESULTS = BENCH / "results"
RAW_TRANS = ROOT / "data" / "HI-Small_Trans.csv"
WINDOW_CSV = CACHE / "window.csv"
CONFIG = BENCH / "config.json"


def load_config(path: Path = CONFIG) -> dict:
    return json.loads(Path(path).read_text())


def usd_rate(cfg: dict) -> dict[str, float]:
    return {k: float(v) for k, v in cfg["usd_per_unit"].items()}


def read_window(path: Path = WINDOW_CSV, cfg: dict | None = None) -> pd.DataFrame:
    """The cached window as one row per transaction.

    ``row`` is the 1-based data-row number in window.csv, so ``tx_id`` equals the id the
    HELIX Dataset_Loader gives the same row (``aml-<stem>-<row>``).
    """
    cfg = cfg or load_config()
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df.columns = ["ts_s", "from_bank", "from_acct", "to_bank", "to_acct", "amt_recv", "cur_recv",
                  "amt_paid", "cur_paid", "fmt", "label"]
    df["row"] = np.arange(1, len(df) + 1)
    df["tx_id"] = "aml-" + Path(path).stem + "-" + df["row"].astype(str)
    df["ts"] = pd.to_datetime(df["ts_s"], format="%Y/%m/%d %H:%M", utc=True)
    # Same account key as HELIX: "<int bank>:<account>".
    df["src"] = df["from_bank"].astype(int).astype(str) + ":" + df["from_acct"].str.strip()
    df["dst"] = df["to_bank"].astype(int).astype(str) + ":" + df["to_acct"].str.strip()
    df["amount"] = df["amt_paid"].astype(float)
    rate = usd_rate(cfg)
    missing = sorted(set(df["cur_paid"]) - set(rate))
    if missing:
        raise ValueError(f"no USD rate for currencies {missing}; add them to benchmarks/config.json")
    df["usd"] = df["amount"] * df["cur_paid"].map(rate)
    df["y"] = (df["label"] == "1").astype(int)
    df["hour"] = df["ts"].dt.floor("h")
    return df[["row", "tx_id", "ts", "hour", "src", "dst", "amount", "cur_paid", "usd", "fmt", "y"]]

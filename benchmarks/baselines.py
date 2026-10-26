"""Baselines for the AMLworld benchmark: a threshold rule, Isolation Forest and XGBoost.

All three see the same causal per-transaction features, computed over the cached window in
(timestamp, row) order so a test transaction only uses history up to itself:

- ``log_usd``: log1p of Amount Paid in USD (fixed rate table, benchmarks/config.json)
- ``hour``: hour of day
- ``src_1h`` / ``src_24h``: the source account's transactions in the past 1 h / 24 h,
  this one included (history starts at fit_start)
- ``dst_new``: 1 when this source has not paid this destination before in the window
- ``fmt``: Payment Format as a fixed ordinal

Labels are used only to train XGBoost on the fit window and, later, to score everyone.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from xgboost import XGBClassifier

FEATURES = ["log_usd", "hour_of_day", "src_1h", "src_24h", "dst_new", "fmt_ord"]
FORMATS = ["ACH", "Bitcoin", "Cash", "Cheque", "Credit Card", "Reinvestment", "Wire"]


def _window_count(src_code: np.ndarray, minutes: np.ndarray, order: np.ndarray, span_min: int) -> np.ndarray:
    """Per row: rows of the same source in (t - span, t], counting only rows at or before it
    in (ts, row) order. ``order`` sorts rows by (src, ts, row)."""
    big = np.int64(10**8)  # > any minute offset in the window, so sources never overlap
    key = src_code[order].astype(np.int64) * big + minutes[order]
    left = np.searchsorted(key, key - span_min, side="right")
    out = np.empty(len(key), dtype=np.int64)
    out[order] = np.arange(len(key)) - left + 1
    return out


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    """``df`` must be in (ts, row) order (read_window returns it that way)."""
    df = df.copy()
    df["log_usd"] = np.log1p(df["usd"].to_numpy())
    df["hour_of_day"] = df["ts"].dt.hour
    src_code = pd.factorize(df["src"])[0]
    minutes = ((df["ts"] - df["ts"].iloc[0]).dt.total_seconds() // 60).astype(np.int64).to_numpy()
    order = np.lexsort((df["row"].to_numpy(), minutes, src_code))
    df["src_1h"] = _window_count(src_code, minutes, order, 60)
    df["src_24h"] = _window_count(src_code, minutes, order, 24 * 60)
    df["dst_new"] = (~df.duplicated(["src", "dst"], keep="first")).astype(int)
    ordmap = {f: i for i, f in enumerate(FORMATS)}
    df["fmt_ord"] = df["fmt"].map(ordmap).fillna(len(FORMATS)).astype(int)
    return df


def rule_scores(fit: pd.DataFrame, test: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Flag amount >= fit P99 (USD) or 1 h source burst >= fit P99. Score = the larger ratio."""
    amt_p99 = float(np.percentile(fit["usd"], 99))
    burst_p99 = float(np.percentile(fit["src_1h"], 99))
    ratio = np.maximum(test["usd"] / amt_p99, test["src_1h"] / burst_p99)
    flagged = (test["usd"] >= amt_p99) | (test["src_1h"] >= burst_p99)
    out = test[["tx_id"]].assign(score=ratio.to_numpy(), flagged=flagged.to_numpy())
    return out, {"amount_p99_usd": round(amt_p99, 2), "burst_1h_p99": burst_p99}


def iforest_scores(fit: pd.DataFrame, test: pd.DataFrame, seed: int) -> pd.DataFrame:
    m = IsolationForest(n_estimators=200, random_state=seed, n_jobs=4)
    m.fit(fit[FEATURES].to_numpy(dtype=float))
    s = -m.score_samples(test[FEATURES].to_numpy(dtype=float))  # higher = more anomalous
    return test[["tx_id"]].assign(score=s, flagged=True)


def xgb_scores(fit: pd.DataFrame, test: pd.DataFrame, seed: int) -> tuple[pd.DataFrame, dict]:
    """Trained on the fit window only (earlier in time than the test window; never shuffled
    across the split). Default-ish params, no tuning."""
    y = fit["y"].to_numpy()
    pos = int(y.sum())
    spw = (len(y) - pos) / max(pos, 1)
    m = XGBClassifier(n_estimators=200, max_depth=6, learning_rate=0.1, subsample=0.8,
                      colsample_bytree=0.8, scale_pos_weight=spw, tree_method="hist",
                      eval_metric="aucpr", random_state=seed, n_jobs=4)
    m.fit(fit[FEATURES].to_numpy(dtype=float), y)
    s = m.predict_proba(test[FEATURES].to_numpy(dtype=float))[:, 1]
    return test[["tx_id"]].assign(score=s, flagged=True), {"scale_pos_weight": round(spw, 1)}

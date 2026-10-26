"""Scoring logic for the AMLworld benchmark (pure pandas; unit-tested in tests/).

Alert unit: one alert = one account investigation.
- Baselines: flagged test transactions in the same clock hour are grouped by source account;
  the alert's score is the max transaction score.
- HELIX: one Case. Its hour is the clock hour it was admitted to the Active_Queue (or, for
  the equal-capacity variant, created, when it never was). Only the Case's test-window
  transactions with a timestamp before the end of that hour count (what an investigator
  could see at that point).

Capacity: every system takes its own top N alerts per clock hour by its own ranking.
True positive: the alert holds at least one laundering transaction.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import numpy as np
import pandas as pd

HOUR = pd.Timedelta(hours=1)


@dataclass(frozen=True)
class Costs:
    """One alert = one case = ``minutes_per_case`` of analyst time at ``analyst_cost_per_hour``.

    There is exactly one cost per case (no separate per-alert fee on top): 20 min x 30/h = 10.
    The minutes sensitivity uses the same formula (10 min -> 5, 30 min -> 15)."""

    minutes_per_case: float = 20.0
    analyst_cost_per_hour: float = 30.0

    @property
    def cost_per_case(self) -> float:
        return self.minutes_per_case / 60.0 * self.analyst_cost_per_hour


def group_alerts(scored: pd.DataFrame, tx: pd.DataFrame) -> pd.DataFrame:
    """Flagged transactions -> one alert per (clock hour, source account).

    ``scored``: tx_id, score, flagged. ``tx``: tx_id, hour, src. Returns hour, key, score,
    tx_ids (tuple), sorted by hour then score descending.
    """
    d = scored[scored["flagged"]].merge(tx[["tx_id", "hour", "src"]], on="tx_id", how="inner")
    g = d.groupby(["hour", "src"], sort=False).agg(score=("score", "max"), tx_ids=("tx_id", tuple)).reset_index()
    g = g.rename(columns={"src": "key"})
    return g.sort_values(["hour", "score", "key"], ascending=[True, False, True], kind="mergesort").reset_index(drop=True)


def top_n_per_hour(alerts: pd.DataFrame, n: int) -> pd.DataFrame:
    """The ``n`` highest-``score`` alerts in each hour (ties broken by ``key``)."""
    a = alerts.sort_values(["hour", "score", "key"], ascending=[True, False, True], kind="mergesort")
    return a.groupby("hour", sort=False).head(n).reset_index(drop=True)


def _hour_end_state(placements: list) -> dict:
    """Placement at the end of each clock hour that saw a placement change."""
    out: dict = {}
    for ts, p in placements:
        out[pd.Timestamp(ts).floor("h")] = p
    return out


def admission_hour(placements: list):
    """First clock hour that ends with the Case in the Active_Queue (an ACTIVE placement
    displaced to the Backlog within the same hour does not count), else None."""
    for h, p in sorted(_hour_end_state(placements).items()):
        if p == "ACTIVE":
            return h
    return None


def helix_alerts(cases: pd.DataFrame, tx: pd.DataFrame, test_start, test_end, include_backlog: bool) -> pd.DataFrame:
    """HELIX Cases -> alerts in the same shape as ``group_alerts``.

    Merged-away Cases are dropped (their transactions live on in the survivor). ``score`` is
    Case_Priority. With ``include_backlog`` a never-admitted Case is available in the hour it
    was created; otherwise only admitted Cases are alerts.
    """
    ts_of = dict(zip(tx["tx_id"], tx["ts"]))
    rows = []
    for c in cases.itertuples(index=False):
        if isinstance(c.merged_into, str) and c.merged_into:
            continue
        h = admission_hour(json.loads(c.placements)) if isinstance(c.placements, str) else None
        if h is None:
            if not include_backlog:
                continue
            h = pd.Timestamp(c.created_ts).floor("h")
        if not (test_start <= h < test_end):
            continue
        ids = str(c.tx_ids).split(";") if isinstance(c.tx_ids, str) and c.tx_ids else []
        hend = h + HOUR
        keep = tuple(t for t in ids if t in ts_of and test_start <= ts_of[t] < hend)
        rows.append({"hour": h, "key": c.case_id, "score": float(c.priority), "tx_ids": keep,
                     "risk": float(c.risk)})
    return pd.DataFrame(rows, columns=["hour", "key", "score", "tx_ids", "risk"])


def evaluate(selected: pd.DataFrame, tx: pd.DataFrame, costs: Costs, hours: int, n: int | None = None,
             value_cap_usd: float | None = None) -> dict:
    """Metrics for one system at one capacity. ``tx``: test-window tx_id, y, usd.

    ``value_cap_usd`` adds ``*_capped`` value metrics where each laundering transaction is
    worth at most the cap (a few huge transactions otherwise dominate value).
    """
    lab = tx.loc[tx["y"] == 1]
    lab_usd = dict(zip(lab["tx_id"], lab["usd"]))
    total_l, total_v = len(lab_usd), float(sum(lab_usd.values()))
    cap = float("inf") if value_cap_usd is None else float(value_cap_usd)
    tp = 0
    caught: set = set()
    for ids in selected["tx_ids"]:
        hit = [t for t in ids if t in lab_usd]
        if hit:
            tp += 1
            caught.update(hit)
    alerts = int(len(selected))
    fp = alerts - tp
    value = float(sum(lab_usd[t] for t in caught))
    inv_h = alerts * costs.minutes_per_case / 60.0
    cost = alerts * costs.cost_per_case  # cost per true catch = alerts x cost_per_case / TP
    out = {
        "alerts": alerts,
        "capacity": None if n is None else n * hours,
        "tp": tp,
        "fp": fp,
        "alert_precision": tp / alerts if alerts else 0.0,
        # precision@N over the N x hours slots: an unused slot counts as a miss
        "precision_at_n": tp / (n * hours) if n and hours else None,
        "laundering_tx_caught": len(caught),
        "tx_recall": len(caught) / total_l if total_l else 0.0,
        "value_captured_usd": round(value, 2),
        "value_captured_frac": value / total_v if total_v else 0.0,
        "investigator_hours": round(inv_h, 2),
        "value_per_inv_hour_usd": round(value / inv_h, 2) if inv_h else 0.0,
        "total_cost_usd": round(cost, 2),
        "cost_per_true_catch_usd": round(cost / tp, 2) if tp else None,
        "wasted_hours": round(fp * costs.minutes_per_case / 60.0, 2),
    }
    if value_cap_usd is not None:
        vc = float(sum(min(lab_usd[t], cap) for t in caught))
        tc = float(sum(min(v, cap) for v in lab_usd.values()))
        out["value_capped_usd"] = round(vc, 2)
        out["value_capped_frac"] = vc / tc if tc else 0.0
        out["value_capped_per_inv_hour_usd"] = round(vc / inv_h, 2) if inv_h else 0.0
    return out

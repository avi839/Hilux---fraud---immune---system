"""Scoring logic of the AMLworld benchmark on tiny hand-made frames.

    backend\\.venv\\Scripts\\python.exe -m pytest benchmarks\\tests -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from baselines import _window_count  # noqa: E402
from scoring import Costs, admission_hour, evaluate, group_alerts, helix_alerts, top_n_per_hour  # noqa: E402

T0 = pd.Timestamp("2022-09-04 01:00", tz="UTC")


def _tx(rows):
    """rows: (tx_id, minute offset, src, y, usd)."""
    df = pd.DataFrame(rows, columns=["tx_id", "m", "src", "y", "usd"])
    df["ts"] = T0 + pd.to_timedelta(df["m"], unit="min")
    df["hour"] = df["ts"].dt.floor("h")
    return df


TX = _tx([
    ("a", 0, "S1", 0, 10.0), ("b", 5, "S1", 1, 100.0), ("c", 10, "S2", 0, 20.0),
    ("d", 70, "S1", 0, 30.0), ("e", 75, "S3", 1, 1000.0), ("f", 80, "S3", 0, 5.0),
])


def test_group_alerts_by_hour_and_source_scored_by_max():
    scored = pd.DataFrame({"tx_id": list("abcdef"), "score": [0.1, 0.9, 0.5, 0.2, 0.3, 0.8],
                           "flagged": [True, True, True, True, True, False]})
    g = group_alerts(scored, TX)
    assert list(zip(g["hour"].dt.hour, g["key"], g["score"])) == [(1, "S1", 0.9), (1, "S2", 0.5),
                                                                  (2, "S3", 0.3), (2, "S1", 0.2)]
    assert set(g.loc[g["key"] == "S1", "tx_ids"].iloc[0]) == {"a", "b"}
    assert g.loc[g["key"] == "S3", "tx_ids"].iloc[0] == ("e",)  # f is not flagged


def test_top_n_per_hour_takes_each_hours_best():
    a = pd.DataFrame({"hour": [T0, T0, T0, T0 + pd.Timedelta(hours=1)], "key": ["x", "y", "z", "w"],
                      "score": [1.0, 3.0, 2.0, 0.1], "tx_ids": [(), (), (), ()]})
    top = top_n_per_hour(a, 2)
    assert list(top["key"]) == ["y", "z", "w"]


def test_tp_rule_value_per_hour_and_cost_per_catch():
    sel = pd.DataFrame({"hour": [T0, T0, T0], "key": ["S1", "S2", "S3"], "score": [3, 2, 1],
                        "tx_ids": [("a", "b"), ("c",), ("e", "b")]})
    costs = Costs(minutes_per_case=20, analyst_cost_per_hour=30, cost_per_case=10)
    m = evaluate(sel, TX, costs, hours=2, n=5, value_cap_usd=500)
    assert (m["alerts"], m["tp"], m["fp"]) == (3, 2, 1)  # an alert with >= 1 laundering tx is a TP
    assert m["laundering_tx_caught"] == 2 and m["tx_recall"] == 1.0  # b counted once
    assert m["value_captured_usd"] == 1100.0
    assert m["investigator_hours"] == 1.0  # 3 alerts x 20 min
    assert m["value_per_inv_hour_usd"] == 1100.0
    assert m["cost_per_true_catch_usd"] == pytest.approx(3 * 10 / 2)  # 20 min x 30/h = 10 per alert
    assert m["wasted_hours"] == pytest.approx(1 / 3, abs=0.01)
    assert m["precision_at_n"] == pytest.approx(2 / 10)
    assert m["value_capped_usd"] == 600.0  # e capped at 500


def test_no_alerts_gives_no_cost_per_catch():
    m = evaluate(pd.DataFrame({"tx_ids": []}), TX, Costs(), hours=1, n=1)
    assert m["tp"] == 0 and m["cost_per_true_catch_usd"] is None and m["value_per_inv_hour_usd"] == 0.0


def test_admission_hour_ignores_same_hour_displacement():
    h = lambda m: (T0 + pd.Timedelta(minutes=m)).isoformat()  # noqa: E731
    assert admission_hour([[h(1), "ACTIVE"], [h(30), "BACKLOG"], [h(70), "ACTIVE"]]) == T0 + pd.Timedelta(hours=1)
    assert admission_hour([[h(1), "BACKLOG"]]) is None
    assert admission_hour([[h(1), "ACTIVE"], [h(70), "BACKLOG"]]) == T0


def test_helix_alerts_keep_only_tx_seen_by_end_of_hour():
    cases = pd.DataFrame([
        {"case_id": "C-1", "created_ts": T0.isoformat(), "merged_into": "", "priority": 5.0, "risk": 0.9,
         "tx_ids": "a;b;d", "placements": json.dumps([[T0.isoformat(), "ACTIVE"]])},
        {"case_id": "C-2", "created_ts": T0.isoformat(), "merged_into": "C-1", "priority": 9.0, "risk": 0.9,
         "tx_ids": "c", "placements": json.dumps([[T0.isoformat(), "ACTIVE"]])},
        {"case_id": "C-3", "created_ts": (T0 + pd.Timedelta(minutes=75)).isoformat(), "merged_into": "",
         "priority": 1.0, "risk": 0.2, "tx_ids": "e", "placements": json.dumps([])},
    ])
    end = T0 + pd.Timedelta(hours=2)
    act = helix_alerts(cases, TX, T0, end, include_backlog=False)
    assert list(act["key"]) == ["C-1"] and act["tx_ids"].iloc[0] == ("a", "b")  # d is after the hour
    both = helix_alerts(cases, TX, T0, end, include_backlog=True)
    assert list(both["key"]) == ["C-1", "C-3"]  # merged C-2 dropped; C-3 available when created


@settings(max_examples=60, deadline=None)
@given(st.lists(st.tuples(st.integers(0, 3), st.integers(0, 600)), min_size=1, max_size=60))
def test_property_window_count_matches_brute_force(rows):
    """Causal source count over (t - 60 min, t], in (ts, row) order, equals a brute-force count."""
    rows = sorted(rows, key=lambda r: r[1])  # window order: by time, then row
    src = np.array([r[0] for r in rows])
    mins = np.array([r[1] for r in rows], dtype=np.int64)
    order = np.lexsort((np.arange(len(rows)), mins, src))
    got = _window_count(src, mins, order, 60)
    want = [sum(1 for j in range(i + 1) if src[j] == src[i] and mins[i] - 60 < mins[j] <= mins[i])
            for i in range(len(rows))]
    assert list(got) == want

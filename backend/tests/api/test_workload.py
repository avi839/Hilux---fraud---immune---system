"""Workload metrics (helix.cases.workload, design §26, Req 41.10): unit tests and Property 90."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st

from helix.api import AppState, create_app
from helix.cases import Label, WorkloadMeter
from helix.config import HelixConfig
from helix.sim.clock import ManualClock
from tests.cases.helpers import T0, RiskSensor, at, tx

KEYS = {"labelled_last_hour", "labelled_last_hour_by", "labelled_total", "auto_cleared_total",
        "auto_cleared_last_hour", "bulk_requests_total"}


class FakeMono:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_window_is_the_trailing_hour_exclusive_of_its_start():
    clk = FakeMono(1_000.0)
    m = WorkloadMeter(now=clk)
    m.on_label("ana")
    clk.t = 2_000.0
    m.on_label("ben")
    m.on_auto_clear()
    clk.t = 4_600.0  # 1_000 is now exactly 3_600 s old: outside (now - 3600, now]
    s = m.snapshot()
    assert s["labelled_last_hour"] == 1 and s["labelled_last_hour_by"] == {"ben": 1}
    assert s["labelled_total"] == 2 and s["auto_cleared_total"] == 1 and s["auto_cleared_last_hour"] == 1
    clk.t = 10_000.0
    s = m.snapshot()
    assert s["labelled_last_hour"] == 0 and s["auto_cleared_last_hour"] == 0 and s["labelled_total"] == 2


@pytest.mark.integration
def test_metrics_endpoint_has_the_workload_block_fed_by_labels():
    from helix.motifs import MotifEngine

    state = AppState.build(HelixConfig(), ManualClock(T0), learner=False, sensors=[RiskSensor()],
                           sensor_clock=lambda: 0.0, motif_engine=MotifEngine(detectors=()))
    h = state.helix
    cid = h.process(tx(1, "A", "B", 85, at(0))).case_id
    with TestClient(create_app(state, manage_lifecycle=False)) as cl:
        w = cl.get("/api/v1/metrics").json()["workload"]
        assert set(w) == KEYS | {"sim_investigator"} and w["labelled_total"] == 0
        assert w["sim_investigator"]["enabled"] is False  # off by default (task 31.1)
        assert cl.post(f"/api/v1/cases/{cid}/open", json={"investigator": "ana"}).status_code == 200
        assert cl.post(f"/api/v1/cases/{cid}/feedback",
                       json={"by": "ana", "label": Label.CONFIRMED_FRAUD.value}).status_code == 200
        w = cl.get("/api/v1/metrics").json()["workload"]
        assert w["labelled_total"] == 1 and w["labelled_last_hour_by"] == {"ana": 1}
        assert w["auto_cleared_total"] == 0 and w["bulk_requests_total"] == 0
    state.enable_workload()  # idempotent: still one hook
    assert sum(getattr(f, "workload_meter", None) is state.workload for f in h.feedback_hooks) == 1


# --- Property 90 --------------------------------------------------------------------------


@st.composite
def workload_runs(draw):
    """Nondecreasing (monotonic) label and auto-clear times, then a query time at or after them."""
    gaps = draw(st.lists(st.floats(0, 2_000, allow_nan=False), max_size=40))
    kinds = draw(st.lists(st.sampled_from(["ana", "ben", "cat", None]), min_size=len(gaps), max_size=len(gaps)))
    t, events = 0.0, []
    for g, k in zip(gaps, kinds):
        t += g
        events.append((t, k))  # k None = auto-clear
    q = t + draw(st.floats(0, 8_000, allow_nan=False))
    return events, q


# Feature: helix-fraud-immune-system, Property 90: Workload windows count exactly the trailing hour
@pytest.mark.property
@given(run=workload_runs())
def test_workload_windows_count_exactly_the_trailing_hour(run):
    """**Validates: Requirements 41.10**"""
    events, q = run
    clk = FakeMono()
    m = WorkloadMeter(now=clk)
    for t, who in events:
        clk.t = t
        if who is None:
            m.on_auto_clear()
        else:
            m.on_label(who)
    clk.t = q
    s = m.snapshot()
    inside = [(t, w) for t, w in events if q - 3_600 < t <= q]
    by: dict[str, int] = {}
    for _, w in inside:
        if w is not None:
            by[w] = by.get(w, 0) + 1
    assert s["labelled_last_hour_by"] == by
    assert s["labelled_last_hour"] == sum(by.values())
    assert s["auto_cleared_last_hour"] == sum(1 for _, w in inside if w is None)
    assert s["labelled_total"] == sum(1 for _, w in events if w is not None)
    assert s["auto_cleared_total"] == sum(1 for _, w in events if w is None)

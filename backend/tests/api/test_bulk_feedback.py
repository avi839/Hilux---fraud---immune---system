"""POST /api/v1/cases/bulk-feedback (design §26, Req 41.6, 41.7): unit tests and Property 89."""

from __future__ import annotations

from collections import Counter

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from helix.api import AppState, create_app
from helix.cases import CaseStatus, Label
from helix.config import HelixConfig
from helix.sim.clock import ManualClock
from tests.cases.helpers import T0, RiskSensor, at, tx

pytestmark = pytest.mark.integration

URL = "/api/v1/cases/bulk-feedback"


def make_state(n: int, *, soc: bool = False, learner: bool = True) -> tuple[AppState, list[str]]:
    from helix.motifs import MotifEngine

    state = AppState.build(HelixConfig(), ManualClock(T0), learner=learner, soc=soc, sensors=[RiskSensor()],
                           sensor_clock=lambda: 0.0, motif_engine=MotifEngine(detectors=()))
    ids = [state.helix.process(tx(i, f"S{i}", f"D{i}", 85, at(i))).case_id for i in range(n)]
    assert None not in ids
    return state, ids


def client(state: AppState) -> TestClient:
    return TestClient(create_app(state, manage_lifecycle=False))


def view(state: AppState) -> tuple:
    h = state.helix
    cases = tuple((c.case_id, c.status, c.assignee, c.label, c.label_by, c.placement, len(c.history))
                  for c in sorted(h.cases.cases.values(), key=lambda c: c.seq_no))
    return cases, tuple(h.prioritiser.active_ids()), tuple(h.prioritiser.backlog_ids()), tuple(sorted(h.weights.items()))


# --- unit -------------------------------------------------------------------------------


def test_bulk_labels_open_and_own_cases_and_audits_each_once():
    state, (a, b, c) = make_state(3, soc=True)
    h = state.helix
    h.open_case(c, "ana")
    with client(state) as cl:
        r = cl.post(URL, json={"by": "ana", "label": "FALSE_POSITIVE", "note": "batch", "case_ids": [a, b, c]})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["by"] == "ana" and body["label"] == "FALSE_POSITIVE" and body["count"] == 3
        assert body["case_ids"] == [a, b, c] and [x["case_id"] for x in body["cases"]] == [a, b, c]
        assert all(x["status"] == "CLOSED" and x["closure_reason"] == "LABELLED" for x in body["cases"])
        for cid in (a, b, c):
            case = h.cases.get(cid)
            assert (case.status, case.label, case.label_by, case.label_note) == (
                CaseStatus.CLOSED, Label.FALSE_POSITIVE, "ana", "batch")
        labels = [e for e in state.ledger.entries() if e.category == "FEEDBACK_LABEL"]
        assert sorted(e.payload["case_id"] for e in labels) == sorted([a, b, c])  # exactly once each
        assert [x.case_id for x in h.learner.labels] == [a, b, c]
        w = cl.get("/api/v1/metrics").json()["workload"]
        assert w["labelled_total"] == 3 and w["labelled_last_hour"] == 3
        assert w["labelled_last_hour_by"] == {"ana": 3} and w["bulk_requests_total"] == 1


def test_zero_fifty_and_fifty_one_ids():
    state, ids = make_state(51, learner=False)
    with client(state) as cl:
        r = cl.post(URL, json={"by": "ana", "label": "CONFIRMED_FRAUD", "case_ids": []})
        assert r.status_code == 422 and r.json()["failures"] == []
        before = view(state)
        r = cl.post(URL, json={"by": "ana", "label": "CONFIRMED_FRAUD", "case_ids": ids})
        assert r.status_code == 422
        assert [f["case_id"] for f in r.json()["failures"]] == ids and view(state) == before
        r = cl.post(URL, json={"by": "ana", "label": "CONFIRMED_FRAUD", "case_ids": ids[:50]})
        assert r.status_code == 200 and r.json()["count"] == 50
        assert state.helix.cases.get(ids[50]).is_open


def test_most_severe_status_wins_and_every_failing_id_is_listed():
    state, (a, b, c, d) = make_state(4, learner=False)
    h = state.helix
    h.open_case(b, "ben")  # foreign assignee -> 409
    h.open_case(c, "ana")
    h.label(c, "ana", Label.CONFIRMED_FRAUD)  # CLOSED -> 409
    with client(state) as cl:
        before = view(state)
        r = cl.post(URL, json={"by": "ana", "label": "FALSE_POSITIVE", "case_ids": [a, b, c]})
        assert r.status_code == 409
        assert {(f["case_id"], f["status"]) for f in r.json()["failures"]} == {(b, 409), (c, 409)}
        r = cl.post(URL, json={"by": "ana", "label": "FALSE_POSITIVE", "case_ids": [a, b, "C-999"]})
        assert r.status_code == 404
        assert {(f["case_id"], f["status"]) for f in r.json()["failures"]} == {(b, 409), ("C-999", 404)}
        r = cl.post(URL, json={"by": "ana", "label": "FALSE_POSITIVE", "case_ids": [a, a, "C-999", d]})
        assert r.status_code == 422
        assert {(f["case_id"], f["status"]) for f in r.json()["failures"]} == {(a, 422), ("C-999", 404)}
        assert view(state) == before
        # Malformed bodies are 422 before any Case is looked at.
        assert cl.post(URL, json={"by": "ana", "label": "AUTO_CLEARED", "case_ids": [a]}).status_code == 422
        assert cl.post(URL, json={"by": "ana", "label": "FALSE_POSITIVE", "note": "x" * 2_001,
                                  "case_ids": [a]}).status_code == 422
        assert cl.post(URL, json={"by": " ", "label": "FALSE_POSITIVE", "case_ids": [a]}).status_code == 422
        assert view(state) == before


# --- Property 89 --------------------------------------------------------------------------


@st.composite
def bulk_requests(draw):
    """Cases in random states plus a request of 1-60 ids with duplicates, unknown ids,
    CLOSED Cases and other assignees."""
    n = draw(st.integers(1, 6))
    states = draw(st.lists(st.sampled_from(["OPEN", "ana", "ben", "CLOSED"]), min_size=n, max_size=n))
    pool = [f"C-{i}" for i in range(1, n + 1)] + ["C-404", "C-405"]
    ids = draw(st.lists(st.sampled_from(pool), min_size=1, max_size=60)
               | st.lists(st.sampled_from(pool), min_size=1, max_size=len(pool), unique=True))
    return states, ids, draw(st.sampled_from(["ana", "ben"])), draw(st.sampled_from(list(Label)))


# Feature: helix-fraud-immune-system, Property 89: Bulk feedback is all-or-nothing
@pytest.mark.property
@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(req=bulk_requests())
def test_bulk_feedback_is_all_or_nothing(req):
    """**Validates: Requirements 41.6, 41.7**"""
    states, ids, by, label = req
    state, cids = make_state(len(states))
    h = state.helix
    for cid, s in zip(cids, states):
        if s in ("ana", "ben", "CLOSED"):
            h.open_case(cid, "ana" if s == "CLOSED" else s)
        if s == "CLOSED":
            h.label(cid, "ana", Label.CONFIRMED_FRAUD)
    expected: dict[str, int] = {}
    if len(ids) > 50:
        expected = {i: 422 for i in ids}
    else:
        counts = Counter(ids)
        for i in ids:
            case = h.cases.cases.get(i)
            if counts[i] > 1:
                expected[i] = 422
            elif case is None:
                expected[i] = 404
            elif case.status is CaseStatus.CLOSED or (case.assignee is not None and case.assignee != by):
                expected[i] = 409
    before = view(state)
    with client(state) as cl:
        r = cl.post(URL, json={"by": by, "label": label.value, "note": "", "case_ids": ids})
    assert r.status_code != 500
    if not expected:
        assert r.status_code == 200, r.text
        for i in ids:
            c = h.cases.get(i)
            assert (c.status, c.label, c.label_by) == (CaseStatus.CLOSED, label, by)
    else:
        sev = {422: 3, 404: 2, 409: 1}
        assert r.status_code == max(expected.values(), key=sev.__getitem__)
        got = {f["case_id"]: f["status"] for f in r.json()["failures"]}
        assert got == expected
        assert view(state) == before  # Cases, Active_Queue, Backlog and Sensor_Weights unchanged

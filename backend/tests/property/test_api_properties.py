"""API_Server pagination property (Req 23.8)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st

from helix.api import AppState, create_app
from helix.codec import TransactionEvent
from helix.config import HelixConfig
from helix.fusion import FusedAssessment, Mass
from helix.sim.clock import ManualClock

pytestmark = [pytest.mark.property, pytest.mark.integration]

T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
API = "/api/v1"
RISK = 0.95  # above the default alerting threshold: every scored event opens a Case


def fa(tx: str, r: float) -> FusedAssessment:
    m = Mass(2 * r - 1, 0.0, 2 - 2 * r)
    return FusedAssessment(tx, 1, m, m.f, m.f + m.t, m.t, 0.0, False, ())


def add_cases(state: AppState, start: int, n: int) -> None:
    """Route ``n`` alerting events from distinct accounts through the real Case_Manager."""
    cm = state.helix.cases
    for i in range(start, start + n):
        ev = TransactionEvent.model_validate({"tx_id": f"t{i}", "ts": T0.isoformat(), "src": f"S{i}",
                                              "dst": f"D{i}", "amount": "100.00", "currency": "USD"})
        cm.on_scored_event(ev, fa(ev.tx_id, RISK), RISK)
    cm.drain_changes()


# Feature: helix-fraud-immune-system, API pagination: every Case exactly once (task 11.1)
@settings(max_examples=40, deadline=None)
@given(n=st.integers(0, 120), page_size=st.integers(1, 100), extra=st.integers(0, 10))
def test_pagination_returns_every_case_exactly_once(n, page_size, extra):
    """Following next_cursor from the first page returns every Case exactly once, in
    creation order, each page has at most page_size items, and the token is null exactly
    on the last page; Cases created mid-walk appear once on a later page.

    **Validates: Requirements 23.8**
    """
    state = AppState.build(HelixConfig(), ManualClock(T0), learner=False)
    add_cases(state, 0, n)
    client = TestClient(create_app(state, manage_lifecycle=False))

    def ids() -> list[str]:
        cases = state.helix.cases.cases
        return sorted(cases, key=lambda c: cases[c].seq_no)

    initial = ids()
    assert len(initial) == n
    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        params = {"page_size": page_size, **({"cursor": cursor} if cursor else {})}
        r = client.get(f"{API}/cases", params=params)
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["items"]) <= page_size
        seen += [c["case_id"] for c in body["items"]]
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            break
        assert len(body["items"]) == page_size  # only the last page may be short
        if pages == 1 and extra:
            add_cases(state, n, extra)  # new Cases while the client is still paging

    # Every Case that existed when the walk started (plus, when the walk continued past
    # the first page, every Case created mid-walk) exactly once, in creation order.
    assert seen == (ids() if pages > 1 else initial)
    assert len(set(seen)) == len(seen)

"""Property tests for helix.replay (design Properties 54, 55)."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from helix.cases import Case, CaseStatus, Placement
from helix.codec import TransactionEvent
from helix.config import HelixConfig, SeverityTier
from helix.config.schema import GraphConfig, SensorConfig
from helix.replay import (
    DefaultStage1Engine,
    TimestampOutOfRangeError,
    frame_at,
    mismatches,
    recompute,
    replay_timeline,
)
from helix.storage import Storage

pytestmark = pytest.mark.property

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
ACCOUNTS = ["A", "B", "C", "D", "E"]
WINDOW_S = 3_600.0
CFG = HelixConfig(sensors=SensorConfig(min_history_count=2), graph=GraphConfig(window_s=WINDOW_S))
db_settings = settings(
    max_examples=30, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)


def zero_clock() -> float:
    return 0.0


def factory(snap, weights, cfg):
    return DefaultStage1Engine.from_snapshot(snap, weights, cfg, clock=zero_clock)


def make_case(accounts, tx_ids, updated_at) -> Case:
    return Case(
        case_id="C-1", seq_no=1, status=CaseStatus.OPEN, tier=SeverityTier.HIGH, risk=0.8,
        risk_at_creation=0.8, expected_loss=Decimal("0"), priority=Decimal("0"),
        accounts=set(accounts), tx_ids=set(tx_ids), assignee=None, created_at=updated_at,
        updated_at=updated_at, admitted_period=None, placement=Placement.ACTIVE,
        merged_into=None, label=None, label_note=None, escalated=False, history=[],
    )  # fmt: skip


# One raw event: (minutes after previous, late-by minutes, src, dst, cents, channel, location)
raw_event = st.tuples(
    st.integers(0, 90),
    st.sampled_from([0, 0, 0, 0, 3, 45]),  # occasional out-of-order arrival
    st.sampled_from(ACCOUNTS),
    st.sampled_from(ACCOUNTS + ["X", "Y"]),
    st.integers(1, 500_000),
    st.sampled_from(["web", "pos", None]),
    st.sampled_from([None, (52.52, 13.40), (6.52, 3.38)]),
)


def build_events(raws) -> list[TransactionEvent]:
    out, t = [], 0
    for i, (gap, late, src, dst, cents, ch, loc) in enumerate(raws):
        t += gap
        out.append(
            TransactionEvent(
                tx_id=f"T{i:03d}", ts=T0 + timedelta(minutes=max(0, t - late)), src=src, dst=dst,
                amount=Decimal(cents) / 100, currency="USD", channel=ch,
                lat=None if loc is None else loc[0], lon=None if loc is None else loc[1],
            )
        )  # fmt: skip
    return out


def run_live(storage: Storage, events, snapshot_after: int | None):
    eng = DefaultStage1Engine(CFG, clock=zero_clock)
    clock = None
    for seq, e in enumerate(events, start=1):
        fa = eng.process(e)
        storage.record_event(e, seq_no=seq)
        storage.record_assessment(fa)
        clock = e.ts if clock is None else max(clock, e.ts)  # event-driven SimClock
        if snapshot_after == seq:
            storage.snapshot(*eng.snapshot_state(last_seq_no=seq), clock)
    assert storage.flush()


scenario = st.lists(raw_event, min_size=2, max_size=40).flatmap(
    lambda raws: st.tuples(
        st.just(raws),
        st.none() | st.integers(1, len(raws)),  # snapshot after this seq_no
        st.sets(st.sampled_from(ACCOUNTS), min_size=1, max_size=3),
        st.sets(st.integers(0, len(raws) - 1), min_size=1, max_size=4),
        st.sampled_from([0.0, 0.5, 2.0, 24.0]),
    )
)


def _case_for(events, accounts, picks):
    tx = {events[i].tx_id for i in picks if events[i].src in accounts or events[i].dst in accounts}
    assume(tx)
    return make_case(accounts, tx, max(e.ts for e in events))


@db_settings
@given(sc=scenario)
def test_p54_replay_reproduces_stored_assessments(tmp_path, sc):
    """Feature: helix-fraud-immune-system, Property 54: Replay reproduces stored assessments.

    **Validates: Requirements 17.8**
    """
    raws, snap_after, accounts, picks, lead_h = sc
    events = build_events(raws)
    case = _case_for(events, accounts, picks)
    with Storage(tmp_path / f"{uuid.uuid4().hex}.duckdb").open() as storage:
        run_live(storage, events, snap_after)
        tl = replay_timeline(case, storage, lead_h)
        run = recompute(case, storage, factory, CFG, timeline=tl)
        assert len(run.assessments) == len(tl.entries) > 0
        assert [a.tx_id for a in run.assessments] == [e.tx_id for e in tl.entries]
        assert all(s is not None for s in run.stored)
        assert mismatches(run, tol=1e-9) == []


@db_settings
@given(sc=scenario, frac=st.floats(0.0, 1.0), on_event=st.booleans())
def test_p55_point_in_time_isolation(tmp_path, sc, frac, on_event):
    """Feature: helix-fraud-immune-system, Property 55: Point-in-time isolation.

    **Validates: Requirements 17.5, 17.7**
    """
    raws, _, accounts, picks, lead_h = sc
    events = build_events(raws)
    case = _case_for(events, accounts, picks)
    with Storage(tmp_path / f"{uuid.uuid4().hex}.duckdb").open() as storage:
        run_live(storage, events, None)
        tl = replay_timeline(case, storage, lead_h)
        in_span = [e.ts for e in tl.entries]
        if on_event and in_span:
            t = in_span[int(frac * (len(in_span) - 1))]
        else:
            t = tl.start + (tl.end - tl.start) * frac
        fr = frame_at(case, storage, t, window_s=WINDOW_S, hops=2, max_nodes=50, timeline=tl)

        lo = t - timedelta(seconds=WINDOW_S)
        assert fr.window_start == lo
        assert all(lo <= e.ts <= t for e in fr.subgraph.edges)
        assert all(e.ts <= t for e in fr.entries)
        assert [e.tx_id for e in fr.entries] == [e.tx_id for e in tl.entries if e.ts <= t]
        by_tx = {e.tx_id: e.ts for e in events}
        assert all(by_tx[a.tx_id] <= t for a in fr.assessments)
        # Completeness of the window: every stored edge in [lo, t] touching a Case
        # account is in the subgraph (no truncation at this size).
        direct = {e.tx_id for e in events if lo <= e.ts <= t and (e.src in accounts or e.dst in accounts)}
        assert direct <= {e.tx_id for e in fr.subgraph.edges}

        for bad in (tl.start - timedelta(microseconds=1), tl.end + timedelta(microseconds=1)):
            with pytest.raises(TimestampOutOfRangeError) as ei:
                frame_at(case, storage, bad, window_s=WINDOW_S, timeline=tl)
            assert (ei.value.start, ei.value.end) == (tl.start, tl.end)

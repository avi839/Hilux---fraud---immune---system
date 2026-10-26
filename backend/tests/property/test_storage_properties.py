"""Property tests for helix.storage (design Properties 71, 72).

P71 covers the state available now: Case / Evidence_Chain / history / feedback / weight /
antibody / motif rows (as dict payloads), Profile_Store (DNA) and CPD snapshots. Queue
membership and labels are part of the Case payloads, whose models are not final.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from helix.changepoint import ChangePointDetector
from helix.codec import TransactionEvent
from helix.graph import TemporalGraph
from helix.memory import ProfileStore
from helix.storage import JSON_TABLES, Storage, profile_store_from_dict, profile_store_to_dict

pytestmark = pytest.mark.property

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
ACCOUNTS = ["A", "B", "C", "D"]
db_settings = settings(
    max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)

offsets = st.sampled_from([timezone.utc, timezone(timedelta(hours=2)), timezone(timedelta(hours=-5, minutes=-30))])
aware = st.integers(0, 10 * 86_400 * 1000).map(lambda ms: T0 + timedelta(milliseconds=ms))

leaf = (
    st.none() | st.booleans() | st.integers(-(2**63), 2**63) | st.text(max_size=8)
    | st.floats(allow_nan=False) | aware | st.datetimes()
    | st.decimals(allow_nan=False, allow_infinity=False, places=2)
)  # fmt: skip
keys = st.text(max_size=6) | st.sampled_from(["$dt", "$set", "$map", "$tuple"])
payloads = st.recursive(
    leaf,
    lambda ch: st.lists(ch, max_size=4)
    | st.tuples(ch, ch)
    | st.frozensets(st.integers(-5, 5) | st.text(max_size=3), max_size=4).map(set)
    | st.dictionaries(keys, ch, max_size=4),
    max_leaves=12,
)


def row_for(table: str):
    spec = JSON_TABLES[table]
    fields = {}
    for col, typ in spec.columns:
        if col == spec.auto_key:
            continue
        if typ == "BIGINT":
            fields[col] = st.integers(0, 5)
        elif typ == "TIMESTAMP":
            fields[col] = aware
        elif typ == "BOOLEAN":
            fields[col] = st.booleans()
        else:
            fields[col] = st.sampled_from(["k1", "k2", "k3"]) if col in spec.keys else st.text(max_size=6)
    return st.fixed_dictionaries(fields, optional={"data": payloads, "extra": payloads})


table_writes = st.lists(
    st.sampled_from(sorted(JSON_TABLES)).flatmap(lambda t: st.tuples(st.just(t), row_for(t))),
    max_size=25,
)

def _event(i, spec):
    m, src, dst, cents, ch, loc, tz = spec
    return TransactionEvent(
        tx_id=f"tx{i}", ts=(T0 + timedelta(minutes=m)).astimezone(tz),
        src=src, dst=dst, amount=Decimal(cents) / 100, currency="USD", channel=ch,
        lat=None if loc is None else loc[0], lon=None if loc is None else loc[1],
    )  # fmt: skip


# Unique tx_ids (the ingestor deduplicates before Storage); minutes over 3 days, so
# windows of 1-24 h see eviction, exact-boundary timestamps and ties.
events = st.lists(
    st.tuples(
        st.integers(0, 3 * 24 * 60),
        st.sampled_from(ACCOUNTS),
        st.sampled_from(ACCOUNTS + ["X", "Y"]),
        st.integers(1, 10_000_00),
        st.none() | st.sampled_from(["web", "pos"]),
        st.none() | st.tuples(st.floats(-90, 90), st.floats(-180, 180)),
        offsets,
    ),
    max_size=40,
).map(lambda specs: [_event(i, s) for i, s in enumerate(specs)])


def expected_rows(writes):
    out: dict[str, dict] = {t: {} for t in JSON_TABLES}
    counters = {t: 0 for t in JSON_TABLES}
    for table, row in writes:
        spec = JSON_TABLES[table]
        row = dict(row)
        if spec.auto_key:
            counters[table] += 1
            row[spec.auto_key] = counters[table]
        key = tuple(row[k] for k in spec.keys)
        if spec.append_only and key in out[table]:
            continue  # insert-only: the earlier record is kept unchanged
        out[table][key] = row
    return {t: [rows[k] for k in sorted(rows)] for t, rows in out.items()}


# Feature: helix-fraud-immune-system, Property 71: Persistence round-trip
@pytest.mark.property
# **Validates: Requirements 24.5, 24.6**
@db_settings
@given(writes=table_writes, evs=events, ops=st.lists(st.tuples(st.sampled_from(["withhold", "exclude", "release"]), st.integers(0, 39)), max_size=6), data=st.data())
def test_p71_persistence_round_trip(tmp_path, writes, evs, ops, data):
    path = tmp_path / f"{uuid.uuid4().hex}.duckdb"
    ps, cpd = ProfileStore(half_life_days=3.0), ChangePointDetector()
    for i, e in enumerate(evs):
        ps.apply(e, withhold=data.draw(st.booleans(), label=f"withhold{i}"))
        cpd.observe(e)
    for op, i in ops:
        if evs:
            e = evs[i % len(evs)]
            getattr(ps, op)(e.src, [e.tx_id])

    s = Storage(path).open()
    for table, row in writes:
        s.enqueue(table, row)
    snap_ts = T0 + timedelta(days=4)
    assert s.close(lambda: (profile_store_to_dict(ps), cpd.to_dict(), snap_ts))

    s = Storage(path).open()
    r = s.restore()
    s.close()
    exp = expected_rows(writes)
    for table in JSON_TABLES:
        assert getattr(r, table) == exp[table], table

    assert r.snapshot is not None and r.snapshot.ts == snap_ts
    ps2 = profile_store_from_dict(r.snapshot.dna)
    assert profile_store_to_dict(ps2) == profile_store_to_dict(ps)
    cpd2 = ChangePointDetector()
    cpd2.load_dict(r.snapshot.cpd)
    assert cpd2.to_dict() == cpd.to_dict()
    for a in ACCOUNTS:
        assert ps2.view(a) == ps.view(a) and ps2.withheld(a) == ps.withheld(a)

    # Restored state behaves identically on the next event.
    nxt = TransactionEvent(tx_id="next", ts=T0 + timedelta(days=5), src="A", dst="Z",
                           amount=Decimal("12.34"), currency="USD")
    ps.apply(nxt), ps2.apply(nxt)
    assert ps2.view("A") == ps.view("A")
    assert cpd2.observe(nxt).to_dict() == cpd.observe(nxt).to_dict()


# Feature: helix-fraud-immune-system, Property 72: Graph rebuild equals the pre-shutdown graph
@pytest.mark.property
# **Validates: Requirements 24.7**
@db_settings
@given(evs=events, window_h=st.sampled_from([1, 6, 24]), arrival=st.randoms(use_true_random=False))
def test_p72_graph_rebuild_equals_pre_shutdown(tmp_path, evs, window_h, arrival):
    path = tmp_path / f"{uuid.uuid4().hex}.duckdb"
    window_s = window_h * 3_600.0
    order = list(evs)
    arrival.shuffle(order)  # late and too-old arrivals occur
    live = TemporalGraph(window_s)
    s = Storage(path).open()
    for seq, e in enumerate(order):
        live.add(e)
        s.record_event(e, seq_no=seq)  # every processed event is stored
    assert s.close()

    s = Storage(path).open()
    rebuilt = s.rebuild_graph(window_s)
    s.close()
    assert set(rebuilt.nodes()) == set(live.nodes())
    assert rebuilt.edges() == live.edges()
    assert rebuilt.latest == live.latest
    assert {e.tx_id: e.ts.utcoffset() for e in rebuilt.edges()} == {e.tx_id: e.ts.utcoffset() for e in live.edges()}

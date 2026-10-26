"""Property tests for helix.ingest (Stream_Ingestor)."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from helix.codec import TransactionEvent, format
from helix.config.schema import IngestConfig
from helix.ingest import IngestStatus, LatenessKind, StreamIngestor
from helix.sim.clock import ManualClock
from tests.property.test_codec_properties import events, invalid_documents

T0 = datetime(2024, 6, 3, tzinfo=timezone.utc)
DAY = 86_400
accounts = st.sampled_from(["A", "B", "C"])
offsets = st.integers(-12, 12).map(lambda h: timezone(timedelta(hours=h)))


def ev(tx_id: str, src: str, ts: datetime, amount: str = "10.00") -> TransactionEvent:
    return TransactionEvent(tx_id=tx_id, ts=ts, src=src, dst="D", amount=Decimal(amount), currency="EUR")


# Feature: helix-fraud-immune-system, Property 2: Codec rejects invalid documents completely
@pytest.mark.property
@given(case=invalid_documents(), before=events(), after=events())
def test_ingestor_rejects_invalid_documents_and_continues(case, before, after):
    """Ingestor side: malformed counter +1, rejected log entry, next valid document accepted.

    **Validates: Requirements 1.10, 2.8**
    """
    raw, broken = case
    before = before.model_copy(update={"tx_id": "before"})
    after = after.model_copy(update={"tx_id": "after"})
    clock = ManualClock(T0)
    ing = StreamIngestor(clock=clock)
    assert ing.submit(before).status is IngestStatus.ACCEPTED
    clock.advance(timedelta(seconds=1))

    r = ing.submit(raw)
    assert r.status is IngestStatus.REJECTED and r.error.fields == broken
    assert ing.counters.malformed == 1 and len(ing) == 1
    (rec,) = ing.rejected_log.records()
    assert rec.raw == raw and rec.received_at == clock.now() and set(rec.fields) == broken

    assert ing.submit(after).status is IngestStatus.ACCEPTED
    assert {a.event.tx_id for a in ing.drain()} == {"before", "after"}
    assert ing.counters.malformed == 1


# Feature: helix-fraud-immune-system, Property 3: Exactly-once delivery
@pytest.mark.property
@given(data=st.data())
def test_exactly_once_delivery(data):
    """**Validates: Requirements 1.1, 1.6, 1.7**"""
    specs = data.draw(st.lists(st.tuples(accounts, st.integers(0, 3600)), min_size=1, max_size=25))
    base = [ev(f"T{i}", a, T0 + timedelta(seconds=s)) for i, (a, s) in enumerate(specs)]
    order = data.draw(st.lists(st.integers(0, len(base) - 1), min_size=1, max_size=3 * len(base)))

    clock = ManualClock(T0)
    ing = StreamIngestor(IngestConfig(dedup_ttl_s=7 * DAY), clock)  # submissions span < 4 days
    first_seen: dict[str, TransactionEvent] = {}
    delivered = []
    for k, i in enumerate(order):
        e = base[i]
        if k and data.draw(st.booleans()):  # a later copy may differ in content
            e = e.model_copy(update={"amount": Decimal("77.77")})
        doc = format(e) if data.draw(st.booleans()) else e
        r = ing.submit(doc)
        expected = IngestStatus.DUPLICATE if e.tx_id in first_seen else IngestStatus.ACCEPTED
        assert r.status is expected
        first_seen.setdefault(e.tx_id, e)
        clock.advance(timedelta(seconds=data.draw(st.integers(0, 3600))))
        if data.draw(st.booleans()):
            if (a := ing.pop()) is not None:
                delivered.append(a)
    delivered += ing.drain()

    ids = [a.event.tx_id for a in delivered]
    assert len(ids) == len(set(ids)) == len(first_seen)
    assert {a.event.tx_id: a.event for a in delivered} == first_seen
    assert ing.counters.duplicate == len(order) - len(first_seen)


# Feature: helix-fraud-immune-system, Property 4: Per-account delivery order
@pytest.mark.property
@given(
    specs=st.lists(st.tuples(accounts, st.integers(0, 15), offsets), min_size=1, max_size=60),
    data=st.data(),
)
def test_per_account_delivery_order(specs, data):
    """**Validates: Requirements 1.2, 1.3**"""
    interleave = data.draw(st.booleans())
    ing = StreamIngestor()
    queued: dict[str, list[tuple[datetime, int]]] = defaultdict(list)  # model: (ts, arrival index)
    index: dict[str, int] = {}
    delivered = []

    def check_pop():
        a = ing.pop()
        acct = a.event.src
        assert (a.event.ts, index[a.event.tx_id]) == min(queued[acct])  # minimum of its account
        queued[acct].remove((a.event.ts, index[a.event.tx_id]))
        delivered.append(a)

    for i, (acct, sec, tz) in enumerate(specs):
        ts = (T0 + timedelta(seconds=sec)).astimezone(tz)  # same instant may carry different offsets
        ing.submit(ev(f"T{i}", acct, ts))
        index[f"T{i}"] = i
        queued[acct].append((ts, i))
        if interleave and data.draw(st.booleans()):
            check_pop()
    while len(ing):
        check_pop()

    if not interleave:  # everything was queued together
        for acct in {s[0] for s in specs}:
            got = [index[a.event.tx_id] for a in delivered if a.event.src == acct]
            want = sorted((i for i, s in enumerate(specs) if s[0] == acct), key=lambda i: (specs[i][1], i))
            assert got == want


# Feature: helix-fraud-immune-system, Property 5: Lateness classification
@pytest.mark.property
@given(
    latest=st.datetimes(min_value=datetime(2000, 1, 1), max_value=datetime(2090, 1, 1), timezones=offsets),
    tol=st.integers(0, DAY) | st.floats(0, DAY),
    data=st.data(),
)
def test_lateness_classification(latest, tol, data):
    """**Validates: Requirements 1.4, 1.5**"""
    tol_us = round(tol * 1_000_000)
    lag_us = data.draw(
        st.integers(-2 * DAY * 10**6, 2 * DAY * 10**6) | st.sampled_from([tol_us - 1, tol_us, tol_us + 1, 0, 1])
    )
    ts = latest - timedelta(microseconds=lag_us)
    ing = StreamIngestor(IngestConfig(lateness_tolerance_s=tol))
    ing.submit(ev("first", "A", latest))
    assert ing.pop().lateness.kind is LatenessKind.IN_ORDER
    ing.submit(ev("second", "A", ts))
    a = ing.pop()

    lag = (latest - ts).total_seconds()
    assert a is not None and a.event.tx_id == "second"  # always delivered
    assert a.lateness.late == (lag > tol)
    assert a.lateness.seconds == (lag if lag > tol else 0.0)
    assert (a.lateness.kind is LatenessKind.OUT_OF_ORDER) == (0 < lag <= tol)
    assert ing.counters.out_of_order == (1 if 0 < lag <= tol else 0)
    assert ing.counters.late == (1 if lag > tol else 0)


POOL = [ev(f"P{i}", f"S{i % 13}", T0 + timedelta(seconds=i % 97)) for i in range(2000)]


# Feature: helix-fraud-immune-system, Property 6: Backpressure hysteresis
@pytest.mark.property
@given(
    limit=st.integers(100, 600) | st.sampled_from([1_000_000]),
    bursts=st.lists(st.tuples(st.booleans(), st.integers(1, 900)), min_size=1, max_size=8),
)
def test_backpressure_hysteresis(limit, bursts):
    """Limits are drawn small (plus the 1,000,000 maximum) so bursts actually cross them.

    **Validates: Requirements 1.8, 1.9**
    """
    ing = StreamIngestor(IngestConfig(backpressure_limit=limit))
    flag, length, next_ev, popped = False, 0, 0, []
    for enqueue, n in bursts:
        for _ in range(n):
            if enqueue:
                if next_ev == len(POOL):
                    break
                ing.submit(POOL[next_ev])
                next_ev += 1
                length += 1
            else:
                a = ing.pop()
                if a is None:
                    assert length == 0
                    break
                popped.append(a.event.tx_id)
                length -= 1
            if length > limit:
                flag = True
            elif length <= limit // 2:
                flag = False
            assert len(ing) == length and ing.backpressure is flag
    popped += [a.event.tx_id for a in ing.drain()]
    assert sorted(popped) == sorted(e.tx_id for e in POOL[:next_ev])  # nothing dropped

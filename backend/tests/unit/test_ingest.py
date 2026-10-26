"""Unit tests for helix.ingest (Stream_Ingestor)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from helix.codec import TransactionEvent, format
from helix.config.schema import IngestConfig
from helix.ingest import (
    InMemoryRejectedLog,
    IngestStatus,
    LatenessKind,
    RejectedLog,
    StreamIngestor,
)
from helix.sim.clock import ManualClock

T0 = datetime(2024, 6, 3, 9, 0, tzinfo=timezone.utc)
SAMPLE = Path(__file__).resolve().parents[3] / "docs" / "sample_transactions.jsonl"


def ev(tx_id: str, src: str = "A", ts: datetime = T0, amount: str = "10.00") -> TransactionEvent:
    return TransactionEvent(tx_id=tx_id, ts=ts, src=src, dst="D", amount=Decimal(amount), currency="EUR")


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def test_accepts_raw_json_and_event_objects():
    ing = StreamIngestor()
    r1 = ing.submit(format(ev("T1")))
    r2 = ing.submit(ev("T2", ts=at(1)))
    assert r1.status is IngestStatus.ACCEPTED and r1.tx_id == "T1" and r1.accepted
    assert r2.status is IngestStatus.ACCEPTED
    out = ing.drain()
    assert [a.event.tx_id for a in out] == ["T1", "T2"]
    assert out[0].event == ev("T1")
    assert all(a.lateness.kind is LatenessKind.IN_ORDER for a in out)
    assert ing.counters.accepted == 2 and ing.counters.delivered == 2
    assert ing.pop() is None and len(ing) == 0


def test_duplicate_is_discarded_before_enqueue_even_if_content_differs():
    ing = StreamIngestor()
    ing.submit(ev("T1", amount="1.00"))
    r = ing.submit(ev("T1", amount="999.00"))
    assert r.status is IngestStatus.DUPLICATE and r.tx_id == "T1"
    assert len(ing) == 1 and ing.counters.duplicate == 1
    (only,) = ing.drain()
    assert only.event.amount == Decimal("1.00")
    # Still a duplicate after delivery.
    assert ing.submit(format(ev("T1"))).status is IngestStatus.DUPLICATE
    assert ing.counters.duplicate == 2


def test_dedup_ttl_eviction_boundary():
    clock = ManualClock(T0)
    ing = StreamIngestor(IngestConfig(dedup_ttl_s=3600), clock)
    ing.submit(ev("T1"))
    clock.advance(timedelta(seconds=3600))  # age == ttl: still retained
    assert ing.submit(ev("T1")).status is IngestStatus.DUPLICATE
    clock.advance(timedelta(microseconds=1))  # age > ttl: evicted
    assert ing.submit(ev("T1")).status is IngestStatus.ACCEPTED
    assert ing.counters.duplicate == 1 and ing.counters.accepted == 2


def test_rejected_document_is_logged_counted_and_ingestion_continues():
    clock = ManualClock(T0)
    log = InMemoryRejectedLog()
    ing = StreamIngestor(clock=clock, rejected_log=log)
    bad = json.dumps({"tx_id": "X", "ts": "2024-06-03T09:00:00Z", "src": "A", "dst": "B",
                      "amount": "-20.00", "currency": "euro"})
    r = ing.submit(bad)
    assert r.status is IngestStatus.REJECTED and r.tx_id is None
    assert r.error is not None and r.error.fields == {"amount", "currency"}
    assert ing.counters.malformed == 1 and len(ing) == 0
    (rec,) = log.records()
    assert rec.raw == bad and rec.received_at == T0 and rec.fields == ("amount", "currency")
    assert "amount" in rec.reason
    assert ing.submit(ev("T2")).status is IngestStatus.ACCEPTED
    # A rejected tx_id is not remembered for dedup.
    assert ing.submit(ev("X")).status is IngestStatus.ACCEPTED


def test_malformed_json_and_bytes_are_rejected():
    ing = StreamIngestor()
    assert ing.submit("{not json").status is IngestStatus.REJECTED
    assert ing.submit(b"\xff\xfe").status is IngestStatus.REJECTED
    recs = ing.rejected_log.records()
    assert [r.fields for r in recs] == [("$",), ("$",)]
    assert recs[1].raw == b"\xff\xfe".decode("utf-8", errors="replace")
    assert ing.counters.malformed == 2


def test_in_memory_log_satisfies_interface():
    assert isinstance(InMemoryRejectedLog(), RejectedLog)


def test_per_account_order_with_arrival_tie_break_and_token_fifo():
    ing = StreamIngestor()
    ing.submit(ev("A10", "A", at(10)))
    ing.submit(ev("B5", "B", at(5)))
    ing.submit(ev("A1", "A", at(1)))
    ing.submit(ev("A1b", "A", at(1)))  # tie with A1: arrival order
    out = [a.event.tx_id for a in ing.drain()]
    # Tokens: A, B, A, A -> A's min, B's min, A's min, A's min.
    assert out == ["A1", "B5", "A1b", "A10"]
    assert ing.counters.out_of_order == 0 and ing.counters.late == 0


def test_lateness_late_out_of_order_and_in_order():
    ing = StreamIngestor(IngestConfig(lateness_tolerance_s=300))
    ing.submit(ev("T1", ts=at(60)))
    ing.pop()
    ing.submit(ev("T2", ts=at(57)))  # lag 180 s <= 300: out of order
    ing.submit(ev("T3", ts=at(55)))  # lag 300 s == tol: out of order
    ing.submit(ev("T4", ts=at(50)))  # lag 600 s > tol: late
    ing.submit(ev("T5", ts=at(60)))  # lag 0: in order
    # Queued together, so they leave in ts order: T4, T3, T2, T5; latest stays at 60.
    out = {a.event.tx_id: a.lateness for a in ing.drain()}
    assert out["T4"].kind is LatenessKind.LATE and out["T4"].seconds == 600.0 and out["T4"].late
    assert out["T3"].kind is LatenessKind.OUT_OF_ORDER and out["T3"].seconds == 0.0
    assert out["T2"].kind is LatenessKind.OUT_OF_ORDER
    assert out["T5"].kind is LatenessKind.IN_ORDER
    assert ing.counters.late == 1 and ing.counters.out_of_order == 2
    assert ing.latest_processed("A") == at(60)


def test_zero_tolerance_marks_any_positive_lag_late():
    ing = StreamIngestor(IngestConfig(lateness_tolerance_s=0))
    ing.submit(ev("T1", ts=at(1)))
    ing.pop()
    ing.submit(ev("T2", ts=at(1) - timedelta(microseconds=1)))
    a = ing.pop()
    assert a.lateness.late and a.lateness.seconds == pytest.approx(1e-6)


def test_lateness_is_per_account():
    ing = StreamIngestor()
    ing.submit(ev("A", "A", at(100)))
    ing.pop()
    ing.submit(ev("B", "B", at(0)))
    assert ing.pop().lateness.kind is LatenessKind.IN_ORDER


def test_backpressure_hysteresis_and_no_drops():
    ing = StreamIngestor(IngestConfig(backpressure_limit=100))
    for i in range(100):
        assert not ing.submit(ev(f"T{i}", src=f"S{i % 7}")).backpressure
    r = ing.submit(ev("T100"))
    assert r.backpressure and ing.backpressure and len(ing) == 101
    for _ in range(50):
        ing.pop()
    assert len(ing) == 51 and ing.backpressure  # above limit // 2: unchanged
    ing.pop()
    assert len(ing) == 50 and not ing.backpressure
    assert len(ing.drain()) == 50 and ing.counters.delivered == 101


def test_counters_returns_snapshot():
    ing = StreamIngestor()
    c = ing.counters
    c.accepted = 99
    assert ing.counters.accepted == 0


@pytest.mark.asyncio
async def test_next_event_waits_for_submit():
    ing = StreamIngestor()
    task = asyncio.create_task(ing.next_event())
    await asyncio.sleep(0)
    assert not task.done()
    ing.submit(ev("T1"))
    got = await asyncio.wait_for(task, 1)
    assert got.event.tx_id == "T1"
    ing.submit(ev("T2", ts=at(1)))
    assert (await ing.next_event()).event.tx_id == "T2"


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_lose_events():
    ing = StreamIngestor()
    t1 = asyncio.create_task(ing.next_event())
    await asyncio.sleep(0)
    t1.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t1
    t2 = asyncio.create_task(ing.next_event())
    await asyncio.sleep(0)
    ing.submit(ev("T1"))
    assert (await asyncio.wait_for(t2, 1)).event.tx_id == "T1"


def test_sample_transactions_file():
    clock = ManualClock(T0)
    ing = StreamIngestor(clock=clock)
    lines = [ln for ln in SAMPLE.read_text(encoding="utf-8").splitlines() if ln.strip()]
    results = [ing.submit(ln) for ln in lines]
    by_status = {s: [r for r in results if r.status is s] for s in IngestStatus}
    assert len(by_status[IngestStatus.ACCEPTED]) == 13
    assert [r.tx_id for r in by_status[IngestStatus.DUPLICATE]] == ["T007"]
    assert len(by_status[IngestStatus.REJECTED]) == 1
    (rec,) = ing.rejected_log.records()
    assert '"tx_id":"T009"' in rec.raw and set(rec.fields) == {"amount", "currency"}
    c = ing.counters
    assert (c.accepted, c.duplicate, c.malformed) == (13, 1, 1)
    out = ing.drain()
    ids = [a.event.tx_id for a in out]
    assert len(ids) == 13 and len(set(ids)) == 13 and "T009" not in ids
    assert all(a.lateness.kind is LatenessKind.IN_ORDER for a in out)

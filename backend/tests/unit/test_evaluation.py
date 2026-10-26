"""Evaluation_Monitor unit tests (Req 4.20, 4.21, 14.12, 14.13, 20.14, 21.1)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from helix.cases.prioritiser import PeriodReport
from helix.evaluation import (
    NOT_APPLICABLE,
    EvaluationMonitor,
    InMemoryGroundTruthReader,
    StoreGroundTruthReader,
    ground_truth_channel,
)
from helix.immune.miss_detector import MissDetector
from helix.sim.ground_truth import GroundTruthRecord, GTSource, InMemoryGroundTruth
from helix.storage import Storage

T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
H = timedelta(hours=1)


def rec(eid, label="FRAUD", **kw):
    return GroundTruthRecord(event_id=eid, label=label, source=kw.pop("source", GTSource.LOADER), written_at=T0, **kw)


def period(p, admitted=0, backlog=0, active=0):
    return PeriodReport(period=p, start=T0 + H * p, end=T0 + H * (p + 1), admitted=admitted,
                        backlog_size=backlog, active_size=active)


def monitor(*recs):
    sink = InMemoryGroundTruth()
    for r in recs:
        sink.write(r)
    return sink, EvaluationMonitor(InMemoryGroundTruthReader(sink))


def test_period_report_not_applicable_without_fraud():
    _, m = monitor(rec("t1", "LEGIT"))
    m.on_event_ingested("t1", Decimal("10.00"), T0 + timedelta(minutes=1))
    r = m.on_period(period(0, admitted=2, backlog=3, active=2))
    assert (r.admitted, r.backlog_size, r.active_size) == (2, 3, 2)
    assert r.captured_fraud_fraction == NOT_APPLICABLE
    assert r.fraud_amount_ingested == 0


def test_captured_fraction_counts_fraud_amount_in_admitted_cases():
    _, m = monitor(rec("f1"), rec("f2"), rec("l1", "LEGIT"))
    m.on_event_ingested("f1", Decimal("30.00"), T0 + timedelta(minutes=1))
    m.on_event_ingested("f2", Decimal("70.00"), T0 + timedelta(minutes=2))
    m.on_event_ingested("l1", Decimal("500.00"), T0 + timedelta(minutes=3))
    m.on_case_admitted("C-1", ["f1", "l1"], T0 + timedelta(minutes=5))
    r = m.on_period(period(0, admitted=1))
    assert r.captured_fraud_fraction == pytest.approx(0.3)
    assert (r.fraud_amount_captured, r.fraud_amount_ingested) == (Decimal("30.00"), Decimal("100.00"))
    assert r.detected_events == 2
    assert m.detected_events() == {"f1", "l1"}


def test_period_uses_stream_time_cutoff():
    _, m = monitor(rec("f1"), rec("f2"))
    m.on_event_ingested("f1", Decimal("10"), T0 + timedelta(minutes=10))
    m.on_event_ingested("f2", Decimal("90"), T0 + H + timedelta(minutes=1))  # next period
    m.on_case_admitted("C-1", ["f2"], T0 + H + timedelta(minutes=2))
    r0 = m.on_period(period(0))
    assert r0.captured_fraud_fraction == 0.0 and r0.fraud_amount_ingested == Decimal("10")
    r1 = m.on_period(period(1, admitted=1))
    assert r1.captured_fraud_fraction == pytest.approx(0.9)
    assert [r.period for r in m.reports] == [0, 1]


def test_precision_recall_alerts_and_latency():
    _, m = monitor(rec("f1"), rec("f2"), rec("f3"))
    for i, tx in enumerate(["f1", "f2", "f3", "l1"]):
        m.on_event_ingested(tx, Decimal("1"), T0 + timedelta(minutes=i))
    m.on_case_admitted("C-1", ["f1", "l1"], T0 + timedelta(minutes=10))  # f1 latency 600 s
    m.on_case_admitted("C-1", ["f2"], T0 + timedelta(minutes=21))  # growth; f2 latency 1200 s
    m.on_case_admitted("C-2", ["f1"], T0 + timedelta(minutes=30))  # f1 keeps first detection
    m.on_event_ingested("x", Decimal("1"), T0 + 2 * H)
    mt = m.metrics()
    assert mt.cases_admitted == 2 and mt.detected_events == 3
    assert mt.precision == pytest.approx(2 / 3)
    assert mt.recall == pytest.approx(2 / 3)
    assert mt.alerts_per_hour == pytest.approx(1.0)
    assert mt.latency_median_s == pytest.approx(900.0)
    assert mt.ingested_events == 5


def test_metrics_not_applicable_when_empty():
    _, m = monitor()
    mt = m.metrics()
    assert mt.precision == mt.recall == mt.alerts_per_hour == mt.captured_fraud_fraction == NOT_APPLICABLE
    assert mt.latency_median_s is None and mt.lineages == {}


def test_labels_written_after_monitor_creation_are_picked_up():
    sink, m = monitor()
    sink.write(rec("f1"))
    m.on_event_ingested("f1", Decimal("5"), T0)
    m.on_case_admitted("C-1", ["f1"], T0 + timedelta(seconds=1))
    assert m.captured_fraud_fraction() == 1.0


def test_lineage_stats_follow_parent_chain():
    a = dict(source=GTSource.ATTACKER)
    _, m = monitor(
        rec("a1", campaign_id="k1", genome_id="g1", **a),
        rec("a2", campaign_id="k1", genome_id="g1", **a),
        rec("b1", campaign_id="k2", genome_id="g2", parent_genome_id="g1", **a),
        rec("c1", campaign_id="k3", genome_id="g9", **a),
    )
    for i, tx in enumerate(["a1", "a2", "b1", "c1"]):
        m.on_event_ingested(tx, Decimal("1"), T0 + timedelta(minutes=i))
    m.on_case_admitted("C-1", ["a2"], T0 + timedelta(minutes=4))  # k1 latency: 4 min from a1
    m.mark_campaign_completed("k1")
    lin = m.metrics().lineages
    assert set(lin) == {"g1", "g9"}
    g1 = lin["g1"]
    assert g1.genomes == ("g1", "g2") and g1.campaigns == 2 and g1.completed_campaigns == 1
    assert g1.detection_rate == 0.5 and g1.event_detection_rate == pytest.approx(1 / 3)
    assert g1.median_latency_s == 240.0
    assert lin["g9"].detection_rate == 0.0 and lin["g9"].median_latency_s is None


def test_dataset_summary_pattern_types_mapped_and_unmapped():
    _, m = monitor(
        rec("t1"), rec("t1", pattern_id="p1", pattern_type="CYCLE"),
        rec("t2"), rec("t2", pattern_id="p2", pattern_type="CYCLE"),
        rec("t3"), rec("t3", pattern_id="p3", pattern_type="STACK"),
        rec("t4", "LEGIT"),
    )
    for i, tx in enumerate(["t1", "t2", "t3", "t4"]):
        m.on_event_ingested(tx, Decimal("10"), T0 + timedelta(minutes=i))
    m.on_case_admitted("C-1", ["t1", "t4"], T0 + timedelta(minutes=9))
    m.on_period(period(0, admitted=1))
    s = m.dataset_summary()
    assert s.admissions_per_period == ((0, 1),)
    assert s.precision == 0.5 and s.recall == pytest.approx(1 / 3)
    assert s.captured_fraud_fraction == pytest.approx(1 / 3)
    cyc, stk = s.pattern_types["CYCLE"], s.pattern_types["STACK"]
    assert (cyc.mapped, cyc.motif, cyc.patterns, cyc.detection_rate) == (True, "ROUND_TRIP_CYCLE", 2, 0.5)
    assert (stk.mapped, stk.motif, stk.detection_rate) == (False, None, 0.0)
    assert s.mapped_types == ("CYCLE",) and s.unmapped_types == ("STACK",)


@pytest.mark.integration
def test_store_reader_and_channel(tmp_path):
    s = Storage(tmp_path / "h.duckdb").open()
    try:
        writer, reader = ground_truth_channel(s)
        assert isinstance(reader, StoreGroundTruthReader)
        writer.write(rec("f1", campaign_id="k1", genome_id="g1", source=GTSource.ATTACKER))
        writer.write(rec("l1", "LEGIT"))
        m = EvaluationMonitor(reader)
        m.on_event_ingested("f1", Decimal("12.50"), T0)
        m.on_event_ingested("l1", Decimal("99.00"), T0)
        m.on_case_admitted("C-1", ["f1"], T0 + timedelta(minutes=1))
        r = m.on_period(period(0, admitted=1))
        assert r.captured_fraud_fraction == 1.0
        assert m.metrics().lineages["g1"].detection_rate == 1.0
        assert reader.get("l1").label == "LEGIT"
    finally:
        s.close()


def test_in_memory_channel():
    writer, reader = ground_truth_channel(None)
    writer.write(rec("f1"))
    assert [v.event_id for v in reader.labels_since(None)] == ["f1"] and len(reader) == 1


def test_miss_detector_records_each_undetected_attack_event_once():
    sink = InMemoryGroundTruth()
    sink.write(rec("a1", campaign_id="k1", source=GTSource.ATTACKER))
    sink.write(rec("a2", campaign_id="k1", source=GTSource.ATTACKER))
    sink.write(rec("l1", "LEGIT"))
    detected = {"a2"}
    got: list[str] = []
    md = MissDetector(InMemoryGroundTruthReader(sink), detected.__contains__, got.append)
    for tx in ("a1", "a2", "l1"):
        md.observe(tx, T0)
    assert md.record_misses(T0 - timedelta(seconds=1)) == []  # deadline not reached
    misses = md.record_misses(T0 + timedelta(minutes=10))
    assert [(x.event_id, x.campaign_id) for x in misses] == [("a1", "k1")]
    assert md.record_misses(T0 + timedelta(minutes=20)) == [] and got == ["a1"]

"""SOC_Console (helix.soc.console, Req 36): SLA_Timers, workload, handoff notes, MTTD / MTTR."""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from helix.config import SeverityTier
from helix.soc.console import (
    ACK,
    BREACHED_LABEL,
    CLOSE,
    DEFAULT_SLA_TARGETS,
    HandoffNoteError,
    HandoffNoteStore,
    SlaPolicy,
    SocConsole,
    response_metrics,
    sla_rows,
    sla_timer,
    workload,
)
from helix.storage import Storage

T0 = datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)
FAST = settings(max_examples=60, deadline=None, derandomize=True, database=None)
TIERS = list(SeverityTier)


def case(n: int, tier: str = "HIGH", status: str = "OPEN", created: datetime = T0, opened=None, label_at=None,
         assignee=None, merged_into=None):
    return SimpleNamespace(case_id=f"C-{n}", seq_no=n, status=status, tier=SeverityTier(tier), assignee=assignee,
                           created_at=created, opened_at=opened, label_at=label_at, merged_into=merged_into,
                           updated_at=label_at or created)


def test_default_targets_match_the_requirement():
    m, h = 60.0, 3600.0
    assert {t.value: (v.ack_s, v.close_s) for t, v in DEFAULT_SLA_TARGETS.items()} == {
        "CRITICAL": (15 * m, 4 * h), "HIGH": (h, 24 * h), "MEDIUM": (4 * h, 72 * h), "LOW": (24 * h, 7 * 24 * h)}
    pol = SlaPolicy({"CRITICAL": {"ack_s": 60}, SeverityTier.LOW: (10, 20)})
    assert pol.target("CRITICAL").ack_s == 60 and pol.target("CRITICAL").close_s == 4 * h
    assert pol.target("LOW").close_s == 20 and pol.target("HIGH").ack_s == h
    with pytest.raises(ValueError, match="HIGH.ack_s"):
        SlaPolicy({"HIGH": {"ack_s": 0}})
    with pytest.raises(ValueError, match="unknown Severity_Tier"):
        SlaPolicy({"URGENT": (1, 2)})


def test_breach_label_names_the_breached_timer_and_uses_the_current_tier():
    c = case(1, "CRITICAL")
    t = sla_timer(c, T0 + timedelta(minutes=10))
    assert t.label is None and t.breached_timers == () and t.age_s == 600
    assert t.ack.running and t.ack.remaining_s == 300
    t = sla_timer(c, T0 + timedelta(minutes=16))
    assert t.label == BREACHED_LABEL and t.breached_timers == (ACK,)
    d = t.to_dict()
    assert d["label"] == "SLA BREACHED" and d["breached_timers"] == [ACK] and d["timers"][ACK]["breached"] is True
    # The Case was re-tiered to LOW: the LOW targets apply now (Req 36.2).
    c.tier = SeverityTier.LOW
    assert sla_timer(c, T0 + timedelta(minutes=16)).label is None
    # Both timers breached.
    c.tier = SeverityTier.CRITICAL
    assert sla_timer(c, T0 + timedelta(hours=5)).breached_timers == (ACK, CLOSE)


def test_acknowledge_timer_stops_when_opened():
    c = case(2, "CRITICAL", status="IN_REVIEW", opened=T0 + timedelta(minutes=5), assignee="ana")
    t = sla_timer(c, T0 + timedelta(hours=1))
    assert not t.ack.running and t.ack.elapsed_s == 300 and t.breached_timers == ()
    late = case(3, "CRITICAL", status="IN_REVIEW", opened=T0 + timedelta(minutes=20), assignee="ana")
    assert sla_timer(late, T0 + timedelta(minutes=30)).breached_timers == (ACK,)  # acknowledged late


def test_rows_cover_open_cases_only_breached_first_then_oldest():
    cs = [case(1, "LOW", created=T0), case(2, "CRITICAL", created=T0 + timedelta(hours=1)),
          case(3, "HIGH", status="CLOSED", label_at=T0 + timedelta(hours=2)),
          case(4, "LOW", created=T0 + timedelta(hours=2))]
    rows = sla_rows(cs, T0 + timedelta(hours=3))
    assert [r.case_id for r in rows] == ["C-2", "C-1", "C-4"]
    assert rows[0].breached and rows[0].age_s == 7200 and not rows[1].breached


def test_workload_counts_in_review_cases_per_investigator():
    cs = [case(1, status="IN_REVIEW", assignee="bo"), case(2, status="IN_REVIEW", assignee="ana"),
          case(3, status="IN_REVIEW", assignee="bo"), case(4, status="OPEN"),
          case(5, status="CLOSED", assignee="ana", label_at=T0)]
    assert workload(cs) == [{"investigator": "bo", "in_review": 2}, {"investigator": "ana", "in_review": 1}]


def test_mttd_and_mttr_over_the_window():
    now = T0 + timedelta(hours=30)
    first = {"C-1": T0 + timedelta(hours=10), "C-2": T0 + timedelta(hours=20), "C-3": T0}
    cs = [case(1, created=T0 + timedelta(hours=10, minutes=1)),  # MTTD 60 s
          case(2, status="CLOSED", created=T0 + timedelta(hours=20, minutes=3),
               label_at=T0 + timedelta(hours=21, minutes=3)),  # MTTD 180 s, MTTR 3600 s
          case(3, created=T0 + timedelta(minutes=1)),  # created outside the 24 h window
          case(4, status="CLOSED", created=T0 + timedelta(hours=10), merged_into="C-1",
               label_at=None)]  # merged: excluded
    mttd, mttr = response_metrics(cs, now, 24 * 3600, lambda c: first.get(c.case_id))
    assert (mttd.mean_s, mttd.cases) == (120.0, 2)
    assert (mttr.mean_s, mttr.cases) == (3600.0, 1)
    mttd, mttr = response_metrics(cs, now, 60, lambda c: first.get(c.case_id))
    assert mttd.mean_s is None and mttd.cases == 0 and mttr.cases == 0


def test_handoff_notes_reverse_chronological_and_validated():
    store = HandoffNoteStore()
    store.add("ana", "queue is clear", T0)
    store.add("bo", "watch C-4", T0 + timedelta(hours=8))
    store.add("cy", "backfilled", T0 + timedelta(hours=1))
    assert [(n.author, n.note_id) for n in store.list()] == [("bo", 2), ("cy", 3), ("ana", 1)]
    assert [n.author for n in store.list(1)] == ["bo"]
    for author, text, field in [("", "x", "author"), ("a" * 65, "x", "author"), ("ana", "", "text"),
                                ("ana", "x" * 2001, "text"), (None, "x", "author")]:
        with pytest.raises(HandoffNoteError) as e:
            store.add(author, text)
        assert e.value.field == field
    assert len(store) == 3


def test_handoff_notes_persist_in_duckdb(tmp_path):
    path = tmp_path / "helix.duckdb"
    s = Storage(path).open()
    try:
        store = HandoffNoteStore(s)
        store.add("ana", "first", T0)
        store.add("bo", "second", T0 + timedelta(minutes=1))
        again = HandoffNoteStore(s)
        assert [(n.author, n.text, n.ts) for n in again.list()] == [
            ("bo", "second", T0 + timedelta(minutes=1)), ("ana", "first", T0)]
        assert again.add("cy", "third", T0 + timedelta(minutes=2)).note_id == 3
        assert again.storage_errors == 0
    finally:
        s.close()
    s2 = Storage(path).open()  # the extra table does not break the schema check
    try:
        assert len(HandoffNoteStore(s2)) == 3
    finally:
        s2.close()


def test_console_snapshot_over_a_pipeline_like_object():
    c1 = case(1, "CRITICAL", created=T0)
    c2 = case(2, "LOW", status="IN_REVIEW", created=T0, opened=T0 + timedelta(minutes=1), assignee="ana")
    chain = SimpleNamespace(entries={("s", "T1"): SimpleNamespace(ts=T0 - timedelta(seconds=30))})
    helix = SimpleNamespace(cases=SimpleNamespace(cases={"C-1": c1, "C-2": c2}),
                            chain=lambda cid: chain if cid == "C-1" else None,
                            clock=SimpleNamespace(now=lambda: T0 + timedelta(minutes=20)))
    con = SocConsole(helix, time_source="stream", wall_clock=lambda: T0 + timedelta(days=8))
    con.notes.add("ana", "handover", T0)
    doc = con.snapshot()
    assert doc["time_source"] == "stream" and doc["open_cases"] == 2 and doc["breached_cases"] == 1
    assert doc["cases"][0]["case_id"] == "C-1" and doc["cases"][0]["label"] == "SLA BREACHED"
    assert doc["workload"] == [{"investigator": "ana", "in_review": 1}]
    assert doc["mttd"] == {"mean_s": 30.0, "cases": 1} and doc["mttr"] == {"mean_s": None, "cases": 0}
    assert doc["handoff_notes"][0]["author"] == "ana"
    wall = con.snapshot(time_source="wall")  # wall clock eight days later: LOW close target (7 d) breached too
    assert wall["breached_cases"] == 2 and wall["cases"][0]["age_s"] == 8 * 86_400
    assert {r["case_id"]: r["breached_timers"] for r in wall["cases"]} == {"C-1": [ACK, CLOSE], "C-2": [CLOSE]}
    with pytest.raises(ValueError):
        SocConsole(helix, time_source="sundial")


# --- properties --------------------------------------------------------------------------------


@FAST
@given(tier=st.sampled_from(TIERS), age=st.floats(0, 10 * 86_400, allow_nan=False),
       opened_after=st.none() | st.floats(0, 10 * 86_400, allow_nan=False))
def test_breach_label_iff_a_timer_exceeds_its_target(tier, age, opened_after):
    """For all Cases and times, the SLA_Timer shows "SLA BREACHED" exactly when elapsed time from
    creation exceeds the current tier's target, naming exactly the breached timers.

    **Validates: Requirements 36.1, 36.2, 36.3, 36.6**
    """
    now = T0 + timedelta(seconds=age)
    opened = None if opened_after is None or opened_after > age else T0 + timedelta(seconds=opened_after)
    c = case(1, tier.value, status="OPEN" if opened is None else "IN_REVIEW", opened=opened, assignee="x")
    t = sla_timer(c, now)
    tgt = DEFAULT_SLA_TARGETS[tier]
    ack_elapsed = (now - T0).total_seconds() if opened is None else (opened - T0).total_seconds()
    expected = tuple(n for n, e, lim in ((ACK, ack_elapsed, tgt.ack_s),
                                         (CLOSE, (now - T0).total_seconds(), tgt.close_s)) if e > lim)
    assert t.breached_timers == expected
    assert (t.label == BREACHED_LABEL) is bool(expected)
    assert t.age_s == pytest.approx((now - T0).total_seconds())


@FAST
@given(offsets=st.lists(st.integers(0, 10_000), min_size=1, max_size=25))
def test_notes_are_listed_newest_first_and_none_is_lost(offsets):
    """For all submission sequences, stored notes keep author and timestamp and list in reverse
    chronological order.

    **Validates: Requirements 36.5**
    """
    store = HandoffNoteStore()
    for i, o in enumerate(offsets):
        store.add(f"inv{i}", f"note {i}", T0 + timedelta(seconds=o))
    out = store.list()
    assert sorted(n.note_id for n in out) == list(range(1, len(offsets) + 1))
    keys = [(n.ts, n.note_id) for n in out]
    assert keys == sorted(keys, reverse=True)
    assert all(n.author == f"inv{n.note_id - 1}" and n.ts == T0 + timedelta(seconds=offsets[n.note_id - 1])
               for n in out)


@FAST
@given(rows=st.lists(st.tuples(st.integers(0, 200_000), st.integers(0, 5_000), st.none() | st.integers(0, 90_000)),
                     max_size=20),
       window=st.integers(60, 200_000))
def test_mttd_mttr_are_the_means_over_the_window(rows, window):
    """For all Case sets, MTTD / MTTR equal the arithmetic means of the in-window detection and
    response times.

    **Validates: Requirements 36.7**
    """
    now = T0 + timedelta(seconds=250_000)
    cs, first = [], {}
    for i, (created_s, detect_s, close_after) in enumerate(rows):
        created = T0 + timedelta(seconds=created_s)
        closed = None if close_after is None else created + timedelta(seconds=close_after)
        if closed is not None and closed > now:
            closed = None
        cs.append(case(i, status="OPEN" if closed is None else "CLOSED", created=created, label_at=closed))
        first[f"C-{i}"] = created - timedelta(seconds=detect_s)
    lo = now - timedelta(seconds=window)
    exp_d = [d for (c_s, d, _), c in zip(rows, cs) if lo < c.created_at <= now]
    exp_r = [(c.label_at - c.created_at).total_seconds() for c in cs if c.label_at is not None and lo < c.label_at <= now]
    mttd, mttr = response_metrics(cs, now, window, lambda c: first[c.case_id])
    assert mttd.cases == len(exp_d) and mttr.cases == len(exp_r)
    assert mttd.mean_s == (pytest.approx(statistics.fmean(exp_d)) if exp_d else None)
    assert mttr.mean_s == (pytest.approx(statistics.fmean(exp_r)) if exp_r else None)

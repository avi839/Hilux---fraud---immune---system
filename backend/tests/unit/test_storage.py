"""Unit tests for helix.storage (design §20, Req 24)."""
from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import duckdb
import pytest

from helix.changepoint import ChangePointDetector
from helix.codec import TransactionEvent, format as format_event
from helix.config.schema import StorageConfig
from helix.fusion import Contribution, ContributorType, Mass, fuse
from helix.ingest import RejectedLog, RejectedRecord
from helix.memory import ProfileStore
from helix.storage import (
    DuckDBRejectedLog,
    SchemaMismatchError,
    Storage,
    StorageOpenError,
    decode_json,
    encode_json,
    profile_store_from_dict,
    profile_store_to_dict,
)
from helix.storage.ground_truth import GroundTruthStore

pytestmark = pytest.mark.integration

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
PLUS2 = timezone(timedelta(hours=2))


def ev(i: int, minutes: float = 0, src: str = "A", dst: str = "B", amount: str = "10.00", **kw) -> TransactionEvent:
    return TransactionEvent(
        tx_id=f"tx{i}", ts=T0 + timedelta(minutes=minutes), src=src, dst=dst,
        amount=Decimal(amount), currency="USD", **kw,
    )  # fmt: skip


def digest(p) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def tables(path) -> set[str]:
    con = duckdb.connect(str(path), read_only=True)
    try:
        return {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    finally:
        con.close()


@pytest.fixture
def db(tmp_path):
    s = Storage(tmp_path / "helix.duckdb").open()
    yield s
    if s.is_open:
        s.close()


# ---- open / schema ---------------------------------------------------------------------
def test_missing_file_creates_empty_store(tmp_path):
    p = tmp_path / "sub" / "h.duckdb"
    s = Storage(p).open()
    assert s.created and p.exists()
    r = s.restore()
    assert r.cases == [] and r.snapshot is None and r.latest_event_ts is None and r.last_seq_no is None
    s.close()
    assert "schema_version" in tables(p)
    s2 = Storage(p).open()
    assert not s2.created
    s2.close()


def test_unreadable_file_refused_and_untouched(tmp_path):
    p = tmp_path / "h.duckdb"
    p.write_bytes(b"this is not a duckdb file" * 100)
    before = digest(p)
    with pytest.raises(StorageOpenError):
        Storage(p).open()
    assert digest(p) == before


def test_version_mismatch_refused_and_untouched(tmp_path):
    p = tmp_path / "h.duckdb"
    Storage(p).open().close()
    con = duckdb.connect(str(p))
    con.execute("UPDATE schema_version SET version = 99")
    con.close()
    before, mtime = digest(p), p.stat().st_mtime_ns
    with pytest.raises(SchemaMismatchError, match="version"):
        Storage(p).open()
    assert digest(p) == before and p.stat().st_mtime_ns == mtime


def test_missing_version_table_and_column_drift_refused(tmp_path):
    p = tmp_path / "other.duckdb"
    con = duckdb.connect(str(p))
    con.execute("CREATE TABLE x (a INTEGER)")
    con.close()
    before = digest(p)
    with pytest.raises(SchemaMismatchError, match="schema_version"):
        Storage(p).open()
    assert digest(p) == before

    q = tmp_path / "drift.duckdb"
    Storage(q).open().close()
    con = duckdb.connect(str(q))
    con.execute("ALTER TABLE cases ADD COLUMN extra INTEGER")
    con.close()
    before = digest(q)
    with pytest.raises(SchemaMismatchError, match="cases"):
        Storage(q).open()
    assert digest(q) == before


def test_snapshot_interval_range_and_from_config(tmp_path):
    for bad in (4.9, 3_601):
        with pytest.raises(ValueError, match="storage.snapshot_interval_s"):
            Storage(tmp_path / "x.duckdb", snapshot_interval_s=bad)
    s = Storage.from_config(StorageConfig(path=str(tmp_path / "c.duckdb"), snapshot_interval_s=5))
    assert s.snapshot_interval_s == 5 and s.path.name == "c.duckdb"


# ---- rows ------------------------------------------------------------------------------
def test_events_and_assessments_round_trip(db):
    e1 = ev(1, 0, lat=51.5, lon=-0.12, channel="web")
    e2 = TransactionEvent(tx_id="tx2", ts=datetime(2026, 1, 1, 3, tzinfo=PLUS2), src="B", dst="C",
                          amount=Decimal("999999999.99"), currency="EUR", mcc="5411")
    db.record_event(e1, seq_no=1, received_at=T0)
    db.record_event(e2, seq_no=2, late=True, lateness_s=7200.0)
    cs = [Contribution("amount", ContributorType.SENSOR, Mass(0.3, 0.1, 0.6), 0.9),
          Contribution("geo", ContributorType.SENSOR, Mass(0.0, 0.5, 0.5), 1.0)]
    fa, _ = fuse(cs, 0.8, tx_id="tx1")
    db.record_assessment(fa, risk=0.42)
    assert db.pending == 3 and db.flush() and db.pending == 0

    got = db.events()  # ordered by ts: e1 at 00:00Z, e2 at 03:00+02:00 = 01:00Z
    assert [g.event for g in got] == [e1, e2]
    # the original UTC offset survives, so formatting is identical
    assert [format_event(g.event) for g in got] == [format_event(e1), format_event(e2)]
    assert (got[1].seq_no, got[1].late, got[1].lateness_s) == (2, True, 7200.0)
    assert got[0].received_at == T0
    (sa,) = db.assessments("tx1")
    assert sa.assessment == fa and sa.risk == 0.42


def test_upsert_append_and_auto_keys(tmp_path):
    p = tmp_path / "h.duckdb"
    s = Storage(p).open()
    s.upsert("cases", {"case_id": "c1", "status": "OPEN", "updated_at": T0, "risk": 0.7})
    s.upsert("cases", {"case_id": "c1", "status": "IN_REVIEW", "updated_at": T0, "risk": 0.8})
    s.append("case_history", {"case_id": "c1", "entry_no": 1, "ts": T0, "action": "CREATE"})
    s.append("case_history", {"case_id": "c1", "entry_no": 1, "ts": T0, "action": "TAMPER"})
    k1 = s.append("weight_history", {"contributor_id": "amount", "old": 1.0, "new": 0.9, "ts": T0})
    assert s.flush()
    s.close()
    s = Storage(p).open()
    k2 = s.append("weight_history", {"contributor_id": "amount", "old": 0.9, "new": 0.8, "ts": T0})
    assert (k1, k2) == (1, 2)
    s.flush()
    r = s.restore()
    assert r.cases == [{"case_id": "c1", "status": "IN_REVIEW", "updated_at": T0, "risk": 0.8}]
    assert [h["action"] for h in r.case_history] == ["CREATE"]  # insert-only: first kept
    assert [w["new"] for w in r.weight_history] == [0.9, 0.8]
    s.close()


def test_enqueue_validation(db):
    with pytest.raises(ValueError, match="case_id"):
        db.upsert("cases", {"status": "OPEN"})
    with pytest.raises(ValueError, match="aware"):
        db.upsert("cases", {"case_id": "c", "updated_at": datetime(2026, 1, 1)})
    with pytest.raises(ValueError, match="bool"):
        db.upsert("antibodies", {"ab_id": "a", "active": "yes"})
    with pytest.raises(ValueError):
        db.append("cases", {"case_id": "c"})  # cases is an upsert table
    with pytest.raises(ValueError):
        db.enqueue("ground_truth", {"event_id": "x"})  # not reachable via Storage
    with pytest.raises(TypeError):
        db.upsert("cases", {"case_id": "c", "obj": object()})
    assert db.pending == 0


# ---- degraded mode ---------------------------------------------------------------------
def test_degraded_after_three_failures_then_recovers(db):
    db.upsert("cases", {"case_id": "c1", "status": "OPEN"})
    with db._locked() as con:
        con.execute("ALTER TABLE cases RENAME TO cases_away")
    assert not db.flush() and not db.flush()
    assert not db.degraded and db.status().consecutive_failures == 2
    assert not db.flush()
    st = db.status()
    assert st.degraded and st.buffered == 1 and st.last_error
    db.upsert("cases", {"case_id": "c2", "status": "OPEN"})  # keeps processing
    with db._locked() as con:
        con.execute("ALTER TABLE cases_away RENAME TO cases")
    assert db.flush()
    st = db.status()
    assert not st.degraded and st.buffered == 0 and st.consecutive_failures == 0
    assert [c["case_id"] for c in db.load_rows("cases")] == ["c1", "c2"]


def test_buffer_cap_drops_oldest(tmp_path):
    s = Storage(tmp_path / "h.duckdb", batch_rows=2, buffer_cap=3).open()
    for i in range(5):
        s.upsert("antibodies", {"ab_id": f"a{i}", "active": True})
    assert s.status().buffered == 3 and s.status().dropped == 2
    assert s.flush()
    assert [a["ab_id"] for a in s.load_rows("antibodies")] == ["a2", "a3", "a4"]
    s.close()


# ---- async writer, snapshots, shutdown -------------------------------------------------
async def _until(pred, timeout=3.0):
    t = asyncio.get_running_loop().time() + timeout
    while not pred():
        assert asyncio.get_running_loop().time() < t, "timed out"
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_writer_flushes_on_interval_and_on_batch_size(tmp_path):
    s = Storage(tmp_path / "a.duckdb", flush_interval_s=0.05).open()
    task = asyncio.create_task(s.writer())
    s.upsert("cases", {"case_id": "c1"})
    await _until(lambda: s.pending == 0)
    assert s.load_rows("cases") == [{"case_id": "c1"}]
    await s.shutdown(tasks=[task])

    s = Storage(tmp_path / "b.duckdb", flush_interval_s=60, batch_rows=10).open()
    task = asyncio.create_task(s.writer())
    await asyncio.sleep(0)
    for i in range(9):
        s.upsert("antibodies", {"ab_id": f"a{i}", "active": True})
    await asyncio.sleep(0.1)
    assert s.pending == 9  # below batch size, interval far away
    s.upsert("antibodies", {"ab_id": "a9", "active": True})
    await _until(lambda: s.pending == 0)
    await s.shutdown(tasks=[task])


@pytest.mark.asyncio
async def test_periodic_and_final_snapshot_on_shutdown(tmp_path):
    p = tmp_path / "h.duckdb"
    s = Storage(p, flush_interval_s=0.05).open()
    ps, cpd = ProfileStore(), ChangePointDetector()
    clock = {"t": T0}

    def source():
        return profile_store_to_dict(ps), cpd.to_dict(), clock["t"]

    tasks = [asyncio.create_task(s.writer()), asyncio.create_task(s.snapshotter(source, interval_s=0.05))]
    await _until(lambda: s.latest_snapshot() is not None)
    for i in range(5):
        e = ev(i, i * 7, amount=f"{10 + i}.00")
        ps.apply(e)
        cpd.observe(e)
        s.record_event(e, seq_no=i)
    clock["t"] = T0 + timedelta(hours=1)
    s.upsert("cases", {"case_id": "late-write"})  # pending at shutdown
    assert await s.shutdown(source, tasks)
    assert not s.is_open

    s = Storage(p).open()
    r = s.restore()
    assert r.snapshot is not None and r.snapshot.ts == clock["t"]
    assert r.snapshot.dna == profile_store_to_dict(ps)
    assert r.snapshot.cpd == cpd.to_dict()
    assert r.cases == [{"case_id": "late-write"}] and r.last_seq_no == 4
    assert s.latest_snapshot(at_or_before=T0).ts == T0  # an earlier periodic snapshot
    s.close()


# ---- rejected log / ground truth -------------------------------------------------------
def test_duckdb_rejected_log(db):
    rl = DuckDBRejectedLog(db)
    assert isinstance(rl, RejectedLog)
    recs = [RejectedRecord("{bad", T0, ("$",), "json_invalid"), RejectedRecord('{"a":1}', T0, ("amount", "ts"))]
    for r in recs:
        rl.append(r)
    db.flush()
    assert rl.records() == recs and len(rl) == 2


def test_ground_truth_store_is_separate(tmp_path):
    p = tmp_path / "h.duckdb"
    s = Storage(p).open()
    s.close()
    assert "ground_truth" not in tables(p)  # Storage itself never creates it
    s = Storage(p).open()
    gt = GroundTruthStore(s)
    gt.write({"event_id": "e1", "label": "FRAUD", "source": "ATTACKER", "campaign_id": "k1", "written_at": T0})
    gt.write_many([{"event_id": "e2", "label": "LEGIT", "source": "LOADER", "written_at": T0 + timedelta(hours=1)}])
    with pytest.raises(ValueError):
        gt.write({"event_id": "e3"})
    assert gt.get("e1")["campaign_id"] == "k1" and gt.get("nope") is None
    assert [r["event_id"] for r in gt.labels_since(T0 + timedelta(minutes=1))] == ["e2"]
    assert len(gt) == 2
    s.close()
    s = Storage(p).open()  # a file with the ground-truth table still passes the schema check
    assert [r["event_id"] for r in GroundTruthStore(s).all()] == ["e1", "e2"]
    s.close()


# ---- serde / graph ---------------------------------------------------------------------
def test_tagged_json_round_trip():
    v = {"a": (1, 2.5, None), "s": {3, 1}, "d": Decimal("1.10"), "t": T0, "n": datetime(2026, 1, 1),
         "td": timedelta(seconds=1.5), "inf": float("-inf"), "esc": {"$dt": "not a date"}, "l": [True, "x"]}
    assert decode_json(encode_json(v)) == v
    assert "Infinity" not in encode_json(v)


def test_profile_store_serde_preserves_withholding():
    ps = ProfileStore(half_life_days=7.0)
    events = [ev(i, i * 30, dst=f"P{i % 3}", amount=f"{5 + i}.25", channel="pos" if i % 2 else None,
                 lat=10.0 + i, lon=20.0) for i in range(8)]
    for e in events:
        ps.apply(e, withhold=e.tx_id == "tx3")
    ps.withhold("A", ["tx5", "tx99"])
    ps.exclude("A", ["tx6"])
    d = profile_store_to_dict(ps)
    ps2 = profile_store_from_dict(decode_json(encode_json(d)))
    assert profile_store_to_dict(ps2) == d
    assert ps2.view("A") == ps.view("A")
    assert ps.release("A", ["tx3", "tx5"]) and ps2.release("A", ["tx3", "tx5"])
    assert ps2.view("A") == ps.view("A") and ps2.withheld("A") == ps.withheld("A")


def test_rebuild_graph_empty_and_window(db):
    assert db.rebuild_graph(3_600).num_edges == 0
    for i, m in enumerate([0, 30, 90, 200]):
        db.record_event(ev(i, m, src="A", dst=f"N{i}"), seq_no=i)
    db.flush()
    g = db.rebuild_graph(3_600)  # window 60 min ending at minute 200
    assert [e.tx_id for e in g.edges()] == ["tx3"]
    g = db.rebuild_graph(3_600, latest_ts=T0 + timedelta(minutes=90))
    assert [e.tx_id for e in g.edges()] == ["tx1", "tx2"]

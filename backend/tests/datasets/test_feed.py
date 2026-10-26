"""Dataset_Feeder, CLI and GET /dataset (design §24): Properties 77, 78, 79 plus unit tests."""

from __future__ import annotations

import asyncio
import csv
import tempfile
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st

from helix.api.__main__ import build_state, parse_args
from helix.api.app import create_app
from helix.api.state import AppState
from helix.codec import TransactionEvent
from helix.config import HelixConfig
from helix.core import EVENTS
from helix.datasets import HEADER, RowError, parse_row
from helix.datasets.csv_adapter import MappedRowParser
from helix.datasets.feed import DatasetFeeder, DatasetSpec, DatasetStartupError, FeedStatus
from helix.datasets.mapping import LabelSpec, dump_mapping, load_mapping
from helix.datasets.patterns import MOTIF_FOR_PATTERN
from helix.evaluation import EvaluationMonitor, ground_truth_channel
from helix.motifs.models import MotifType
from helix.sim import InMemoryGroundTruth, ManualClock
from helix.sim.ground_truth import GroundTruthRecord, GTSource
from tests.property.test_datasets_properties import valid_rows

UTC = timezone.utc
REPO = Path(__file__).resolve().parents[3]
FIX = Path(__file__).resolve().parents[1] / "fixtures"
AML_TRANS = FIX / "aml" / "HI-Tiny_Trans.csv"
AML_PATTERNS = FIX / "aml" / "HI-Tiny_Patterns.txt"
GENERIC_CSV = FIX / "onboarding" / "generic_bank.csv"
GENERIC_MAP = REPO / "mappings" / "generic_bank.yaml"
GENERIC_HEADER = ["TxnRef", "PostedAt", "DebitAccount", "CreditAccount", "Amount", "Currency", "Channel", "MCC",
                  "DeviceId", "Lat", "Lon", "IsFraud"]
T0 = datetime(2022, 9, 1, tzinfo=UTC)


def _aml_ts(dt: datetime) -> str:
    return f"{dt.year:04d}/{dt.month:02d}/{dt.day:02d} {dt.hour:02d}:{dt.minute:02d}"


def _write(path: Path, header: list[str], rows: list[list[str]]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def _fake_state(ingest, gt=None) -> SimpleNamespace:
    return SimpleNamespace(cfg=HelixConfig(), ingestor=ingest, clock=ManualClock(datetime(2000, 1, 1, tzinfo=UTC)),
                           gt_writer=gt if gt is not None else InMemoryGroundTruth(), helix=None)


class ScheduledIngest:
    """Fake Stream_Ingestor: after the k-th submit, backpressure holds for schedule[k] polls."""

    def __init__(self, schedule: list[int], log: list | None = None) -> None:
        self.schedule, self.hold, self.k = schedule, 0, 0
        self.events: list[TransactionEvent] = []
        self.violations = 0
        self.log = log

    @property
    def backpressure(self) -> bool:
        return self.hold > 0

    def submit(self, ev: TransactionEvent) -> None:
        if self.hold > 0:
            self.violations += 1
        self.events.append(ev)
        if self.log is not None:
            self.log.append(("submit", ev.tx_id))
        self.hold = self.schedule[self.k] if self.k < len(self.schedule) else 0
        self.k += 1

    def __len__(self) -> int:
        return 0


def _feeder(spec: DatasetSpec, state) -> DatasetFeeder:
    ing = state.ingestor

    async def sleep(d: float) -> None:
        if d > 0 and getattr(ing, "hold", 0) > 0:
            ing.hold -= 1

    f = DatasetFeeder(spec, state, sleep=sleep, load_in_thread=False)
    f.precheck()
    return f


# ------------------------------------------------------------------ Property 77


@st.composite
def aml_files(draw):
    n = draw(st.integers(0, 30))
    rows = []
    for _ in range(n):
        f = list(draw(valid_rows()))
        f[0] = _aml_ts(T0 + timedelta(minutes=draw(st.integers(0, 15))))  # ties and descents
        bad = draw(st.sampled_from([None] * 6 + ["ts", "amount", "short"]))
        if bad == "ts":
            f[0] = "not-a-date"
        elif bad == "amount":
            f[7] = "x"
        elif bad == "short":
            f = f[:5]
        rows.append(f)
    return rows


# Feature: helix-fraud-immune-system, Property 77: Dataset stream order equals timestamp order, and nothing is dropped under backpressure
@pytest.mark.property
@settings(max_examples=60)
@given(rows=aml_files(), max_rows=st.one_of(st.none(), st.integers(1, 35)),
       schedule=st.lists(st.integers(0, 4), max_size=40))
def test_stream_order_and_no_drop_under_backpressure(rows, max_rows, schedule):
    """**Validates: Requirements 39.2, 39.3**"""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "HI-Prop_Trans.csv"
        _write(path, list(HEADER), rows)
        ing = ScheduledIngest(schedule)
        feeder = _feeder(DatasetSpec("amlworld", path, max_rows=max_rows), _fake_state(ing))
        asyncio.run(feeder.run())
    assert feeder.status is FeedStatus.DONE, feeder.error
    ok = []
    for i, f in enumerate(rows, start=1):
        res = parse_row(f, i, path.stem, UTC)
        if not isinstance(res, RowError):
            ok.append((res[0].ts, i, res[0].tx_id))
    ok.sort(key=lambda t: (t[0], t[1]))  # stable: ties in CSV row order
    want = [tx for _, _, tx in ok][: max_rows if max_rows is not None else None]
    got = [e.tx_id for e in ing.events]
    assert got == want and len(set(got)) == len(got)
    assert ing.violations == 0
    p = feeder.progress()
    assert p.submitted == p.in_scope == len(want) and p.rows_read + 0 >= len(want)


# ------------------------------------------------------------------ Property 78

FRAUD_V, LEGIT_V = "LBL-FRAUD-Y", "LBL-LEGIT-N"
ids = st.from_regex(r"\A[A-Za-z0-9]{1,12}\Z")


class LoggingGT:
    def __init__(self, log: list) -> None:
        self.records: list[GroundTruthRecord] = []
        self.log = log

    def write(self, rec: GroundTruthRecord) -> None:
        self.records.append(rec)
        self.log.append(("label", rec.event_id))


# Feature: helix-fraud-immune-system, Property 78: Streamed labels reach only the Ground_Truth_Store
@pytest.mark.property
@settings(max_examples=50)
@given(data=st.data(), n=st.integers(1, 20))
def test_streamed_labels_reach_only_ground_truth(data, n):
    """**Validates: Requirements 39.4, 40.4**"""
    base = load_mapping(GENERIC_MAP)
    m = base.model_copy(update={"label": LabelSpec(source=base.label.source, fraud_values=(FRAUD_V,),
                                                   legit_values=(LEGIT_V,))})
    p = MappedRowParser(m, GENERIC_HEADER, "bank")
    t = datetime(2024, 1, 10, 9, 0, tzinfo=UTC)
    rows, labels = [], {}
    for i in range(n):
        t += timedelta(seconds=data.draw(st.integers(-600, 600)))  # unordered: the loader sorts
        ev = TransactionEvent(tx_id=f"T{i}", ts=t, src=data.draw(ids), dst=data.draw(ids), amount=Decimal("10.00"),
                              currency="GBP", channel=data.draw(st.one_of(st.none(), ids)),
                              device_id=data.draw(st.one_of(st.none(), ids)))
        lab = data.draw(st.sampled_from([True, False, None]))
        labels[ev.tx_id] = lab
        rows.append(p.format(ev, lab))
    log: list = []
    with tempfile.TemporaryDirectory() as d:
        mp, tp = Path(d) / "m.yaml", Path(d) / "bank.csv"
        mp.write_text(dump_mapping(m, "yaml"), encoding="utf-8")
        _write(tp, GENERIC_HEADER, rows)
        ing = ScheduledIngest([], log)
        gt = LoggingGT(log)
        feeder = _feeder(DatasetSpec("csv", tp, mapping=mp), _fake_state(ing, gt))
        asyncio.run(feeder.run())
    assert feeder.status is FeedStatus.DONE, feeder.error
    kinds = [k for k, _ in log]
    if "submit" in kinds and "label" in kinds:
        assert max(i for i, k in enumerate(kinds) if k == "label") < kinds.index("submit")
    assert {r.event_id: r.label for r in gt.records} == {
        tx: ("FRAUD" if v else "LEGIT") for tx, v in labels.items() if v is not None}
    assert all(r.source is GTSource.LOADER for r in gt.records)
    assert len(ing.events) == n
    for ev in ing.events:
        doc = ev.model_dump()
        assert set(doc) == set(TransactionEvent.model_fields)
        assert not {FRAUD_V, LEGIT_V} & {str(v) for v in doc.values()}


# ------------------------------------------------------------------ Property 79

PTYPES = ["CYCLE", "FAN-OUT", "FAN-IN", "STACK", "RANDOM", "BIPARTITE", "GATHER-SCATTER", "SCATTER-GATHER"]
MOTIFS = [m.value for m in MotifType]


@st.composite
def eval_worlds(draw):
    txs = [f"t{i}" for i in range(draw(st.integers(1, 30)))]
    fraud = draw(st.sets(st.sampled_from(txs)))
    fl = sorted(fraud)
    patterns = []
    if fl:
        for k in range(draw(st.integers(0, 6))):
            members = draw(st.sets(st.sampled_from(fl), min_size=1))
            patterns.append((f"p{k}", draw(st.sampled_from(PTYPES)), members))
    cases = []
    for c in range(draw(st.integers(0, 6))):
        cases.append((f"C-{c}", draw(st.sets(st.sampled_from(txs), min_size=1)),
                      draw(st.sets(st.sampled_from(MOTIFS)))))
    return txs, fraud, patterns, cases


def _r(num: int, den: int):
    return "not applicable" if den == 0 else num / den


# Feature: helix-fraud-immune-system, Property 79: Per-pattern metrics match the oracle
@pytest.mark.property
@given(world=eval_worlds())
def test_per_pattern_metrics_match_the_oracle(world):
    """**Validates: Requirements 39.6**"""
    txs, fraud, patterns, cases = world
    writer, reader = ground_truth_channel(None)
    now = T0
    for tx in txs:
        writer.write(GroundTruthRecord(event_id=tx, label="FRAUD" if tx in fraud else "LEGIT",
                                       source=GTSource.LOADER, written_at=now))
    for pid, ptype, members in patterns:
        for tx in sorted(members):
            writer.write(GroundTruthRecord(event_id=tx, label="FRAUD", source=GTSource.LOADER, written_at=now,
                                           pattern_id=pid, pattern_type=ptype))
    mon = EvaluationMonitor(reader)
    for i, tx in enumerate(txs):
        mon.on_event_ingested(tx, Decimal("1"), T0 + timedelta(seconds=i))
    for cid, ctx, kinds in cases:
        mon.on_case_admitted(cid, sorted(ctx), T0 + timedelta(hours=1), motifs=sorted(kinds))
    stats = mon.dataset_summary().pattern_types
    detected = set().union(*(c[1] for c in cases)) if cases else set()
    by_type: dict[str, list] = {}
    for pid, ptype, members in patterns:
        by_type.setdefault(ptype, []).append(members)
    assert set(stats) == set(by_type)
    for ptype, groups in by_type.items():
        s = stats[ptype]
        mem = set().union(*groups)
        assert s.patterns == len(groups)
        assert s.detected_patterns == sum(1 for g in groups if g & detected)
        assert s.recall == _r(len(mem & detected), len(mem))
        motif = MOTIF_FOR_PATTERN.get(ptype)
        if motif is None:
            assert s.precision is None and not s.mapped
        else:
            in_cases = set().union(*(c[1] for c in cases if motif.value in c[2])) if cases else set()
            assert s.precision == _r(len(in_cases & fraud), len(in_cases))


# ------------------------------------------------------------------ CLI unit tests


def test_dataset_and_generator_are_mutually_exclusive(capsys):
    with pytest.raises(SystemExit) as ei:
        parse_args(["--dataset", "amlworld", "--trans", str(AML_TRANS), "--generator"])
    assert ei.value.code == 2 and "--dataset and --generator are mutually exclusive" in capsys.readouterr().err


@pytest.mark.parametrize("argv,msg", [
    (["--dataset", "csv", "--trans", "x.csv"], "--dataset csv needs --mapping"),
    (["--dataset", "csv", "--trans", "x.csv", "--mapping", "m.yaml", "--patterns", "p.txt"], "--patterns"),
    (["--dataset", "amlworld"], "--dataset needs --trans"),
    (["--trans", "x.csv"], "need --dataset"),
    (["--dataset", "amlworld", "--trans", "x", "--speed", "0.5"], "--speed"),
    (["--dataset", "amlworld", "--trans", "x", "--warmup-until", "2024-01-01"], "--warmup-until needs --warmup"),
])
def test_inconsistent_dataset_options_are_argparse_errors(argv, msg, capsys):
    with pytest.raises(SystemExit) as ei:
        parse_args(argv)
    assert ei.value.code == 2 and msg in capsys.readouterr().err


def test_cli_parses_dataset_options():
    a = parse_args(["--dataset", "csv", "--trans", "t.csv", "--mapping", "m.yaml", "--speed", "60", "--max-rows", "0",
                    "--warmup", "--warmup-until", "2024-03-01T08:30:00Z"])
    assert (a.dataset, a.speed, a.max_rows, a.warmup) == ("csv", 60.0, 0, True)
    assert a.warmup_until == datetime(2024, 3, 1, 8, 30, tzinfo=UTC)
    assert parse_args(["--dataset", "amlworld", "--trans", "t"]).max_rows == 20_000


@pytest.mark.parametrize("make,reason", [
    (lambda d: ["--dataset", "amlworld", "--trans", str(d / "missing.csv")], "transactions file not found"),
    (lambda d: ["--dataset", "amlworld", "--trans", str(AML_TRANS), "--patterns", str(d / "no.txt")],
     "patterns file not found"),
    (lambda d: ["--dataset", "csv", "--trans", str(AML_TRANS), "--mapping", str(GENERIC_MAP)], "invalid header"),
    (lambda d: ["--dataset", "amlworld", "--trans", str(GENERIC_CSV)], "invalid header"),
    (lambda d: ["--dataset", "csv", "--trans", str(GENERIC_CSV), "--mapping", str(d / "bad.yaml")],
     "invalid Column_Mapping"),
])
def test_bad_dataset_files_refuse_to_start(tmp_path, capsys, make, reason):
    (tmp_path / "bad.yaml").write_text("name: x\n", encoding="utf-8")
    with pytest.raises(SystemExit) as ei:
        build_state(parse_args(make(tmp_path)))
    err = capsys.readouterr().err
    assert ei.value.code == 1 and err.startswith("dataset: ") and reason in err


def test_precheck_error_names_path_and_reason(tmp_path):
    f = DatasetFeeder(DatasetSpec("amlworld", tmp_path / "nope.csv"), None)
    with pytest.raises(DatasetStartupError) as ei:
        f.precheck()
    assert ei.value.path == tmp_path / "nope.csv" and "not found" in ei.value.reason


# ------------------------------------------------------------------ API unit tests


def test_get_dataset_is_disabled_without_dataset_mode():
    with TestClient(create_app(AppState.build(), manage_lifecycle=False)) as c:
        doc = c.get("/api/v1/dataset").json()
    assert doc["status"] == "DISABLED"
    assert all(doc[k] is None for k in ("kind", "files", "rows_read", "skipped", "skip_reasons", "in_scope",
                                        "submitted", "progress", "warmup", "evaluation"))


def _wait_done(c: TestClient) -> dict:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        doc = c.get("/api/v1/dataset").json()
        if doc["status"] in ("DONE", "FAILED"):
            return doc
        time.sleep(0.02)
    raise AssertionError(f"dataset feeder did not finish: {doc}")


def test_amlworld_fixture_streams_through_the_live_api():
    st_ = build_state(parse_args(["--dataset", "amlworld", "--trans", str(AML_TRANS), "--patterns", str(AML_PATTERNS)]))
    assert st_.generator is None and st_.dataset is not None
    seen: list[dict] = []
    st_.publisher.subscribe(EVENTS, seen.append)
    with TestClient(create_app(st_)) as c:
        doc = _wait_done(c)
        deadline = time.monotonic() + 10
        while len(seen) < 37 and time.monotonic() < deadline:
            time.sleep(0.02)
        r = c.post("/api/v1/sim/generator/start")
    assert r.status_code == 409 and r.json() == {"detail": "dataset streaming is active"}
    assert doc["status"] == "DONE" and doc["kind"] == "amlworld" and doc["error"] is None
    assert doc["rows_read"] == 40 and doc["skipped"] == 3 and doc["in_scope"] == 37 and doc["submitted"] == 37
    assert doc["progress"] == 1.0 and sum(doc["skip_reasons"].values()) == 3
    assert doc["files"] == {"trans": str(AML_TRANS), "patterns": str(AML_PATTERNS), "mapping": None}
    ev = doc["evaluation"]
    types = {t["pattern_type"]: t for t in ev["pattern_types"]}
    assert types and all(set(t) >= {"patterns", "detected_patterns", "recall", "precision", "precision_reason"}
                         for t in types.values())
    for t in types.values():
        if not t["mapped"]:
            assert t["precision"] is None and t["precision_reason"] == "unmapped"
    assert len(seen) == 37
    for msg in seen:  # /ws/events payloads: no label or pattern field (Req 39.4)
        keys = set(msg["event"])
        assert not keys & {"label", "is_laundering", "Is Laundering", "pattern_id", "pattern_type"}
        assert "warmup" not in msg


def test_feeder_failure_sets_failed_and_server_keeps_running(tmp_path):
    p = tmp_path / "HI-Gone_Trans.csv"
    p.write_text(AML_TRANS.read_text(encoding="utf-8"), encoding="utf-8")
    st_ = build_state(parse_args(["--dataset", "amlworld", "--trans", str(p)]))
    p.unlink()  # passes the precheck, then disappears before the background load
    with TestClient(create_app(st_)) as c:
        doc = _wait_done(c)
        assert c.get("/api/v1/status").status_code == 200
    assert doc["status"] == "FAILED" and "file not found" in doc["error"]

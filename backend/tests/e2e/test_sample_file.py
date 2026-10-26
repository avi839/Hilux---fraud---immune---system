"""End-to-end test on docs/sample_transactions.jsonl (task 8.2).

Raw lines -> StreamIngestor -> Helix (low Minimum_History_Count) -> Cases, Evidence_Chains
and explanations. Expected story of the sample file:

- 15 lines: 13 accepted, 1 duplicate (second T007), 1 rejected (T009: negative amount,
  currency "euro").
- Alice's T007 (large amount, Berlin -> Lagos in 40 min, new payee) alerts HIGH/CRITICAL.
- Alice's routine T001-T006 create no Case.
- Bob's T010-T012 (three ~9.8k transfers to fresh shells) form one STRUCTURING Case.
- Carl -> Dave -> Erin -> Finn (T013-T015) form one MULE_CHAIN Case.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pytest

from helix.cases import Case
from helix.config import HelixConfig, SeverityTier
from helix.config.schema import SensorConfig
from helix.core import Helix
from helix.core.pipeline import EventOutcome
from helix.explain import Explanation, dumps, explain, loads
from helix.ingest import StreamIngestor
from helix.motifs import MotifType
from helix.risk import tier_rank
from helix.sim.clock import ManualClock

SAMPLE = Path(__file__).resolve().parents[3] / "docs" / "sample_transactions.jsonl"
START = datetime(2024, 6, 2, 0, 0, tzinfo=timezone.utc)  # before the first event (2024-06-03)

ALICE_ROUTINE = {f"T00{i}" for i in range(1, 7)}
BOB = {"T010", "T011", "T012"}
MULE = {"T013", "T014", "T015"}


def const_clock() -> float:
    return 0.0  # sensors never time out: deterministic stage 1


@dataclass
class Run:
    ingestor: StreamIngestor
    helix: Helix
    outcomes: dict[str, EventOutcome]


def run_sample(storage: Optional[object] = None) -> Run:
    cfg = HelixConfig(sensors=SensorConfig(min_history_count=5))
    ing = StreamIngestor(cfg.ingest, ManualClock(START))
    with SAMPLE.open("r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                ing.submit(line.rstrip("\r\n"))
    h = Helix(cfg, ManualClock(START), storage=storage, sensor_clock=const_clock)
    outcomes = {o.event.tx_id: o for o in h.process_all(ing.drain())}
    return Run(ing, h, outcomes)


def open_cases(h: Helix) -> list[Case]:
    return h.cases.open_cases()


def case_with(h: Helix, txs: set[str]) -> list[Case]:
    return [c for c in open_cases(h) if txs <= c.tx_ids]


def render(h: Helix, case: Case) -> Explanation:
    return explain(case, h.chain(case.case_id), currency="EUR")


@pytest.fixture(scope="module")
def run() -> Run:
    return run_sample()


# --- ingest ------------------------------------------------------------------------------


def test_ingest_counters_and_rejected_log(run: Run):
    c = run.ingestor.counters
    assert (c.accepted, c.duplicate, c.malformed) == (13, 1, 1)
    assert c.delivered == 13
    recs = run.ingestor.rejected_log.records()
    assert len(recs) == 1
    assert '"T009"' in recs[0].raw
    assert set(recs[0].fields) == {"amount", "currency"}
    assert len(run.outcomes) == 13


# --- detections --------------------------------------------------------------------------


def test_alice_t007_high_tier_case(run: Run):
    h = run.helix
    o = run.outcomes["T007"]
    assert o.stage1.belief > 0.5
    cs = case_with(h, {"T007"})
    assert len(cs) == 1
    case = cs[0]
    assert tier_rank(case.tier) >= tier_rank(SeverityTier.HIGH)
    reasons = {it.name for it in render(h, case).reasons}
    for sensor in ("amount", "geo", "counterparty"):
        assert any(sensor in name for name in reasons), (sensor, reasons)


def test_alice_routine_events_create_no_case(run: Run):
    h = run.helix
    for tx in sorted(ALICE_ROUTINE):
        assert run.outcomes[tx].case_id is None, tx
    for c in h.cases.cases.values():  # including closed / merged Cases
        assert not (c.tx_ids & ALICE_ROUTINE), (c.case_id, c.tx_ids)


def test_bob_structuring_case(run: Run):
    cs = [c for c in open_cases(run.helix) if c.tx_ids == BOB]
    assert len(cs) == 1, [(c.case_id, sorted(c.tx_ids)) for c in open_cases(run.helix)]
    case = cs[0]
    assert MotifType.STRUCTURING in {m.motif_type for m in case.motifs.values()}
    assert tier_rank(case.tier) >= tier_rank(SeverityTier.LOW)


def test_mule_chain_case(run: Run):
    cs = case_with(run.helix, MULE)
    assert len(cs) == 1, [(c.case_id, sorted(c.tx_ids)) for c in open_cases(run.helix)]
    assert MotifType.MULE_CHAIN in {m.motif_type for m in cs[0].motifs.values()}


def test_three_cases_are_distinct(run: Run):
    h = run.helix
    ids = {case_with(h, {"T007"})[0].case_id, case_with(h, BOB)[0].case_id, case_with(h, MULE)[0].case_id}
    assert len(ids) == 3


def test_evidence_chains_verify_and_round_trip(run: Run):
    h = run.helix
    assert open_cases(h)
    for case in open_cases(h):
        chain = h.chain(case.case_id)
        assert chain is not None, case.case_id
        assert chain.verify() == [], case.case_id
        s = dumps(chain)
        back = loads(s)
        assert back == chain
        assert dumps(back) == s


def test_print_explanations(run: Run):
    h = run.helix
    for case in sorted(open_cases(h), key=lambda c: c.seq_no):
        print(f"\n===== {case.case_id}  tx={sorted(case.tx_ids)}  "
              f"motifs={sorted(m.motif_type.value for m in case.motifs.values())} =====")
        print(render(h, case).text)


# --- with DuckDB Storage -----------------------------------------------------------------


def test_sample_with_duckdb_storage(tmp_path):
    from helix.storage import Storage

    st = Storage(tmp_path / "e2e.duckdb").open()
    try:
        r = run_sample(storage=st)
        assert r.helix.storage_errors == 0
        assert st.flush()
        assert len(st.events()) == 13
        # the same detections as the in-memory run
        h = r.helix
        assert len(case_with(h, {"T007"})) == 1
        assert [c for c in open_cases(h) if c.tx_ids == BOB]
        assert len(case_with(h, MULE)) == 1
    finally:
        st.close()

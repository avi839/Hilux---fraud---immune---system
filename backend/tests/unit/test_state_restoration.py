"""State restoration on restart from Storage (task 19.2, Req 24.6, 24.7, 24.8)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterator

import pytest

from helix.api.state import AppState
from helix.cases import CaseStatus, Placement
from helix.codec import TransactionEvent, parse
from helix.config import HelixConfig
from helix.config.schema import SensorConfig
from helix.explain import dumps
from helix.sim.clock import ManualClock
from helix.storage import Storage

SAMPLE = Path(__file__).resolve().parents[3] / "docs" / "sample_transactions.jsonl"
START = datetime(2024, 6, 2, tzinfo=timezone.utc)


def const_clock() -> float:
    return 0.0  # sensors never time out: deterministic stage 1


def cfg() -> HelixConfig:
    return HelixConfig(sensors=SensorConfig(min_history_count=5))


def sample_events() -> list[TransactionEvent]:
    out, seen = [], set()
    for ln in SAMPLE.read_text(encoding="utf-8").splitlines():
        ev = parse(ln) if ln.strip() else None
        if isinstance(ev, TransactionEvent) and ev.tx_id not in seen:
            seen.add(ev.tx_id)
            out.append(ev)
    return out


@pytest.fixture
def db(tmp_path: Path) -> Iterator[list[Storage]]:
    """Path factory for Storage files; every Storage opened through it is closed at the end
    (on Windows an open DuckDB file cannot be removed)."""
    opened: list[Storage] = []
    yield opened
    for st in opened:
        if st.is_open:
            st.close()


def open_storage(opened: list[Storage], p: Path) -> Storage:
    st = Storage(p).open()
    opened.append(st)
    return st


def build(st: Storage, *, clock: ManualClock | None = None, restore: bool = True) -> AppState:
    return AppState.build(cfg(), clock or ManualClock(START), storage=st, restore_state=restore,
                          learner=False, sensor_clock=const_clock)


def run_and_stop(state: AppState, st: Storage, events: list[TransactionEvent]) -> None:
    for ev in events:
        state.helix.process(ev)
    dna, cpd = state.helix.snapshot_state()  # also queues every coalesced Case write
    st.snapshot(dna, cpd, state.clock.now())
    assert st.flush()
    st.close()


def case_view(c) -> tuple:
    return (c.case_id, c.status, c.tier, c.placement, sorted(c.tx_ids), sorted(c.accounts),
            c.expected_loss, c.label, c.merged_into, [(h.action, h.prev, h.new, h.actor) for h in c.history])


def test_restart_restores_cases_queues_chains_graph_and_dna(db, tmp_path):
    p = tmp_path / "helix.duckdb"
    st = open_storage(db, p)
    before = build(st, restore=False)
    events = sample_events()
    run_and_stop(before, st, events)
    h0 = before.helix
    assert h0.cases.cases, "the sample must produce Cases"

    st2 = open_storage(db, p)
    clock2 = ManualClock(START)
    after = build(st2, clock=clock2)
    h1 = after.helix

    assert h1.seq_no == h0.seq_no == len(events)
    assert clock2.now() >= max(e.ts for e in events)
    assert sorted(h1.cases.cases) == sorted(h0.cases.cases)
    for cid, c0 in h0.cases.cases.items():
        assert case_view(h1.cases.cases[cid]) == case_view(c0)
    assert h1.prioritiser.active_ids() == h0.prioritiser.active_ids()
    assert h1.prioritiser.backlog_ids() == h0.prioritiser.backlog_ids()
    assert h1.cases.tx_to_case == h0.cases.tx_to_case
    assert sorted(h1.chains) == sorted(h0.chains)
    for cid in h0.chains:
        assert dumps(h1.chains[cid]) == dumps(h0.chains[cid])
    assert sorted(e.tx_id for e in h1.graph.edges()) == sorted(e.tx_id for e in h0.graph.edges())
    for acc in {e.src for e in events}:
        assert h1.profiles.view(acc).count == h0.profiles.view(acc).count
    # The Replay_Service reads the restored Cases.
    any_case = next(iter(h1.cases.cases))
    assert after.replay.case(any_case) is h1.cases.cases[any_case]


def test_processing_after_restart_continues_cleanly(db, tmp_path):
    """New Case IDs do not collide with restored ones and already-stored history rows are
    not appended again (no Storage write failures)."""
    p = tmp_path / "helix.duckdb"
    st = open_storage(db, p)
    events = sample_events()
    run_and_stop(build(st, restore=False), st, events)

    st2 = open_storage(db, p)
    h = build(st2).helix
    restored_ids = set(h.cases.cases)
    last = max(e.ts for e in events)
    # Touch every restored open Case's accounts again and add an unrelated large transfer.
    open_case = next(c for c in h.cases.cases.values() if c.status is not CaseStatus.CLOSED)
    acc = sorted(open_case.accounts)
    new = [TransactionEvent(tx_id=f"R{i}", ts=last + timedelta(minutes=i + 1), src=acc[0], dst=acc[-1],
                            amount=Decimal("9000.00"), currency="EUR") for i in range(3)]
    for ev in new:
        h.process(ev)
    h.flush_cases()
    assert st2.flush()
    assert h.storage_errors == 0 and not st2.degraded
    assert h.seq_no == len(events) + len(new)
    for cid in set(h.cases.cases) - restored_ids:
        assert int(cid.split("-")[1]) > max(int(c.split("-")[1]) for c in restored_ids)
    # Stored history is still one row per entry, in order.
    rows = st2.load_rows("case_history")
    keys = [(r["case_id"], r["entry_no"]) for r in rows]
    assert len(keys) == len(set(keys))


def test_restore_without_snapshot_uses_stored_events(db, tmp_path):
    """No snapshot: seq_no, clock and graph still come from the stored events."""
    p = tmp_path / "helix.duckdb"
    st = open_storage(db, p)
    state = build(st, restore=False)
    ev = TransactionEvent(tx_id="T1", ts=START + timedelta(hours=5), src="A1", dst="A2",
                          amount=Decimal("100.00"), currency="USD")
    state.helix.process(ev)
    assert st.flush()
    st.close()

    clock2 = ManualClock(START)
    h = build(open_storage(db, p), clock=clock2).helix
    assert h.seq_no == 1
    assert clock2.now() == ev.ts
    assert [e.tx_id for e in h.graph.edges()] == ["T1"]


def test_empty_storage_starts_with_default_state(db, tmp_path):
    p = tmp_path / "helix.duckdb"
    open_storage(db, p).close()
    h = build(open_storage(db, p)).helix
    assert h.seq_no == 0 and not h.cases.cases and not h.chains
    assert h.profiles.view("A1").count == 0


def test_restore_state_false_skips_restoration(db, tmp_path):
    p = tmp_path / "helix.duckdb"
    st = open_storage(db, p)
    run_and_stop(build(st, restore=False), st, sample_events())
    h = build(open_storage(db, p), restore=False).helix
    assert h.seq_no == 0 and not h.cases.cases


def test_build_without_storage_ignores_restore_flag():
    state = AppState.build(cfg(), ManualClock(START), storage=None, restore_state=True, learner=False)
    assert state.helix.seq_no == 0 and state.replay is None


def test_restored_queue_membership_is_kept_not_readmitted(db, tmp_path):
    """Restored Active Cases stay Active even when the new period's budget is smaller
    (membership equals the last persisted value, Req 24.6)."""
    p = tmp_path / "helix.duckdb"
    st = open_storage(db, p)
    run_and_stop(build(st, restore=False), st, sample_events())
    st2 = open_storage(db, p)
    rows = {r["case_id"]: r["placement"] for r in st2.load_rows("cases") if r["status"] != "CLOSED"}
    small = cfg()
    small = small.model_copy(update={"prioritiser": small.prioritiser.model_copy(update={"alert_budget": 1})})
    h = AppState.build(small, ManualClock(START), storage=st2, learner=False, sensor_clock=const_clock).helix
    for cid, placement in rows.items():
        assert h.cases.cases[cid].placement is Placement(placement)
        assert h.prioritiser.placement_of(cid) is Placement(placement)

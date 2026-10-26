"""Replay_Service flushes Storage before timeline reads (task 19.2, Req 24.1)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

import pytest

from helix.api.state import AppState
from helix.codec import TransactionEvent, parse
from helix.config import HelixConfig
from helix.config.schema import SensorConfig
from helix.sim.clock import ManualClock
from helix.storage import Storage

SAMPLE = Path(__file__).resolve().parents[3] / "docs" / "sample_transactions.jsonl"
START = datetime(2024, 6, 2, tzinfo=timezone.utc)


def sample_events() -> list[TransactionEvent]:
    out, seen = [], set()
    for ln in SAMPLE.read_text(encoding="utf-8").splitlines():
        ev = parse(ln) if ln.strip() else None
        if isinstance(ev, TransactionEvent) and ev.tx_id not in seen:
            seen.add(ev.tx_id)
            out.append(ev)
    return out


@pytest.fixture
def state(tmp_path: Path) -> Iterator[AppState]:
    """A pipeline over a buffered Storage (no writer task: rows stay pending until a flush)."""
    st = Storage(tmp_path / "helix.duckdb").open()
    try:
        cfg = HelixConfig(sensors=SensorConfig(min_history_count=5))
        s = AppState.build(cfg, ManualClock(START), storage=st, learner=False, sensor_clock=lambda: 0.0)
        for ev in sample_events():
            s.helix.process(ev)
        yield s
    finally:
        st.close()


def a_case(state: AppState) -> str:
    return next(cid for cid, c in state.helix.cases.cases.items() if c.tx_ids)


def test_timeline_reads_pending_events(state: AppState):
    st, cid = state.storage, a_case(state)
    assert st.pending > 0  # events are buffered, not yet in DuckDB
    tl = state.replay.timeline(cid)
    assert st.pending == 0
    assert set(state.helix.cases.cases[cid].tx_ids) <= {e.tx_id for e in tl.entries}


@pytest.mark.parametrize("read", ["timeline", "at", "recompute_run"])
def test_every_storage_read_flushes_first(state: AppState, read: str):
    st, cid = state.storage, a_case(state)
    c = state.helix.cases.cases[cid]
    args = (cid, c.created_at) if read == "at" else (cid,)  # inside the Case's timeline
    with patch.object(st, "flush", wraps=st.flush) as flush:
        getattr(state.replay, read)(*args)
    assert flush.call_count >= 1
    assert st.pending == 0

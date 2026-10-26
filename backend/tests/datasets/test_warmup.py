"""Warm_Up_Mode (design §25, Req 40.7, 40.8): Property 85 plus a live-API unit test."""

from __future__ import annotations

import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

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
from helix.datasets.warmup import accounts_ready, build_report
from helix.replay.engine import DefaultStage1Engine
from helix.sim import ManualClock

UTC = timezone.utc
REPO = Path(__file__).resolve().parents[3]
GENERIC_CSV = Path(__file__).resolve().parents[1] / "fixtures" / "onboarding" / "generic_bank.csv"
GENERIC_MAP = REPO / "mappings" / "generic_bank.yaml"
T0 = datetime(2024, 1, 1, tzinfo=UTC)


@st.composite
def event_streams(draw):
    accts = [f"A{i}" for i in range(draw(st.integers(1, 5)))]
    t, out = T0, []
    for i in range(draw(st.integers(1, 40))):
        t += timedelta(seconds=draw(st.integers(0, 7200)))
        out.append(TransactionEvent(
            tx_id=f"w{i}", ts=t, src=draw(st.sampled_from(accts)), dst=draw(st.sampled_from(accts + ["X1", "X2"])),
            amount=draw(st.sampled_from([Decimal("12.00"), Decimal("80.50"), Decimal("9999.00"), Decimal("250000.00")])),
            currency="GBP", channel=draw(st.sampled_from([None, "card", "wire"])),
        ))
    return out


# Feature: helix-fraud-immune-system, Property 85: Warm-up builds memory without Cases
@pytest.mark.property
@settings(max_examples=40)
@given(events=event_streams())
def test_warmup_builds_memory_without_cases(events):
    """**Validates: Requirements 40.7, 40.8**"""
    cfg = HelixConfig()
    state = AppState.build(cfg, ManualClock(T0), immune=True)
    published: list[dict] = []
    state.publisher.subscribe(EVENTS, published.append)
    helix = state.helix
    helix.warmup = True
    for ev in events:
        helix.process(ev)
    assert helix.cases.cases == {} and len(helix.prioritiser.backlog) == 0
    assert all(c.label is None for c in helix.cases.cases.values())  # no Feedback_Label
    assert state.immune.self_size == 0  # no Self_Set offer
    assert len(published) == len(events) and all(m["warmup"] is True and m["case_id"] is None for m in published)
    fresh = DefaultStage1Engine(cfg)
    for ev in events:
        fresh.commit(ev)
    assert helix.profiles.accounts() == fresh.profiles.accounts()
    for a in fresh.profiles.accounts():
        assert helix.profiles.view(a) == fresh.profiles.view(a)
    mh = cfg.sensors.min_history_count
    counts = Counter(ev.src for ev in events)
    rep = build_report(helix, len(events), 0.5, None)
    assert rep.accounts_ready == sum(1 for n in counts.values() if n >= mh) == accounts_ready(fresh.profiles, mh)[1]
    assert rep.accounts == len(counts) and rep.events_per_s == len(events) / 0.5
    helix.warmup = False  # Case creation re-enabled
    assert not helix.warmup and not getattr(helix.stage1, "suspend_self_set", False)


def test_live_warmup_then_streaming_reports_through_get_dataset():
    st_ = build_state(parse_args(["--dataset", "csv", "--mapping", str(GENERIC_MAP), "--trans", str(GENERIC_CSV),
                                  "--warmup", "--warmup-until", "2024-03-01T08:30:00Z"]))
    flags: list[bool] = []
    st_.publisher.subscribe(EVENTS, lambda m: flags.append(bool(m.get("warmup"))))
    with TestClient(create_app(st_)) as c:
        deadline = time.monotonic() + 30
        while (doc := c.get("/api/v1/dataset").json())["status"] not in ("DONE", "FAILED"):
            assert time.monotonic() < deadline
            time.sleep(0.02)
    assert doc["status"] == "DONE", doc["error"]
    w = doc["warmup"]
    # In-scope rows before 08:30Z (generic_bank.csv): T0001-T0004, T0008-T0010, T0015.
    assert w["done"] is True and w["events"] == 8 and w["until"] == "2024-03-01T08:30:00+00:00"
    assert w["accounts"] == 7 and w["accounts_ready"] == 0 and w["min_history"] == 5
    assert w["duration_s"] >= 0 and w["events_per_s"] >= 0
    assert flags[:8] == [True] * 8 and not any(flags[8:]) and len(flags) == 21
    assert doc["submitted"] == 21 and st_.helix.warmup is False
    warm_ids = {"T0001", "T0002", "T0003", "T0004", "T0008", "T0009", "T0010", "T0015"}
    for case in st_.helix.cases.cases.values():  # only post-warm-up events create Cases
        assert set(case.tx_ids) - warm_ids

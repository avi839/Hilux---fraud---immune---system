"""Unit tests for helix.sim.clock."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from helix.sim import ManualClock, SimClock, WallClock

T0 = datetime(2024, 5, 1, 12, 0, tzinfo=timezone.utc)


def test_clocks_satisfy_protocol():
    assert isinstance(ManualClock(T0), SimClock)
    assert isinstance(WallClock(), SimClock)


def test_manual_clock_advances_exactly():
    c = ManualClock(T0)
    assert c.now() == T0
    c.advance(timedelta(seconds=90))
    c.advance(timedelta(0))
    assert c.now() == T0 + timedelta(seconds=90)


def test_manual_clock_normalises_to_utc():
    plus2 = timezone(timedelta(hours=2))
    c = ManualClock(datetime(2024, 5, 1, 14, 0, tzinfo=plus2))
    assert c.now() == T0 and c.now().tzinfo == timezone.utc


def test_manual_clock_rejects_naive_and_backwards():
    with pytest.raises(ValueError):
        ManualClock(datetime(2024, 1, 1))
    c = ManualClock(T0)
    with pytest.raises(ValueError):
        c.advance(timedelta(seconds=-1))
    with pytest.raises(ValueError):
        c.set(T0 - timedelta(microseconds=1))
    c.set(T0 + timedelta(hours=1))
    assert c.now() == T0 + timedelta(hours=1)


def test_wall_clock_is_aware_utc_and_applies_offset():
    c = WallClock(source=lambda: T0)
    assert c.now() == T0 and c.now().tzinfo == timezone.utc
    c.advance(timedelta(minutes=5))
    assert c.now() == T0 + timedelta(minutes=5)


def test_wall_clock_never_goes_backwards():
    readings = iter([T0, T0 - timedelta(seconds=10), T0 + timedelta(seconds=1)])
    c = WallClock(source=lambda: next(readings))
    assert c.now() == T0
    assert c.now() == T0  # system clock stepped back; clamped
    assert c.now() == T0 + timedelta(seconds=1)
    with pytest.raises(ValueError):
        c.advance(timedelta(seconds=-1))


def test_wall_clock_default_source_is_real_time():
    before = datetime.now(timezone.utc)
    now = WallClock().now()
    assert before <= now <= datetime.now(timezone.utc)

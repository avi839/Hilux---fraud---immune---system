"""Unit tests for helix.memory (Profile_Store / Behavioural_DNA, design §7)."""
from __future__ import annotations

import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from helix.config import HelixConfig
from helix.memory import DecayedStats, EventLike, ProfileStore, ProfileStoreConfigError

T0 = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)  # a Monday, 09:00 UTC
DAY = timedelta(days=1)


@dataclass(frozen=True, slots=True)
class Ev:  # minimal stand-in satisfying helix.memory.EventLike
    tx_id: str
    ts: datetime
    src: str
    dst: str
    amount: Decimal
    lat: float | None = None
    lon: float | None = None
    channel: str | None = None
    device_id: str | None = None


def ev(i, ts, amount="10.00", dst="B", src="A", **kw) -> Ev:
    return Ev(f"t{i}", ts, src, dst, Decimal(amount), **kw)


def test_event_stand_in_satisfies_protocol():
    assert isinstance(ev(0, T0), EventLike)


def test_unknown_account_gives_empty_view_without_creating_state():
    s = ProfileStore()
    v = s.view("nobody")
    assert v.count == 0 and v.counterparties == frozenset() and v.t_last is None
    assert v.amount.w == 0 and v.iat.w == 0 and v.last_location is None
    assert s.accounts() == frozenset()


def test_first_event_initialises_dna():
    s = ProfileStore()
    s.apply(ev(0, T0, "12.50", lat=1.0, lon=2.0, channel="card"))
    v = s.view("A")
    assert v.count == 1 and v.t_last == T0
    assert v.amount.mean == 12.5 and v.amount.var == 0.0 and v.amount.w == 1.0
    assert v.iat.w == 0.0  # no inter-arrival yet
    assert v.hour_hist[9] == 1.0 and v.weekday_hist[0] == 1.0
    assert dict(v.channel_counts) == {"card": 1.0}
    assert v.last_location == (1.0, 2.0, T0)
    assert v.counterparties == {"B"}


def test_decay_uses_half_life():
    s = ProfileStore(half_life_days=1)
    s.apply(ev(0, T0, "10.00"))
    s.apply(ev(1, T0 + DAY, "20.00"))
    v = s.view("A")
    mean = (0.5 * 10 + 20) / 1.5
    var = (0.5 * (10 - mean) ** 2 + (20 - mean) ** 2) / 1.5
    assert v.amount.w == pytest.approx(1.5)
    assert v.amount.mean == pytest.approx(mean)
    assert v.amount.var == pytest.approx(var)
    assert v.iat.w == 1.0 and v.iat.mean == 86_400.0
    assert v.hour_hist[9] == pytest.approx(1.5)


def test_late_event_uses_zero_dt_and_keeps_running_max():
    s = ProfileStore(half_life_days=1)
    s.apply(ev(0, T0 + DAY, "10.00"))
    s.apply(ev(1, T0, "30.00"))  # earlier than t_last
    v = s.view("A")
    assert v.t_last == T0 + DAY
    assert v.amount.w == 2.0 and v.amount.mean == 20.0  # no decay
    assert v.iat.w == 1.0 and v.iat.mean == 0.0


def test_west_update_matches_naive_form():
    st = DecayedStats()
    W = S1 = S2 = 0.0
    for x, d in [(3.0, 1.0), (5.0, 0.7), (11.0, 0.2), (2.0, 1.0)]:
        st.update(x, d)
        W, S1, S2 = d * W + 1, d * S1 + x, d * S2 + x * x
    assert st.w == pytest.approx(W)
    assert st.mean == pytest.approx(S1 / W)
    assert st.var == pytest.approx(S2 / W - (S1 / W) ** 2)


def test_missing_optional_fields_leave_derived_stats_unchanged():
    s = ProfileStore()
    s.apply(ev(0, T0, lat=1.0, lon=2.0, channel="card"))
    before = s.view("A")
    s.apply(ev(1, T0 + DAY, lat=5.0, lon=None, channel=None))
    after = s.view("A")
    assert after.last_location == before.last_location
    assert after.channel_counts == before.channel_counts and after.channel_t == T0
    assert after.count == 2 and after.amount.w > 1.0  # remaining stats updated


def test_channel_counts_decay_lazily_on_next_channel_event():
    s = ProfileStore(half_life_days=1)
    s.apply(ev(0, T0, channel="card"))
    s.apply(ev(1, T0 + DAY, channel=None))
    s.apply(ev(2, T0 + 2 * DAY, channel="web"))
    v = s.view("A")
    assert v.channel_counts["card"] == pytest.approx(0.25)
    assert v.channel_counts["web"] == 1.0 and v.channel_t == T0 + 2 * DAY


def test_earlier_location_does_not_overwrite_latest():
    s = ProfileStore()
    s.apply(ev(0, T0 + DAY, lat=1.0, lon=1.0))
    s.apply(ev(1, T0, lat=9.0, lon=9.0))
    assert s.view("A").last_location == (1.0, 1.0, T0 + DAY)


def test_recent_timestamps_keep_last_24h():
    s = ProfileStore()
    for i, h in enumerate([0, 10, 20, 30]):
        s.apply(ev(i, T0 + timedelta(hours=h)))
    v = s.view("A")
    assert v.recent_ts == tuple(T0 + timedelta(hours=h) for h in (10, 20, 30))
    assert v.recent_count(T0 + timedelta(hours=20)) == 2


def test_view_is_a_snapshot():
    s = ProfileStore()
    s.apply(ev(0, T0))
    v = s.view("A")
    s.apply(ev(1, T0 + DAY, dst="C"))
    assert v.count == 1 and v.counterparties == {"B"}
    with pytest.raises(TypeError):
        v.channel_counts["x"] = 1.0  # type: ignore[index]


@pytest.mark.parametrize("bad", [0.5, 366, -1])
def test_half_life_out_of_range_is_refused(bad):
    with pytest.raises(ProfileStoreConfigError, match="half_life_days") as e:
        ProfileStore(half_life_days=bad)
    assert e.value.setting == "memory.half_life_days"


def test_from_config_uses_memory_half_life():
    s = ProfileStore.from_config(HelixConfig())
    assert s.half_life_s == 30 * 86_400


def test_withhold_applied_event_removes_it_exactly():
    s, ref = ProfileStore(), ProfileStore()
    events = [ev(0, T0, "5.00"), ev(1, T0 + DAY, "500.00", dst="X"), ev(2, T0 + 2 * DAY, "6.00")]
    for e in events:
        s.apply(e)
    for e in (events[0], events[2]):
        ref.apply(e)
    seen = []
    assert s.withhold("A", {"t1"}, on_rebuild=lambda a, log: seen.append([e.tx_id for e in log])) == {"t1"}
    assert s.view("A").isclose(ref.view("A"), rel=0.0, abs_=0.0)
    assert "X" not in s.view("A").counterparties
    assert seen == [["t0", "t2"]]
    assert s.withheld("A") == {"t1"}


def test_withhold_before_arrival_and_flagged_apply():
    s = ProfileStore()
    s.withhold("A", {"t1"})
    assert s.apply(ev(0, T0)) is True
    assert s.apply(ev(1, T0 + DAY)) is False
    assert s.apply(ev(2, T0 + DAY), withhold=True) is False
    assert s.view("A").count == 1 and s.withheld("A") == {"t1", "t2"}


def test_release_applies_in_timestamp_order_once_and_skips_canaries():
    s = ProfileStore(is_canary=lambda a: a == "CANARY")
    s.apply(ev(0, T0 + 3 * DAY))
    for e in [ev(1, T0 + 2 * DAY, dst="P"), ev(2, T0 + DAY, dst="Q"), ev(3, T0, dst="CANARY")]:
        s.apply(e, withhold=True)
    hooked = []
    out = s.release("A", {"t1", "t2", "t3"}, on_apply=lambda a, e: hooked.append(e.tx_id))
    assert [e.tx_id for e in out] == ["t2", "t1"] == hooked
    v = s.view("A")
    assert v.count == 3 and {"P", "Q"} <= v.counterparties and "CANARY" not in v.counterparties
    assert s.release("A", {"t1", "t2"}) == [] and s.view("A").count == 3
    assert s.withheld("A") == frozenset()


def test_exclude_drops_withheld_applied_and_future_events():
    s = ProfileStore()
    s.apply(ev(0, T0))
    s.apply(ev(1, T0 + DAY), withhold=True)
    s.apply(ev(2, T0 + DAY, dst="Z"))
    assert s.exclude("A", {"t1", "t2", "t9"}) == {"t2"}
    assert s.withheld("A") == frozenset()
    assert s.apply(ev(9, T0 + 2 * DAY)) is False
    v = s.view("A")
    assert v.count == 1 and v.counterparties == {"B"}
    assert s.release("A", {"t1"}) == []


def test_events_beyond_horizon_are_folded_and_cannot_be_removed():
    s = ProfileStore(horizon_s=2 * 86_400)
    for i in range(6):
        s.apply(ev(i, T0 + i * DAY))
    before = s.view("A")
    assert s.withhold("A", {"t0"}) == set()  # folded into the base
    assert s.view("A").isclose(before, rel=0.0, abs_=0.0)
    assert s.withhold("A", {"t3", "t5"}) == {"t3", "t5"}  # within one horizon


def test_rebase_rebuilds_regime_stats_and_keeps_counterparties_and_count():
    s = ProfileStore(half_life_days=365)
    for i in range(5):
        s.apply(ev(i, T0 + i * DAY, "10.00", dst=f"old{i}", channel="card", lat=0.0, lon=0.0))
    for i in range(5, 8):
        s.apply(ev(i, T0 + i * DAY, "1000.00", dst="new", channel="web"))
    assert s.rebase("A", "t5") is True
    v = s.view("A")
    assert v.count == 8
    assert v.counterparties == {f"old{i}" for i in range(5)} | {"new"}
    d = 0.5 ** (1 / 365)  # one-day decay
    assert v.amount.w == pytest.approx(1 + d + d * d) and v.amount.mean == pytest.approx(1000.0)
    assert v.amount.var == pytest.approx(0.0, abs=1e-9)
    assert set(v.channel_counts) == {"web"} and v.last_location is None
    assert sum(v.hour_hist) == pytest.approx(1 + d + d * d)
    assert v.iat.w == pytest.approx(1 + d + d * d) and v.iat.mean == pytest.approx(86_400.0)
    # Withholding after a rebase still rebuilds exactly from the new base.
    assert s.withhold("A", {"t7"}) == {"t7"}
    assert s.view("A").count == 7 and s.view("A").amount.w == pytest.approx(1 + d)


def test_rebase_unknown_tx_uses_whole_log():
    s = ProfileStore()
    s.apply(ev(0, T0, "10.00"))
    s.apply(ev(1, T0 + DAY, "30.00"))
    assert s.rebase("A", "missing") is False
    d = 0.5 ** (1 / 30)
    assert s.view("A").amount.mean == pytest.approx((10 * d + 30) / (d + 1))
    assert s.rebase("nobody", "t0") is False


@pytest.mark.perf
def test_update_time_does_not_grow_with_history():  # Req 8.2
    def median_update(prior: int) -> float:
        s = ProfileStore()
        t = T0
        for i in range(prior):
            t += timedelta(seconds=60)
            s.apply(ev(i, t, dst=f"d{i % 50}", channel="card", lat=1.0, lon=1.0))
        samples = []
        for j in range(2_000):
            t += timedelta(seconds=60)
            e = ev(prior + j, t, dst=f"d{j % 50}", channel="card", lat=1.0, lon=1.0)
            t0 = time.perf_counter()
            s.apply(e)
            samples.append(time.perf_counter() - t0)
        return statistics.median(samples)

    assert median_update(100_000) <= 2 * median_update(100)

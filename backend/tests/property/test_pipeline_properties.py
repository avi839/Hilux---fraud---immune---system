"""Property tests for helix.core.pipeline (design Properties 24, 28, 42, 73)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from helix.codec import TransactionEvent
from helix.codec.models import OPTIONAL_FIELDS
from helix.config import HelixConfig
from helix.config.schema import SensorConfig
from helix.core import UNAVAILABLE, Helix, LatencyRecorder, Stage
from helix.fusion import Mass
from helix.motifs import MotifEngine
from helix.sensors import SensorOutput
from helix.sim.clock import ManualClock

T0 = datetime(2024, 3, 1, 9, 0, tzinfo=timezone.utc)
ACCOUNTS = "ABCDE"
RISKS = (30, 55, 65, 75, 85, 95)  # percent; none sits exactly on the 0.6 threshold


def const_clock() -> float:
    return 0.0


class RiskSensor:
    """Test sensor: ``mcc`` holds the intended stage-1 risk in percent."""

    name = "fixed"

    def evaluate(self, ev, dna_before, ctx) -> SensorOutput:
        r = int(ev.mcc) / 100
        m = Mass(2 * r - 1, 0.0, 2 - 2 * r) if r >= 0.5 else Mass(0.0, 1 - 2 * r, 2 * r)
        return SensorOutput(m, {"r": r})


# (src, dst offset, gap minutes, amount, risk %)
event_specs = st.lists(
    st.tuples(st.integers(0, 4), st.integers(1, 4), st.integers(0, 40),
              st.sampled_from(["9500.00", "9800.00", "8000.00", "120.00"]), st.sampled_from(RISKS)),
    min_size=1, max_size=25,
)


def to_events(specs) -> list[TransactionEvent]:
    out, t = [], T0
    for i, (s, d, gap, amount, r) in enumerate(specs):
        t += timedelta(minutes=gap)
        out.append(TransactionEvent(tx_id=f"T{i:03d}", ts=t, src=ACCOUNTS[s], dst=ACCOUNTS[(s + d) % 5],
                                    amount=Decimal(amount), currency="EUR", mcc=str(r)))
    return out


def fixed_helix(motifs: bool = True) -> Helix:
    return Helix(HelixConfig(), ManualClock(T0), sensors=[RiskSensor()], sensor_clock=const_clock,
                 motif_engine=None if motifs else MotifEngine(detectors=()))


# --- Property 73 ----------------------------------------------------------------------------


# Feature: helix-fraud-immune-system, Property 73: Latency reports never invent values
@given(
    samples=st.lists(st.tuples(st.floats(0, 200), st.floats(0, 1e4, allow_nan=False)), max_size=60),
    query_at=st.floats(0, 300),
    reservoir=st.integers(1, 20),
)
def test_p73_latency_reports_never_invent_values(samples, query_at, reservoir):
    """**Validates: Requirements 25.4, 25.5, 25.6**"""
    rec = LatencyRecorder(60, now=lambda: 0.0, reservoir=reservoir)
    samples = sorted(samples)
    for t, ms in samples:
        rec.record(Stage.REASON, ms, at=t)
    now = max([query_at] + [t for t, _ in samples])
    windowed = [ms for t, ms in samples if t >= now - 60]
    for summary, pool in ((rec.window(Stage.REASON, now), windowed),
                          (rec.all_time(Stage.REASON), [ms for _, ms in samples])):
        d = summary.to_dict()
        assert d["count"] == len(pool)
        if not pool:
            assert all(d[k] == UNAVAILABLE for k in ("p50", "p95", "p99", "min", "max"))
        else:
            assert min(pool) <= d["p50"] <= d["p95"] <= d["p99"] <= max(pool)
            assert d["p50"] in pool and d["p99"] in pool
            assert (d["min"], d["max"]) == (min(pool), max(pool))
    for s in Stage:  # stages with no samples are unavailable, never 0 ms
        if s is not Stage.REASON:
            assert rec.window(s, now).to_dict()["p50"] == UNAVAILABLE


# --- Property 42 ----------------------------------------------------------------------------


# Feature: helix-fraud-immune-system, Property 42: At most one open Case per event
@settings(max_examples=40)
@given(specs=event_specs)
def test_p42_at_most_one_open_case_per_event_end_to_end(specs):
    """**Validates: Requirements 12.3, 12.6**"""
    h = fixed_helix(motifs=True)
    for ev in to_events(specs):
        h.process(ev)
        owners: dict[str, str] = {}
        for c in h.cases.open_cases():
            for t in c.tx_ids:
                assert t not in owners, f"{t} in {owners.get(t)} and {c.case_id}"
                owners[t] = c.case_id
        assert owners == h.cases.tx_to_case
        for cid in owners.values():  # every open Case has an Evidence_Chain covering its events
            assert set(h.chain(cid).tx_ids) >= h.cases.get(cid).tx_ids


# --- Property 28 ----------------------------------------------------------------------------


# Feature: helix-fraud-immune-system, Property 28: Alert routing
@settings(max_examples=40)
@given(specs=event_specs)
def test_p28_alert_routing_through_pipeline(specs):
    """**Validates: Requirements 13.4, 13.5, 13.6**"""
    h = fixed_helix(motifs=False)  # routing only: no Motif grouping in between
    thr = h.cfg.risk.alert_threshold
    for ev in to_events(specs):
        before = {c.case_id: (c.updated_at, c.update_no, frozenset(c.tx_ids), ev.src in c.accounts)
                  for c in h.cases.open_cases()}
        o = h.process(ev)
        after = {c.case_id: frozenset(c.tx_ids) for c in h.cases.open_cases()}
        if o.risk < thr:
            assert after == {k: v[2] for k, v in before.items()} and o.case_id is None
            continue
        hits = [(v[0], v[1], k) for k, v in before.items() if v[3]]
        if hits:
            expected = max(hits)[2]
            assert o.case_id == expected and after[expected] == before[expected][2] | {ev.tx_id}
        else:
            assert o.case_id not in before and after[o.case_id] == {ev.tx_id}


# --- Property 24 ----------------------------------------------------------------------------

coords = st.tuples(st.floats(-60, 60), st.floats(-170, 170))
full_events = st.lists(
    st.tuples(st.integers(1, 3), st.integers(1, 600), st.integers(1, 5000), coords,
              st.sampled_from(["web", "pos", "atm"])),
    min_size=0, max_size=8,
)


def full_event(i: int, ts: datetime, dst: int, amount: int, ll, channel: str) -> TransactionEvent:
    return TransactionEvent(tx_id=f"T{i:03d}", ts=ts, src="A", dst=f"D{dst}", amount=Decimal(f"{amount}.00"),
                            currency="EUR", channel=channel, mcc="5411", lat=ll[0], lon=ll[1],
                            device_id="dev1", session_id=f"s{i}")


# Feature: helix-fraud-immune-system, Property 24: Removing optional fields never lowers uncertainty
@settings(max_examples=40)
@given(history=full_events, final=full_events.filter(bool),
       removed=st.sets(st.sampled_from(OPTIONAL_FIELDS), min_size=1))
def test_p24_removing_optional_fields_never_lowers_stage1_uncertainty(history, final, removed):
    """**Validates: Requirements 7.5**"""
    cfg = HelixConfig(sensors=SensorConfig(min_history_count=2))
    a = Helix(cfg, ManualClock(T0), sensor_clock=const_clock)
    b = Helix(cfg, ManualClock(T0), sensor_clock=const_clock)
    t = T0
    for i, (dst, gap, amount, ll, ch) in enumerate(history):
        t += timedelta(minutes=gap)
        ev = full_event(i, t, dst, amount, ll, ch)
        a.process(ev)
        b.process(ev)
    dst, gap, amount, ll, ch = final[0]
    full = full_event(99, t + timedelta(minutes=gap), dst, amount, ll, ch)
    reduced = full.model_copy(update={f: None for f in removed})
    fa_full, fa_red = a.process(full).stage1, b.process(reduced).stage1
    if not (fa_full.high_conflict or fa_red.high_conflict):
        assert fa_red.uncertainty >= fa_full.uncertainty - 1e-9

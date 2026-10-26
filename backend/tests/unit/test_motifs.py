"""Unit tests for helix.motifs (design §10, Requirement 11)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from time import perf_counter

import pytest

from helix.codec import parse
from helix.codec.codec import CodecError
from helix.fusion import ContributorType, Mass
from helix.graph import Edge, TemporalGraph
from helix.motifs import MotifEngine, MotifType

T0 = datetime(2024, 6, 1, tzinfo=timezone.utc)
SAMPLE = Path(__file__).resolve().parents[3] / "docs" / "sample_transactions.jsonl"


@dataclass(frozen=True)
class Ev:
    tx_id: str
    ts: datetime
    src: str
    dst: str
    amount: Decimal
    currency: str = "EUR"


def ev(tx: str, minutes: float, src: str, dst: str, amount: str | int = "100") -> Ev:
    return Ev(tx, T0 + timedelta(minutes=minutes), src, dst, Decimal(str(amount)))


class Harness:
    def __init__(self, **kw) -> None:
        self.g = TemporalGraph()
        self.engine = MotifEngine(budget_ms=1000, **kw)
        self.emitted = []

    def feed(self, *events: Ev):
        results = []
        for e in events:
            if self.g.add(e):
                r = self.engine.on_event(e, self.g)
                self.emitted.extend(r.instances)
                results.append(r)
        return results

    def of(self, t: MotifType) -> list[frozenset[str]]:
        return [i.tx_ids for i in self.emitted if i.motif_type == t]


# ---------------------------------------------------------------- sample data


def test_sample_file_yields_structuring_and_mule_chain():
    h = Harness()
    for line in SAMPLE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        parsed = parse(line)
        if isinstance(parsed, CodecError):
            continue
        if h.g.add(parsed):
            h.emitted.extend(h.engine.on_event(parsed, h.g).instances)

    structuring = h.of(MotifType.STRUCTURING)
    assert frozenset({"T010", "T011", "T012"}) in structuring
    inst = next(i for i in h.emitted if i.motif_type == MotifType.STRUCTURING)
    assert inst.detail["reporting_threshold"] == "10000"
    assert inst.detail["amounts"] == ("9800.00", "9650.00", "9900.00")

    mule = h.of(MotifType.MULE_CHAIN)
    assert frozenset({"T013", "T014", "T015"}) in mule
    full = next(i for i in h.emitted if i.tx_ids == frozenset({"T013", "T014", "T015"}))
    assert full.accounts == ("ACC-CARL", "ACC-DAVE", "ACC-ERIN", "ACC-FINN")
    assert all(r >= 0.8 for r in full.detail["forwarding_ratios"])
    assert all(d <= 3600 for d in full.detail["hop_delays_s"])
    assert not h.of(MotifType.ROUND_TRIP) and not h.of(MotifType.FAN_OUT) and not h.of(MotifType.FAN_IN)


# ---------------------------------------------------------------- round trip


def test_cycle_closed_by_newest_edge():
    h = Harness()
    h.feed(ev("a", 0, "A", "B"), ev("b", 60, "B", "C"), ev("c", 120, "C", "A"))
    [inst] = [i for i in h.emitted if i.motif_type == MotifType.ROUND_TRIP]
    assert inst.tx_ids == frozenset("abc")
    assert inst.accounts == ("A", "B", "C")
    assert inst.detail["tx_ids_in_order"] == ("a", "b", "c")
    assert inst.duration_s == 7200


def test_late_middle_edge_joins_forward_and_backward():
    h = Harness()
    h.feed(ev("a", 0, "A", "B"), ev("c", 120, "C", "D"), ev("d", 180, "D", "A"))
    assert not h.of(MotifType.ROUND_TRIP)
    h.feed(ev("b", 60, "B", "C"))  # late edge in the middle of the cycle
    assert h.of(MotifType.ROUND_TRIP) == [frozenset("abcd")]


def test_cycle_rejects_long_span_and_non_increasing_ts():
    h = Harness()
    h.feed(ev("a", 0, "A", "B"), ev("b", 72 * 60 + 1, "B", "A"))
    h.feed(ev("c", 0, "X", "Y"), ev("d", 0, "Y", "X"))  # equal timestamps
    assert not h.of(MotifType.ROUND_TRIP)
    h.feed(ev("e", 72 * 60, "B", "A"))  # exactly 72 h is allowed
    assert h.of(MotifType.ROUND_TRIP) == [frozenset("ae")]


def test_cycle_max_six_accounts():
    h = Harness()
    accts = "ABCDEFG"
    h.feed(*[ev(f"t{i}", i, accts[i], accts[i + 1]) for i in range(6)], ev("close", 10, "G", "A"))
    assert not h.of(MotifType.ROUND_TRIP)  # 7 accounts


# ---------------------------------------------------------------- fan / structuring


def test_fan_out_needs_eight_distinct_within_24h():
    h = Harness()
    h.feed(*[ev(f"o{i}", i * 60, "HUB", f"D{i}") for i in range(7)], ev("dup", 500, "HUB", "D1"))
    assert not h.of(MotifType.FAN_OUT)
    h.feed(ev("late", 24 * 60 + 1, "HUB", "D7"))  # 24h01m after o0: window drops o0
    assert not h.of(MotifType.FAN_OUT)
    h.feed(ev("o7", 23 * 60, "HUB", "D7"))
    [inst] = [i for i in h.emitted if i.motif_type == MotifType.FAN_OUT]
    assert {f"o{i}" for i in range(8)} <= inst.tx_ids
    assert inst.detail["hub"] == "HUB" and len(inst.detail["counterparties"]) == 8
    assert inst.duration_s <= 24 * 3600


def test_fan_in():
    h = Harness()
    h.feed(*[ev(f"i{i}", i, f"S{i}", "HUB") for i in range(8)])
    assert h.of(MotifType.FAN_IN) == [frozenset(f"i{i}" for i in range(8))]


def test_structuring_band_edges():
    h = Harness(reporting_threshold=10_000)
    h.feed(ev("s1", 0, "B", "X", "9000.00"), ev("s2", 60, "B", "Y", "10000.00"), ev("s3", 120, "B", "Z", "9999.99"))
    assert not h.of(MotifType.STRUCTURING)  # 10000 is not below RT
    h.feed(ev("s4", 48 * 60, "B", "W", "9500"))
    assert h.of(MotifType.STRUCTURING) == [frozenset({"s1", "s3", "s4"})]


# ---------------------------------------------------------------- mule chain


def test_mule_chain_ratio_and_delay():
    h = Harness()
    h.feed(ev("m1", 0, "A", "B", 1000), ev("m2", 61, "B", "C", 900))  # delay > 60 min
    h.feed(ev("n1", 0, "P", "Q", 1000), ev("n2", 10, "Q", "R", 799))  # ratio < 0.8
    assert not h.of(MotifType.MULE_CHAIN)
    h.feed(ev("k1", 0, "X", "Y", 1000), ev("k2", 60, "Y", "Z", 800))
    assert h.of(MotifType.MULE_CHAIN) == [frozenset({"k1", "k2"})]


def test_mule_chain_late_first_hop_extends_forward():
    h = Harness()
    h.feed(ev("b", 20, "B", "C", 900), ev("c", 40, "C", "D", 850))
    h.feed(ev("a", 0, "A", "B", 1000))
    assert set(h.of(MotifType.MULE_CHAIN)) == {frozenset("bc"), frozenset("ab"), frozenset("abc")}


# ---------------------------------------------------------------- engine


def test_dedup_and_replay():
    h = Harness()
    h.feed(ev("a", 0, "A", "B"), ev("b", 60, "B", "A"))
    assert len(h.emitted) == 1
    again = h.engine.on_event(ev("b", 60, "B", "A"), h.g)  # replayed edge already in graph
    assert again.instances == ()
    forged = h.engine.on_event(ev("zz", 60, "B", "A"), h.g)  # never added to the graph
    assert forged.instances == ()


def test_contributions_mass_and_shape():
    h = Harness(masses={MotifType.MULE_CHAIN: 0.9})
    [_, r] = h.feed(ev("a", 0, "A", "B", 1000), ev("b", 5, "B", "C", 950))
    assert set(r.contributions) == {"a", "b"}
    [c] = r.contributions["a"]
    assert c.kind == ContributorType.MOTIF and c.contributor_id == "motif:MULE_CHAIN"
    assert c.mass == Mass(0.9, 0.0, pytest.approx(0.1))
    assert c.mass.l == 0.0


def test_invalid_mass_and_budget_rejected():
    with pytest.raises(ValueError):
        MotifEngine(masses={MotifType.FAN_IN: 0.0})
    with pytest.raises(ValueError):
        MotifEngine(masses={MotifType.FAN_IN: 1.5})
    with pytest.raises(ValueError):
        MotifEngine(budget_ms=0)


def test_budget_truncation_keeps_edge_and_reports_types():
    h = Harness()
    h.feed(ev("a", 0, "A", "B"))
    e = ev("b", 60, "B", "A")
    assert h.g.add(e)
    r = h.engine.on_edge(Edge(e.tx_id, e.src, e.dst, e.ts, e.amount, e.currency), h.g, deadline=perf_counter() - 1)
    assert r.instances == () and r.truncated == frozenset(MotifType)
    assert "b" in h.g  # edge retained


def test_from_config():
    from helix.config.schema import GraphConfig

    eng = MotifEngine.from_config(GraphConfig(motif_budget_ms=5, reporting_threshold=5000))
    assert eng.budget_s == pytest.approx(0.005)
    s = next(d for d in eng.detectors if d.motif_type == MotifType.STRUCTURING)
    assert s.rt == Decimal("5000")


# ---------------------------------------------------------------- config-driven shapes (task 29.1)


def _cfg_harness(**graph) -> Harness:
    from helix.config.schema import GraphConfig

    h = Harness()
    h.engine = MotifEngine.from_config(GraphConfig(motif_budget_ms=1000, **graph))
    return h


def test_from_config_defaults_match_built_in_detectors():
    from helix.config.schema import GraphConfig
    from helix.motifs.detectors import FAN_MIN_COUNTERPARTIES, FAN_SPAN, ROUND_TRIP_MAX_ACCOUNTS, ROUND_TRIP_SPAN

    eng = MotifEngine.from_config(GraphConfig())
    by = {d.motif_type: d for d in eng.detectors}
    assert [d.motif_type for d in eng.detectors] == [d.motif_type for d in MotifEngine().detectors]
    assert by[MotifType.FAN_OUT].k == by[MotifType.FAN_IN].k == FAN_MIN_COUNTERPARTIES
    assert by[MotifType.FAN_OUT].span == by[MotifType.FAN_IN].span == FAN_SPAN
    assert by[MotifType.ROUND_TRIP].max_edges == ROUND_TRIP_MAX_ACCOUNTS
    assert by[MotifType.ROUND_TRIP].span == ROUND_TRIP_SPAN


def test_from_config_fan_shape_spread_over_days():
    # A 4-counterparty fan-out spread over 3 days: invisible to the defaults (8 in 24 h),
    # found with fan_min_counterparties=4 and a 4-day fan_span_s.
    evs = [ev(f"f{i}", i * 18 * 60, "HUB", f"D{i}") for i in range(4)]
    base = _cfg_harness()
    base.feed(*evs)
    assert base.of(MotifType.FAN_OUT) == []
    wide = _cfg_harness(fan_min_counterparties=4, fan_span_s=4 * 86_400)
    wide.feed(*evs)
    assert wide.of(MotifType.FAN_OUT) == [frozenset(e.tx_id for e in evs)]


def test_from_config_round_trip_length_and_span():
    # 8-account cycle over 77 h: beyond the default 6 accounts / 72 h.
    accts = [f"C{i}" for i in range(8)]
    evs = [ev(f"c{i}", i * 11 * 60 + 60, accts[i], accts[(i + 1) % 8]) for i in range(8)]
    base = _cfg_harness()
    base.feed(*evs)
    assert base.of(MotifType.ROUND_TRIP) == []
    wide = _cfg_harness(round_trip_max_accounts=10, round_trip_span_s=4 * 86_400)
    wide.feed(*evs)
    assert wide.of(MotifType.ROUND_TRIP) == [frozenset(e.tx_id for e in evs)]


def test_config_rejects_motif_span_beyond_graph_window():
    from helix.config.schema import GraphConfig, HelixConfig

    cfg = HelixConfig(graph=GraphConfig(window_s=86_400, fan_span_s=2 * 86_400, round_trip_span_s=3 * 86_400))
    paths = {e.path for e in cfg.validate()}
    assert {"graph.fan_span_s", "graph.round_trip_span_s"} <= paths
    assert HelixConfig(graph=GraphConfig(fan_min_counterparties=1)).validate()  # below range
    assert not HelixConfig(graph=GraphConfig(fan_min_counterparties=4, fan_span_s=4 * 86_400)).validate()


from hypothesis import given, settings, strategies as st  # noqa: E402


@settings(max_examples=40, deadline=None)
@given(
    k=st.integers(2, 6),
    span_h=st.integers(1, 96),
    gaps=st.lists(st.integers(0, 20 * 60), min_size=1, max_size=8),  # total < 7-day Graph_Window
)
def test_property_fan_out_respects_configured_k_and_span(k, span_h, gaps):
    """Every FAN_OUT instance from a config-built engine has >= k distinct counterparties,
    all within fan_span_s; and one is found iff some span window holds k counterparties.

    **Validates: Requirements 11**
    """
    h = _cfg_harness(fan_min_counterparties=k, fan_span_s=span_h * 3600)
    t, evs = 0, []
    for i, gap in enumerate(gaps):
        t += gap
        evs.append(ev(f"x{i}", t, "HUB", f"D{i}"))
    h.feed(*evs)
    span = timedelta(hours=span_h)
    for inst in (i for i in h.emitted if i.motif_type == MotifType.FAN_OUT):
        assert len(set(inst.accounts[1:])) >= k
        assert inst.ts_end - inst.ts_start <= span
    ts = sorted(e.ts for e in evs)
    expect = any(sum(1 for u in ts if s <= u <= s + span) >= k for s in ts)
    assert bool(h.of(MotifType.FAN_OUT)) == expect

"""Property tests for helix.explain (design Properties 49-53)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from helix.cases.manager import stage2_motifs
from helix.cases.models import Case, CaseStatus, Placement
from helix.config import DEFAULT_BANDS
from helix.explain import (
    CONTRIBUTOR_TYPES,
    CaseHeader,
    EntryKind,
    EvidenceChain,
    EvidenceChainError,
    EvidenceEntry,
    ExplanationEngine,
    dumps,
    explain,
    loads,
)
from helix.fusion import VACUOUS, Contribution, ContributorType, Mass, fuse
from helix.motifs import MotifInstance, MotifType, contributor_id
from helix.risk import assess_case, tier

T0 = datetime(2024, 6, 1, tzinfo=timezone.utc)
SENSORS = ("amount", "counterparty", "drift", "geo", "timing", "velocity")
unit = st.floats(min_value=0.0, max_value=1.0, allow_nan=False)
thresholds = st.one_of(st.sampled_from([0.95, 1.0, 0.5]), st.floats(min_value=0.0, max_value=1.0, exclude_min=True))


@st.composite
def masses(draw: st.DrawFn) -> Mass:
    if draw(st.booleans()):
        return draw(st.sampled_from([VACUOUS, Mass(1.0, 0.0, 0.0), Mass(0.0, 1.0, 0.0), Mass(0.5, 0.5, 0.0),
                                     Mass(0.3, 0.0, 0.7), Mass(0.0, 0.3, 0.7), Mass(0.2, 0.2, 0.6)]))
    a, b = sorted((draw(unit), draw(unit)))
    return Mass(a, b - a, 1.0 - b)


weights = st.one_of(st.sampled_from([0.0, 0.5, 1.0]), unit)
offsets = st.integers(-14 * 60, 14 * 60).map(lambda m: timezone(timedelta(minutes=m)))
stamps = st.builds(lambda s, us, tz: (T0 + timedelta(seconds=s, microseconds=us)).astimezone(tz),
                   st.integers(0, 3), st.integers(0, 999_999), offsets)
scalars = st.one_of(
    st.none(), st.booleans(), st.integers(-(2**70), 2**70), st.text(max_size=8),
    st.floats(allow_nan=False, allow_infinity=False), st.sampled_from([-0.0, 5e-324, 1e308, 0.1]),
    st.decimals(allow_nan=False, allow_infinity=False, places=None),
)
feature_maps = st.dictionaries(st.text(min_size=1, max_size=6), scalars, max_size=4)


@st.composite
def entries(draw: st.DrawFn, contributor: bool | None = None) -> EvidenceEntry:
    kinds = list(EntryKind)
    if contributor is True:
        kinds = [EntryKind.CONTRIBUTOR, EntryKind.MOTIF]
    kind = draw(st.sampled_from(kinds))
    needs = kind in (EntryKind.CONTRIBUTOR, EntryKind.MOTIF)
    ctype = draw(st.sampled_from(CONTRIBUTOR_TYPES)) if needs else draw(st.sampled_from([None, "SENSOR"]))
    mass = draw(masses()) if needs or kind is EntryKind.FUSED else draw(st.none() | masses())
    weight = draw(weights) if needs else draw(st.none() | weights)
    cid = draw(st.sampled_from(SENSORS + ("ab-1", "rule-7", "MULE_CHAIN:x", "~fused")))
    return EvidenceEntry(draw(stamps), draw(st.sampled_from(["T1", "T2", "T3"])), cid, kind, ctype, mass, weight,
                         draw(feature_maps), draw(feature_maps))


def unique_entries(min_size: int = 0, contributor: bool | None = None):
    return st.lists(entries(contributor), min_size=min_size, max_size=12, unique_by=lambda e: e.key)


@st.composite
def chain_and_perms(draw: st.DrawFn, min_size: int = 0, contributor: bool | None = None):
    es = draw(unique_entries(min_size, contributor))
    return es, draw(st.permutations(es)), draw(st.permutations(es))


# --- Property 49 ----------------------------------------------------------------------


@st.composite
def cases(draw: st.DrawFn):
    hc = draw(thresholds)
    txs = [f"T{i}" for i in range(draw(st.integers(1, 4)))]
    assessments, ts = {}, {}
    for i, tx in enumerate(txs):
        names = draw(st.lists(st.sampled_from(SENSORS), unique=True, max_size=6))
        cs = [Contribution(n, ContributorType.SENSOR, draw(masses()), draw(weights)) for n in names]
        assessments[tx] = fuse(cs, hc, tx_id=tx)[0]
        ts[tx] = T0 + timedelta(minutes=draw(st.integers(0, 10)))
    motifs = {}
    for j in range(draw(st.integers(0, 2))):
        mt = draw(st.sampled_from(list(MotifType)))
        mtx = frozenset(draw(st.lists(st.sampled_from(txs), min_size=1, unique=True)))
        mid = f"{mt.value}:{j}"
        motifs[mid] = MotifInstance(mid, mt, mtx, ("A", "B"), T0, T0, draw(masses()), draw(st.booleans()))
    mw = draw(weights)
    mc = [Contribution(contributor_id(m.motif_type), ContributorType.MOTIF, m.mass, mw)
          for _, m in stage2_motifs(motifs)]  # as CaseManager: one instance per MotifType
    cr = assess_case(list(assessments.values()), mc, hc)
    case = Case(
        case_id="C-1", seq_no=1, status=CaseStatus.OPEN, tier=tier(cr.score, DEFAULT_BANDS), risk=cr.score,
        risk_at_creation=cr.score, expected_loss=Decimal(0), priority=Decimal(0), accounts={"A"},
        tx_ids=set(txs), assignee=None, created_at=T0, updated_at=T0, admitted_period=None,
        placement=Placement.ACTIVE, merged_into=None, label=None, label_note=None, escalated=False, history=[],
        assessments=assessments, amounts={t: Decimal("10.00") for t in txs}, motifs=motifs, case_risk=cr,
    )
    return case, ts, hc


def _close(a: Mass, b: Mass) -> bool:
    return all(abs(x - y) <= 1e-9 for x, y in ((a.f, b.f), (a.l, b.l), (a.t, b.t)))


# Feature: helix-fraud-immune-system, Property 49: Evidence chain reproducibility
@pytest.mark.property
@given(c=cases())
def test_chain_reproduces_fused_assessments(c) -> None:
    """**Validates: Requirements 15.6**"""
    case, ts, hc = c
    chain = EvidenceChain.build(case, None, ts, high_conflict=hc)
    assert chain.verify() == []
    assert loads(dumps(chain)).verify() == []
    re = chain.recompute()
    for tx, fa in case.assessments.items():
        got = re[("~fused", tx)]
        assert _close(got.mass, fa.mass) and abs(got.conflict - fa.conflict) <= 1e-9
        assert got.high_conflict == fa.high_conflict
    s2 = case.case_risk.stage2
    stage2 = [v for k, v in re.items() if k[0].startswith("~stage2:")]
    assert len(stage2) == (0 if s2 is None else 1)
    if s2 is not None:
        assert _close(stage2[0].mass, s2.mass) and abs(stage2[0].conflict - s2.conflict) <= 1e-9


# --- Property 50 ----------------------------------------------------------------------


# Feature: helix-fraud-immune-system, Property 50: Evidence chain JSON round-trip and ordering
@pytest.mark.property
@given(data=chain_and_perms())
def test_chain_round_trip_and_ordering(data) -> None:
    """**Validates: Requirements 15.5, 15.7**"""
    _, p1, p2 = data
    c1, c2 = EvidenceChain.of("C-1", p1), EvidenceChain.of("C-1", p2)
    order = [e.order for e in c1]
    assert order == sorted(order)
    assert list(c1) == list(c2) and dumps(c1) == dumps(c2)
    back = loads(dumps(c1))
    assert back == c1
    assert list(back) == list(c1)  # same order, field by field
    assert dumps(back) == dumps(c1)  # exact float reprs (incl. -0.0) and Decimal strings
    for a, b in zip(back, c1):
        for k, v in b.features.items():
            assert type(a.features[k]) is type(v) and repr(a.features[k]) == repr(v)


# --- Property 51 ----------------------------------------------------------------------

_ENTRY_FIELDS = ("ts", "tx_id", "contributor_id", "kind", "ctype", "mass", "weight", "features", "detail")
_PLACEHOLDER = "__NONFINITE__"


@st.composite
def mutated(draw: st.DrawFn) -> tuple[str, str]:
    """(mutated JSON, expected offending field path) from a valid chain."""
    chain = EvidenceChain.of("C-1", draw(unique_entries(min_size=1, contributor=True)))
    text = dumps(chain)
    doc = json.loads(text)
    massed = [i for i, e in enumerate(doc["entries"]) if e["mass"] is not None]
    how = draw(st.sampled_from(["malformed", "remove_entry_field", "remove_top_field", "non_finite",
                                "invalid_mass"]))
    if how == "malformed":
        return text[: draw(st.integers(0, len(text) - 1))], "$"
    if how == "remove_top_field":
        name = draw(st.sampled_from(["schema", "case_id", "entries"]))
        del doc[name]
        return json.dumps(doc), name
    if how == "remove_entry_field":
        i = draw(st.integers(0, len(doc["entries"]) - 1))
        name = draw(st.sampled_from(_ENTRY_FIELDS))
        del doc["entries"][i][name]
        return json.dumps(doc), f"entries[{i}].{name}"
    i = draw(st.sampled_from(massed))
    comp = draw(st.sampled_from(["f", "l", "t"]))
    if how == "non_finite":
        doc["entries"][i]["mass"][comp] = _PLACEHOLDER
        token = draw(st.sampled_from(["NaN", "Infinity", "-Infinity", "1e999"]))
        return json.dumps(doc).replace(f'"{_PLACEHOLDER}"', token), f"entries[{i}].mass.{comp}"
    m = doc["entries"][i]["mass"]
    m[comp] = m[comp] + draw(st.floats(min_value=1e-6, max_value=1.0))  # breaks fsum == 1
    return json.dumps(doc), f"entries[{i}].mass"


# Feature: helix-fraud-immune-system, Property 51: Malformed evidence chains are rejected
@pytest.mark.property
@given(m=mutated())
def test_malformed_chains_rejected_naming_field(m: tuple[str, str]) -> None:
    """**Validates: Requirements 15.8**"""
    text, field = m
    result = None
    with pytest.raises(EvidenceChainError) as ei:
        result = loads(text)
    assert result is None  # no partial chain
    assert ei.value.field == field


# --- Property 52 ----------------------------------------------------------------------

HEADER = CaseHeader("C-1", "HIGH", 0.8, 0.7, 0.9, 0.2, 0.1, (("EUR", Decimal("12.345")),))


# Feature: helix-fraud-immune-system, Property 52: Explanation determinism
@pytest.mark.property
@given(data=chain_and_perms())
def test_explanation_is_deterministic(data) -> None:
    """**Validates: Requirements 16.2**"""
    _, p1, p2 = data
    c1, c2 = EvidenceChain.of("C-1", p1), EvidenceChain.of("C-1", p2)
    e1 = ExplanationEngine().explain(HEADER, c1)
    e2 = ExplanationEngine().explain(HEADER, c2)  # rebuilt engine, other insertion order
    e3 = explain(HEADER, loads(dumps(c1)))  # as after a restart (reloaded chain)
    assert e1 == e2 == e3
    assert e1.text.encode() == e2.text.encode() == e3.text.encode()


# --- Property 53 ----------------------------------------------------------------------


# Feature: helix-fraud-immune-system, Property 53: Explanation partition and ordering
@pytest.mark.property
@given(es=unique_entries())
def test_explanation_partition_and_ordering(es: list[EvidenceEntry]) -> None:
    """**Validates: Requirements 16.1, 16.3, 16.4, 16.5, 16.7, 16.8, 16.9**"""
    chain = EvidenceChain.of("C-1", es)
    ex = explain(HEADER, chain)
    groups: dict[str, list[EvidenceEntry]] = {}
    for e in chain:
        if e.kind in (EntryKind.CONTRIBUTOR, EntryKind.MOTIF):
            groups.setdefault(e.contributor_id, []).append(e)
    expected = {}
    for name, g in groups.items():
        F = max(e.weight * e.mass.f for e in g)
        L = max(e.weight * e.mass.l for e in g)
        expected[name] = ("r" if F > L else "m" if L > F else "i", F, L, g)

    buckets = {"r": ex.reasons, "m": ex.mitigating, "i": ex.insufficient}
    names = [it.name for items in buckets.values() for it in items]
    assert sorted(names) == sorted(groups)  # each contributor exactly once
    for b, items in buckets.items():
        for it in items:
            eb, F, L, g = expected[it.name]
            assert eb == b and it.f == F and it.l == L
            assert it.tx_ids == tuple(dict.fromkeys(e.tx_id for e in g))
            assert it.weight in [e.weight for e in g]
            assert it.comparison and it.label and it.name in groups
    assert [(-i.f, i.name) for i in ex.reasons] == sorted((-i.f, i.name) for i in ex.reasons)
    assert [(-i.l, i.name) for i in ex.mitigating] == sorted((-i.l, i.name) for i in ex.mitigating)
    assert [i.name for i in ex.insufficient] == sorted(i.name for i in ex.insufficient)
    assert (ex.message is None) == bool(groups)

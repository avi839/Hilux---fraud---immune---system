"""Hypothesis properties of the technique mapping and the Detection_Rule codec (Req 30.4, 31.8)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from helix.explain import EntryKind, EvidenceChain, EvidenceEntry
from helix.fusion import Mass
from helix.soc.rules import (
    AUTH_FIELDS,
    FIELDS,
    MODIFIERS,
    DetectionRule,
    Item,
    RuleStatus,
    RuleTarget,
    Selection,
    format_rule,
    parse_rule,
)
from helix.soc.techniques import case_techniques, default_catalogue, default_mapping

pytestmark = pytest.mark.property

T0 = datetime(2024, 6, 1, tzinfo=timezone.utc)
CAT = default_catalogue()
MP = default_mapping()
IDS = sorted(CAT.ids)
SENSORS = ["amount", "velocity", "timing", "geo", "counterparty", "drift"]
SIGNALS = ["failed_logins", "mfa_failure", "password_reset", "new_device", "new_payee"]
MOTIFS = ["ROUND_TRIP_CYCLE", "FAN_OUT", "FAN_IN", "STRUCTURING", "MULE_CHAIN"]
TEXT = st.characters(min_codepoint=32, max_codepoint=126) | st.sampled_from("äöüßéñ€")


# --- Req 30.4: Case tags = union over the Evidence_Chain contributors ------------------------

unit = st.floats(min_value=0.0, max_value=1.0, allow_nan=False)


@st.composite
def entry_with_expected(draw, n: int):
    """An Evidence_Chain entry and the technique ids the contributor mapping says it carries."""
    tx = f"T{draw(st.integers(0, 5))}"
    ts = T0 + timedelta(minutes=draw(st.integers(0, 600)))
    kind = draw(st.sampled_from(["sensor", "auth", "motif", "rule", "antibody", "record"]))
    f = draw(unit)
    w = draw(st.sampled_from([0.0, 0.5, 1.0]))
    m = Mass(f, 0.0, 1.0 - f)
    if kind == "sensor":
        name = draw(st.sampled_from(SENSORS))
        e = EvidenceEntry(ts, tx, name, EntryKind.CONTRIBUTOR, "SENSOR", m, w, {})
        exp = set(MP.techniques(f"sensor.{name}")) if w * f > 0 else set()
    elif kind == "auth":
        sig = draw(st.lists(st.sampled_from(SIGNALS), min_size=1, max_size=3, unique=True))
        e = EvidenceEntry(ts, tx, "auth", EntryKind.CONTRIBUTOR, "SENSOR", m, w, {"signals": "|".join(sig)})
        exp = {t for s in sig for t in MP.techniques(f"auth.{s}")} if w * f > 0 else set()
    elif kind == "motif":
        mt = draw(st.sampled_from(MOTIFS))
        e = EvidenceEntry(ts, tx, f"M-{n}", EntryKind.MOTIF, "MOTIF", Mass(max(f, 0.1), 0.0, 1.0 - max(f, 0.1)),
                          1.0, {"motif_type": mt})
        exp = set(MP.techniques(f"motif.{mt}"))
    elif kind == "rule":
        techs = draw(st.lists(st.sampled_from(IDS), min_size=1, max_size=4, unique=True))
        e = EvidenceEntry(ts, tx, f"rule:R{n}", EntryKind.CONTRIBUTOR, "RULE", m, w,
                          {"rule_id": f"R{n}", "version": 1, "techniques": "|".join(techs)})
        exp = set(techs)
    elif kind == "antibody":
        tags = draw(st.lists(st.sampled_from([*IDS, "UNATTRIBUTED"]), min_size=1, max_size=3, unique=True))
        e = EvidenceEntry(ts, tx, f"antibody:AB{n}", EntryKind.CONTRIBUTOR, "ANTIBODY", m, w,
                          {"antibody_id": f"AB{n}", "tags": ",".join(tags)})
        known = {t for t in tags if t in CAT}
        exp = known or {"HX-T0099"}
    else:
        e = EvidenceEntry(ts, tx, f"~late{n}", EntryKind.LATE, detail={"lateness_s": 1.0, "received_at": "x"})
        exp = set()
    return e, exp


@st.composite
def chains(draw, offset: int = 0):
    k = draw(st.integers(0, 12))
    pairs = [draw(entry_with_expected(offset + n)) for n in range(k)]
    uniq = {e.key: (e, x) for e, x in pairs}  # one entry per (contributor, tx), last wins
    return EvidenceChain.of("C-1", [e for e, _ in uniq.values()]), {key: x for key, (_, x) in uniq.items()}


@settings(max_examples=60, deadline=None)
@given(a=chains(), b=chains(offset=100))
def test_case_technique_tags_equal_union_over_chain_contributors(a, b):
    """For all Cases, the Case's HELIX_Technique tags equal the union of the technique ids mapped to
    the contributors in its Evidence_Chain, and stay so when chains are merged.

    **Validates: Requirements 30.4**
    """
    chain, expected = a
    tags = case_techniques(chain)
    assert set(tags) == set().union(*expected.values())
    assert list(tags) == sorted(set(tags)) and set(tags) <= CAT.ids
    other, exp_b = b
    merged = chain.merge(other)  # the survivor's entry wins on a shared (contributor, tx)
    exp_merged = set().union(*expected.values(), *(x for k, x in exp_b.items() if k not in expected))
    assert set(case_techniques(merged)) == exp_merged


# --- Req 31.8: Detection_Rule YAML round-trip -------------------------------------------------

ident = st.from_regex(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,20}", fullmatch=True)
sel_name = st.from_regex(r"[A-Za-z_][A-Za-z0-9_]{0,12}", fullmatch=True).filter(
    lambda s: s not in ("and", "or", "not", "condition"))
text = st.text(TEXT, min_size=1, max_size=40).filter(lambda s: s.strip() != "")
num = st.integers(-10**9, 10**9) | st.floats(-1e12, 1e12, allow_nan=False, allow_infinity=False)


@st.composite
def item(draw, target: RuleTarget):
    fld = draw(st.sampled_from(sorted(FIELDS[target])))
    spec = FIELDS[target][fld]
    mod = draw(st.sampled_from(sorted(MODIFIERS[spec.kind])))
    if mod == "exists":
        return Item(fld, mod, (draw(st.booleans()),))
    if spec.kind == "number":
        v = num
    elif spec.kind == "bool":
        v = st.booleans()
    elif spec.kind == "enum":
        v = st.sampled_from(AUTH_FIELDS[fld].choices)
    else:
        v = st.text(TEXT, max_size=20)
    return Item(fld, mod, tuple(draw(st.lists(v, min_size=1, max_size=3))))


@st.composite
def condition(draw, names: list[str], depth: int = 0):
    if depth >= 3 or draw(st.booleans()):
        return draw(st.sampled_from(names))
    op = draw(st.sampled_from(["and", "or", "not", "paren"]))
    if op == "not":
        return "not " + draw(condition(names, depth + 1))
    if op == "paren":
        return "(" + draw(condition(names, depth + 1)) + ")"
    return f"{draw(condition(names, depth + 1))} {op} {draw(condition(names, depth + 1))}"


@st.composite
def rules(draw):
    target = draw(st.sampled_from(list(RuleTarget)))
    names = draw(st.lists(sel_name, min_size=1, max_size=3, unique=True))
    sels = []
    for nm in names:
        items = draw(st.lists(item(target), min_size=1, max_size=4, unique_by=lambda i: i.key))
        sels.append(Selection(nm, tuple(items)))
    f = draw(st.floats(0.01, 1.0))
    legit = draw(st.floats(0.0, 1.0 - f)) if f < 1.0 else 0.0
    return DetectionRule(
        rule_id=draw(ident), title=draw(text), techniques=tuple(draw(st.lists(st.sampled_from(IDS), min_size=1,
                                                                                max_size=4, unique=True))),
        selections=tuple(sels), condition=draw(condition(names)), mass_fraud=f, mass_legit=legit,
        status=draw(st.sampled_from(list(RuleStatus))), target=target,
        description=draw(st.none() | st.text(TEXT, max_size=80)),
        version=draw(st.none() | st.integers(1, 10**6)),
    )


@settings(max_examples=60, deadline=None)
@given(r=rules())
def test_detection_rule_parse_format_parse_round_trip(r):
    """For all valid Detection_Rules, parsing then formatting then parsing produces a rule equal to
    the original.

    **Validates: Requirements 31.8**
    """
    once = parse_rule(format_rule(r))
    assert once == r
    twice = parse_rule(format_rule(once))
    assert twice == once
    assert format_rule(twice) == format_rule(once)

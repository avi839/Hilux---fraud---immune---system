"""Hypothesis properties of the Audit_Ledger (Req 35.5, 35.6).

Design (Increment 4 headline property): *for any* append-only ledger, verification SHALL report
valid; *for any* modification, deletion or reordering, it SHALL report a broken entry at or
before the first affected index.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from helix.soc.ledger import GENESIS_HASH, AuditCategory, AuditLedger, HeadRecord, verify_chain

pytestmark = pytest.mark.property

FAST = settings(max_examples=60, deadline=None, derandomize=True, database=None)
T0 = datetime(2024, 6, 1, tzinfo=timezone.utc)

json_leaf = (st.none() | st.booleans() | st.integers(-(2**63), 2**63)
             | st.floats(allow_nan=False, allow_infinity=False) | st.text(max_size=12))
json_value = st.recursive(json_leaf, lambda c: st.lists(c, max_size=3)
                          | st.dictionaries(st.text(max_size=6), c, max_size=3), max_leaves=8)
payloads = st.dictionaries(
    st.text(max_size=8),
    json_value | st.decimals(allow_nan=False, allow_infinity=False, places=2)
    | st.datetimes(timezones=st.just(timezone.utc)),
    max_size=4)
appends = st.lists(st.tuples(st.sampled_from(list(AuditCategory)), st.text(min_size=1, max_size=20)
                             .filter(lambda s: s.strip()), payloads), min_size=1, max_size=8)


def build(ops) -> AuditLedger:
    n = iter(range(10_000))
    lg = AuditLedger(clock=lambda: T0 + timedelta(microseconds=next(n) * 1_001))
    for cat, actor, payload in ops:
        lg.append(cat, actor, payload)
    return lg


@FAST
@given(appends)
def test_append_only_ledger_verifies(ops):
    """**Validates: Requirements 35.1, 35.2, 35.3, 35.5**"""
    lg = build(ops)
    es = lg.entries()
    assert [e.index for e in es] == list(range(len(ops)))
    assert es[0].prev_hash == GENESIS_HASH
    assert all(b.prev_hash == a.hash for a, b in zip(es, es[1:]))
    assert lg.head() == HeadRecord(len(ops) - 1, es[-1].hash)
    res = lg.verify()
    assert res.valid and res.first_broken_index is None and res.entries == len(ops)


FIELDS = ("payload_json", "actor", "category", "ts", "prev_hash", "hash", "index")


@st.composite
def tampering(draw):
    """``(ledger, tampered entries, first affected index)``."""
    lg = build(draw(appends))
    es = list(lg.entries())
    n = len(es)
    kind = draw(st.sampled_from(["modify", "delete", "reorder"] if n > 1 else ["modify", "delete"]))
    if kind == "modify":
        idxs = sorted(draw(st.sets(st.integers(0, n - 1), min_size=1, max_size=n)))
        for i in idxs:
            e = es[i]
            f = draw(st.sampled_from(FIELDS))
            if f == "index":
                new = draw(st.integers(-3, n + 3).filter(lambda v: v != e.index))
            elif f == "payload_json":
                new = draw(st.sampled_from(['{"x":1}', "{}", "not json", '{"tampered":true}']).filter(
                    lambda v, cur=e.payload_json: v != cur))
            elif f in ("prev_hash", "hash"):
                new = draw(st.text("0123456789abcdef", min_size=64, max_size=64).filter(
                    lambda v, cur=getattr(e, f): v != cur))
            else:
                new = getattr(e, f) + draw(st.sampled_from(["x", "Z", " "]))
            e = dataclasses.replace(e, **{f: new})
            if f != "hash" and draw(st.booleans()):  # an attacker who also re-hashes the entry
                h = e.recompute_hash()
                if h is not None:
                    e = dataclasses.replace(e, hash=h)
            es[i] = e
        return lg, es, idxs[0]
    if kind == "delete":
        gone = draw(st.sets(st.integers(0, n - 1), min_size=1, max_size=n))
        return lg, [e for i, e in enumerate(es) if i not in gone], min(gone)
    perm = draw(st.permutations(range(n)).filter(lambda p: list(p) != list(range(n))))
    first = next(p for p, q in enumerate(perm) if p != q)
    return lg, [es[q] for q in perm], first


@FAST
@given(tampering())
def test_any_tampering_is_reported_at_or_before_the_first_affected_entry(case):
    """**Validates: Requirements 35.4, 35.6**"""
    lg, tampered, first = case
    res = verify_chain(tampered, lg.head())
    assert not res.valid
    assert res.first_broken_index is not None and 0 <= res.first_broken_index <= first


@FAST
@given(appends, st.data())
def test_tampering_after_a_reload_from_duckdb_is_detected(ops, data):
    """**Validates: Requirements 35.4, 35.6** (stored rows edited directly)"""
    import duckdb

    con = duckdb.connect(":memory:")
    lg = AuditLedger(clock=lambda: T0, storage=con)
    for cat, actor, payload in ops:
        lg.append(cat, actor, payload)
    i = data.draw(st.integers(0, len(ops) - 1))
    con.execute("UPDATE audit_ledger SET actor = actor || '!' WHERE idx = ?", [i])
    res = AuditLedger(storage=con).verify()
    assert not res.valid and res.first_broken_index <= i
    con.close()


def test_decimal_payload_values_hash_as_strings():
    lg = build([(AuditCategory.CONFIG_CHANGE, "a", {"cost": Decimal("1.10")})])
    assert lg.entries()[0].payload == {"cost": "1.10"} and lg.verify().valid

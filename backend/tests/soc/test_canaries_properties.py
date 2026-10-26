"""Hypothesis properties of the deception layer: deterministic generation (Req 33.1) and
Canary mass bounds (Req 33.2)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from helix.codec import AuthEvent, TransactionEvent
from helix.codec.auth import AuthEventType
from helix.fusion import is_valid
from helix.soc.canaries import (
    CANARY_ID_FLOOR,
    ID_FORMATS,
    MIN_FRAUD_MASS,
    CanaryCounts,
    CanaryKind,
    CanaryRegistry,
)

pytestmark = pytest.mark.property

T0 = datetime(2024, 6, 1, tzinfo=timezone.utc)
SETTINGS = settings(max_examples=60, deadline=None, derandomize=True, database=None)
seeds = st.integers(0, 2**32 - 1)
counts = st.builds(CanaryCounts, st.integers(0, 40), st.integers(0, 40), st.integers(0, 40))


@SETTINGS
@given(seed=seeds, c=counts)
def test_canary_generation_is_deterministic_sized_disjoint_and_generator_shaped(seed, c):
    """Same seed and counts give the same Canaries; sizes match the counts; kinds are disjoint;
    every id has the generator's format with a number outside the generator's range.

    **Validates: Requirements 33.1, 33.5**
    """
    a, b = CanaryRegistry.generate(seed, c), CanaryRegistry.generate(seed, c)
    for k in CanaryKind:
        assert a.ids(k) == b.ids(k) and len(a.ids(k)) == c.of(k)
        assert all(ID_FORMATS[k].match(i) and int(i[3:9]) >= CANARY_ID_FLOOR for i in a.ids(k))
    assert len(set(a.ids())) == len(a.ids()) == c.accounts + c.payees + c.devices


# One event shape: a Transaction_Event or an Auth_Event, with a Canary (or nothing) planted
# in one or more of its account / payee / destination / device fields.
@st.composite
def events(draw, reg: CanaryRegistry):
    canary = st.sampled_from(reg.ids())
    plain_acc = st.integers(0, 99_999).map(lambda n: f"ACC{n:06d}")
    plain_dev = st.integers(0, 99_999).map(lambda n: f"DEV{n:06d}-0")
    pick = lambda plain: draw(st.one_of(st.none(), canary) if plain is None else st.one_of(plain, canary))  # noqa: E731
    if draw(st.booleans()):
        ev = TransactionEvent(tx_id="T1", ts=T0, src=pick(plain_acc), dst=pick(plain_acc),
                              amount=Decimal("5.00"), currency="USD", device_id=draw(st.one_of(st.none(), plain_dev, canary)))
    else:
        payee = draw(st.one_of(st.none(), plain_acc, canary))
        ev = AuthEvent(event_id="E1", ts=T0, account_id=pick(plain_acc), session_id="S1",
                       event_type=AuthEventType.NEW_PAYEE_ADDED if payee else AuthEventType.LOGIN_SUCCESS,
                       device_id=draw(st.one_of(st.none(), plain_dev, canary)), payee_account_id=payee)
    return ev


REG = CanaryRegistry.generate(11, CanaryCounts(5, 5, 5))


@SETTINGS
@given(ev=events(REG), m=st.floats(MIN_FRAUD_MASS, 1.0, allow_nan=False))
def test_canary_contribution_mass_bounds(ev, m):
    """An event yields Canary evidence iff it references a Canary; the evidence has
    ``m({FRAUD}) >= 0.95``, ``m({LEGIT}) = 0``, is a valid mass function and has weight 1.0.

    **Validates: Requirements 33.2**
    """
    reg = CanaryRegistry(REG.ids(CanaryKind.ACCOUNT), REG.ids(CanaryKind.PAYEE), REG.ids(CanaryKind.DEVICE),
                         fraud_mass=m)
    fields = [getattr(ev, f, None) for f in ("src", "dst", "account_id", "payee_account_id", "device_id")]
    touched = any(reg.is_canary(v) for v in fields)
    c = reg.contribution(ev)
    assert (c is not None) is touched is reg.touches(ev) is bool(reg.references(ev))
    if c is not None:
        assert c.mass.f >= MIN_FRAUD_MASS and c.mass.l == 0.0 and is_valid(c.mass)
        assert c.weight == 1.0 and str(c.kind) == "CANARY"

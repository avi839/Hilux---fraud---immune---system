"""Property tests for helix.changepoint (design Properties 33-35)."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

from helix.changepoint import (
    ChangePointDetector,
    ChangePointRecord,
    CPDKind,
    CPDState,
    advance,
    drift_mass,
)
from helix.config.schema import ChangePointConfig
from helix.fusion import VACUOUS

pytestmark = pytest.mark.property

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)

amounts = st.floats(min_value=1e-4, max_value=1e9, allow_nan=False, allow_infinity=False)
iats = st.floats(min_value=0.0, max_value=1e8, allow_nan=False, allow_infinity=False)
events = st.lists(st.tuples(amounts, iats), min_size=1, max_size=60)


@st.composite
def regime_sequences(draw):
    """Piecewise-stationary log-amount / log-iat sequences with random abrupt switches,
    so that candidates and confirmations actually occur."""
    out = []
    for _ in range(draw(st.integers(1, 4))):
        mu_a = draw(st.floats(0.0, 10.0))
        mu_i = draw(st.floats(2.0, 12.0))
        for _ in range(draw(st.integers(1, 25))):
            ja = draw(st.floats(-0.3, 0.3))
            ji = draw(st.floats(-0.5, 0.5))
            out.append((math.exp(mu_a + ja), math.expm1(mu_i + ji)))
    return out


def _run(det, seq, acct="A"):
    for i, (a, iat) in enumerate(seq):
        yield det.update(acct, a, None if i == 0 else iat, f"t{i}", T0 + timedelta(seconds=i))


# Feature: helix-fraud-immune-system, Property 33: BOCPD posterior validity
@given(seq=events, r_max=st.sampled_from([3, 7, 20, 300]))
def test_property_33_posterior_validity(seq, r_max):
    """**Validates: Requirements 9.1, 9.2**"""
    det = ChangePointDetector(r_max=r_max, r_small=2)
    for st_ in _run(det, seq):
        p = st_.probs()
        assert len(p) <= r_max + 1
        assert np.all(p >= 0.0) and np.all(p <= 1.0)
        assert abs(float(p.sum()) - 1.0) <= 1e-9
        assert st_.resets == 0  # finite inputs never hit the non-finite reset


def _rec(i: int) -> ChangePointRecord:
    ts = T0 + timedelta(seconds=i)
    return ChangePointRecord("A", f"t{i}", ts, 0.9, ts, f"t{i}", CPDKind.ABRUPT_BREAK)


def _model(triggers, n_confirm):
    kind, quiet, hist, cand = CPDKind.NONE, 0, [], None
    for i, trig in enumerate(triggers):
        if trig:
            kind, quiet, cand = CPDKind.ABRUPT_BREAK, 0, f"t{i}"
            hist.append(f"t{i}")
        elif kind is CPDKind.ABRUPT_BREAK:
            quiet += 1
            if quiet >= n_confirm:
                kind = CPDKind.REGIME_SHIFT
    return kind, quiet, hist, cand


def _check_drift(state, cp):
    m = drift_mass(state, cp, history_count=100, min_history=5)
    if state.kind is CPDKind.ABRUPT_BREAK:
        assert m.f >= cp.abrupt_break_mass and m.l == 0.0
    elif state.kind is CPDKind.REGIME_SHIFT:
        assert m.l >= cp.regime_shift_mass and m.f == 0.0
    else:
        assert m == VACUOUS
    assert abs(m.f + m.l + m.t - 1.0) <= 1e-9
    assert drift_mass(state, cp, history_count=4, min_history=5) == VACUOUS


# Feature: helix-fraud-immune-system, Property 34: Change-point state machine
@given(
    triggers=st.lists(st.booleans(), max_size=80),
    n_confirm=st.integers(1, 12),
    masses=st.tuples(st.floats(0, 1), st.floats(0, 1)),
)
def test_property_34_state_machine_model(triggers, n_confirm, masses):
    """**Validates: Requirements 5.11, 9.3, 9.4, 9.5, 9.6, 9.8**"""
    cp = ChangePointConfig(abrupt_break_mass=masses[0], regime_shift_mass=masses[1])
    state = CPDState("A", np.zeros(1), np.zeros((1, 2, 4)))
    for i, trig in enumerate(triggers):
        prev_kind = state.kind
        confirmed = advance(state, trig, _rec(i) if trig else None, n_confirm)
        kind, quiet, hist, cand = _model(triggers[: i + 1], n_confirm)
        assert state.kind is kind and state.quiet_count == quiet
        assert [r.tx_id for r in state.history] == hist
        assert (state.candidate.tx_id if state.candidate else None) == cand
        assert confirmed == (prev_kind is CPDKind.ABRUPT_BREAK and kind is CPDKind.REGIME_SHIFT)
        if state.kind is CPDKind.REGIME_SHIFT:
            assert state.candidate.outcome is CPDKind.REGIME_SHIFT == state.history[-1].outcome
        _check_drift(state, cp)


# Feature: helix-fraud-immune-system, Property 34: Change-point state machine
@given(seq=regime_sequences(), n_confirm=st.integers(1, 12), theta=st.floats(0.05, 0.95))
def test_property_34_detector_follows_model(seq, n_confirm, theta):
    """The detector's candidates are exactly the armed rising edges of its short-run mass,
    and its state follows the model on that trigger sequence.

    **Validates: Requirements 5.11, 9.3, 9.4, 9.5, 9.6, 9.8**"""
    det = ChangePointDetector(threshold=theta, regime_confirmation_count=n_confirm)
    cp = ChangePointConfig(threshold=theta, regime_confirmation_count=n_confirm)
    triggers, s_prev = [], 0.0
    for i, state in enumerate(_run(det, seq)):
        s = state.s_prev  # short-run mass of this step
        triggers.append(i + 1 > det.r_small + 1 and s > theta and s_prev <= theta)
        s_prev = s
        kind, quiet, hist, cand = _model(triggers, n_confirm)
        assert state.kind is kind and state.quiet_count == quiet
        assert [r.tx_id for r in state.history] == hist
        assert (state.candidate.tx_id if state.candidate else None) == cand
        if triggers[-1]:
            assert state.candidate.short_run_mass > theta and state.candidate.ts == T0 + timedelta(seconds=i)
        _check_drift(state, cp)


# Feature: helix-fraud-immune-system, Property 35: Non-finite posterior resets
@given(
    seq=regime_sequences(),
    where=st.sampled_from(["log_p", "suff"]),
    bad=st.sampled_from([math.nan, math.inf, -math.inf]),
    pos=st.integers(0, 10_000),
    nxt=st.tuples(amounts, iats),
)
def test_property_35_non_finite_resets(seq, where, bad, pos, nxt):
    """**Validates: Requirements 9.10**"""
    det = ChangePointDetector()
    for state in _run(det, seq):
        pass
    history = list(state.history)
    arr = getattr(state, where).reshape(-1)
    arr[pos % arr.size] = bad
    state = det.update("A", nxt[0], nxt[1], "bad", T0 + timedelta(days=1))
    assert state.log_p.tolist() == [0.0]
    np.testing.assert_array_equal(state.suff, det.prior[None])
    assert state.kind is CPDKind.NONE and state.candidate is None
    assert state.history == history
    assert drift_mass(state, ChangePointConfig(), 100, 5) == VACUOUS
